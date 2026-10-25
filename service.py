"""Low-impact background synchronization and ChangeNotify monitoring for Rclone Nexus.

The service is designed for small Android TV devices:
- no media is cached to disk by the watcher;
- no database is created;
- at most a small number of long-lived rclone ChangeNotify processes are used;
- scans are deferred while video is playing;
- change bursts are debounced into one synchronization.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

import xbmc
import xbmcaddon

ADDON = xbmcaddon.Addon()
ADDON_DIR = ADDON.getAddonInfo('path')
sys.path.insert(0, os.path.join(ADDON_DIR, 'resources', 'lib'))

from ariostv.rclone_backend import RcloneBackend, RcloneCancelled, cleanup_managed_servers  # noqa: E402
from ariostv import library_manager as library  # noqa: E402
from ariostv.i18n import english, tr  # noqa: E402
from ariostv.kodi import find_conf_path, setting_bool, setting_int  # noqa: E402

_NOTICE_PATH_RE = re.compile(r'(?:NOTICE|INFO|DEBUG)\s*:\s*("(?:\\.|[^"\\])*")\s*:\s*\d+\s*$')
_DEBUG_PATH_RE = re.compile(r'changeNotify:\s*relativePath="([^"]*)"')


def _normalize_remote_path(value):
    return str(value or '').replace('\\', '/').strip('/')


def _path_matches_export(changed_path, export_path):
    changed = _normalize_remote_path(changed_path).casefold()
    root = _normalize_remote_path(export_path).casefold()
    if not changed or not root:
        return True
    return changed == root or changed.startswith(root + '/') or root.startswith(changed + '/')


def _extract_change_path(line):
    text = str(line or '').strip()
    match = _NOTICE_PATH_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            return None
    match = _DEBUG_PATH_RE.search(text)
    return match.group(1) if match else None


def synchronize_entries(backend, entries, monitor, low_impact=False):
    changed = False
    succeeded = []
    failed = {}
    for entry in entries:
        if monitor.abortRequested():
            raise RcloneCancelled(tr(30500))
        try:
            if hasattr(backend, 'set_scan_low_impact'):
                backend.set_scan_low_impact(low_impact)
            result = library.sync_export(backend, entry, abort_callback=monitor.abortRequested)
            if result.get('added') or result.get('removed') or result.get('updated'):
                changed = True
            succeeded.append(entry.get('id'))
        except RcloneCancelled:
            raise
        except library.LibraryBusy as exc:
            failed[entry.get('id')] = str(exc)
            xbmc.log('[Rclone Nexus/service] Library busy; will retry: %s' % entry.get('name'), xbmc.LOGINFO)
        except Exception as exc:
            library.mark_export_error(entry, exc)
            failed[entry.get('id')] = str(exc)
            xbmc.log('[Rclone Nexus/service] Error syncing %s: %s' % (entry.get('name'), english(exc)), xbmc.LOGERROR)
        finally:
            if hasattr(backend, 'set_scan_low_impact'):
                backend.set_scan_low_impact(False)
    if changed and setting_bool('auto_update_kodi_library', True):
        if low_impact:
            # Scrapers, artwork and Kodi database writes are considerably
            # heavier than the remote metadata scan. Defer only that stage.
            library.queue_library_update()
        else:
            library.request_library_update()
    return {'changed': changed, 'succeeded': succeeded, 'failed': failed}


class ChangeWatcher:
    def __init__(self, backend, remote, poll_minutes, on_change):
        self.backend = backend
        self.remote = remote
        self.poll_minutes = poll_minutes
        self.on_change = on_change
        self.proc = None
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        cmd = self.backend.change_notify_command(self.remote, '%dm' % self.poll_minutes)
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.backend._stream_env(),
            close_fds=True,
            universal_newlines=True,
            bufsize=1,
        )
        self.thread = threading.Thread(target=self._reader, name='RcloneNexusNotify-%s' % self.remote)
        self.thread.daemon = True
        self.thread.start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                if self.stop_event.is_set():
                    break
                changed_path = _extract_change_path(line)
                if changed_path is not None:
                    self.on_change(self.remote, changed_path)
        except Exception as exc:
            if not self.stop_event.is_set():
                xbmc.log('[Rclone Nexus/service] Watcher %s failed: %s' % (self.remote, english(exc)), xbmc.LOGWARNING)

    def alive(self):
        return bool(
            self.proc is not None and self.proc.poll() is None and
            self.thread is not None and self.thread.is_alive()
        )

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            if proc is not None and proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(timeout=1)


class SmartSyncManager:
    def __init__(self, backend):
        self.backend = backend
        self.watchers = {}
        self.dirty_lock = threading.Lock()
        self.last_refresh = 0
        self.feature_cache = {}
        state = library._load_json(self._state_path(), {'dirty': {}})
        dirty = state.get('dirty', {}) if isinstance(state, dict) else {}
        self.dirty = dirty if isinstance(dirty, dict) else {}

    @staticmethod
    def _state_path():
        return library.service_sync_state_path()

    def _save_dirty_locked(self):
        try:
            library._save_json(self._state_path(), {
                'version': 1,
                'updated_at': int(time.time()),
                'dirty': self.dirty,
            })
        except Exception as exc:
            xbmc.log('[Rclone Nexus/service] Could not persist the queue: %s' % english(exc), xbmc.LOGERROR)

    def mark_change(self, remote, changed_path):
        now = time.time()
        entries = [entry for entry in library.get_exports() if entry.get('remote') == remote]
        matched = False
        with self.dirty_lock:
            for entry in entries:
                if _path_matches_export(changed_path, entry.get('path', '')):
                    entry_id = entry.get('id')
                    previous = self.dirty.get(entry_id, {})
                    if not isinstance(previous, dict):
                        previous = {}
                    self.dirty[entry_id] = {
                        'detected_at': now,
                        'next_attempt': now + setting_int('change_debounce_seconds', 90, minimum=30, maximum=600),
                        'attempts': int(previous.get('attempts') or 0),
                        'last_error': str(previous.get('last_error') or '')[:500],
                    }
                    matched = True
            if matched:
                self._save_dirty_locked()
        if matched:
            try:
                self.backend.clear_list_cache()
            except Exception:
                pass
            xbmc.log('[Rclone Nexus/service] Change detected in %s:%s' % (remote, changed_path), xbmc.LOGINFO)

    def refresh_watchers(self, force=False):
        now = time.time()
        if not force and now - self.last_refresh < 600:
            return
        self.last_refresh = now
        if not setting_bool('auto_sync_enabled', False) or not setting_bool('smart_change_detection', True):
            self.stop_all()
            return
        exports = library.get_exports()
        remotes = []
        for entry in exports:
            remote = entry.get('remote')
            if remote and remote not in remotes:
                remotes.append(remote)
        max_watchers = setting_int('change_watch_max_processes', 2, minimum=1, maximum=4)
        selected = []
        for remote in remotes:
            if len(selected) >= max_watchers:
                break
            supported = self.feature_cache.get(remote)
            if supported is None:
                supported = self.backend.supports_change_notify(remote)
                self.feature_cache[remote] = supported
            if supported:
                selected.append(remote)
        for remote in list(self.watchers):
            if remote not in selected:
                self.watchers.pop(remote).stop()
        poll_minutes = setting_int('change_poll_minutes', 5, minimum=1, maximum=60)
        for remote in selected:
            watcher = self.watchers.get(remote)
            if watcher and watcher.alive():
                continue
            if watcher:
                watcher.stop()
            try:
                watcher = ChangeWatcher(self.backend, remote, poll_minutes, self.mark_change)
                watcher.start()
                self.watchers[remote] = watcher
                xbmc.log('[Rclone Nexus/service] ChangeNotify active for %s (every %d min)' % (remote, poll_minutes), xbmc.LOGINFO)
            except Exception as exc:
                xbmc.log('[Rclone Nexus/service] Could not start ChangeNotify for %s: %s' % (remote, english(exc)), xbmc.LOGWARNING)

    def due_dirty_entries(self):
        now = time.time()
        entries_by_id = {entry.get('id'): entry for entry in library.get_exports()}
        due = []
        with self.dirty_lock:
            changed = False
            for entry_id, record in list(self.dirty.items()):
                if not isinstance(record, dict):
                    record = {'detected_at': float(record or now), 'next_attempt': float(record or now)}
                    self.dirty[entry_id] = record
                    changed = True
                if now >= float(record.get('next_attempt') or record.get('detected_at') or 0):
                    entry = entries_by_id.get(entry_id)
                    if entry:
                        due.append(entry)
                    else:
                        self.dirty.pop(entry_id, None)
                        changed = True
            if changed:
                self._save_dirty_locked()
        return due

    def apply_sync_result(self, result):
        now = time.time()
        with self.dirty_lock:
            for entry_id in result.get('succeeded', []):
                self.dirty.pop(entry_id, None)
            for entry_id, error in result.get('failed', {}).items():
                previous = self.dirty.get(entry_id, {})
                if not isinstance(previous, dict):
                    previous = {}
                attempts = int(previous.get('attempts') or 0) + 1
                retry_seconds = min(3 * 3600, 300 * (2 ** min(attempts - 1, 6)))
                self.dirty[entry_id] = {
                    'detected_at': float(previous.get('detected_at') or now),
                    'next_attempt': now + retry_seconds,
                    'attempts': attempts,
                    'last_error': str(error or '')[:500],
                }
            self._save_dirty_locked()

    def record_entries_for_retry(self, entries, error='Scan pending'):
        now = time.time()
        with self.dirty_lock:
            for entry in entries:
                entry_id = entry.get('id')
                if not entry_id or entry_id in self.dirty:
                    continue
                self.dirty[entry_id] = {
                    'detected_at': now,
                    'next_attempt': now,
                    'attempts': 0,
                    'last_error': str(error)[:500],
                }
            self._save_dirty_locked()

    def has_notify_for(self, remote):
        watcher = self.watchers.get(remote)
        return bool(watcher and watcher.alive())

    def stop_all(self):
        for watcher in list(self.watchers.values()):
            watcher.stop()
        self.watchers.clear()


def _backend_identity():
    config = find_conf_path()
    if not config:
        return ('',)
    try:
        stat_result = os.stat(config)
        config_stamp = (stat_result.st_size, int(stat_result.st_mtime))
    except Exception:
        config_stamp = (0, 0)
    return (
        os.path.abspath(config), config_stamp,
        ADDON.getSetting('rclone_path'), ADDON.getSetting('rclone_install_dir'),
        ADDON.getSetting('rclone_arch'),
    )


def main():
    monitor = xbmc.Monitor()
    backend = None
    manager = None
    active_backend_identity = None
    next_backend_refresh = 0
    next_fallback_at = time.time()
    next_storage_cleanup = time.time()
    try:
        if monitor.waitForAbort(30):
            return
        while not monitor.abortRequested():
            playing = xbmc.Player().isPlayingVideo()
            scan_during_playback = setting_bool('sync_during_playback', False)
            can_scan = not playing or scan_during_playback
            now = time.time()

            if now >= next_backend_refresh:
                current_identity = _backend_identity()
                should_rebuild = backend is None or current_identity != active_backend_identity
                if should_rebuild:
                    if manager:
                        manager.stop_all()
                    backend = None
                    manager = None
                    config = current_identity[0]
                    if config:
                        try:
                            backend = RcloneBackend(config)
                            manager = SmartSyncManager(backend)
                            manager.refresh_watchers(force=True)
                            active_backend_identity = current_identity
                            xbmc.log('[Rclone Nexus/service] Automatic backend ready.', xbmc.LOGINFO)
                        except Exception as exc:
                            xbmc.log('[Rclone Nexus/service] Backend unavailable; will retry: %s' % english(exc), xbmc.LOGWARNING)
                    else:
                        xbmc.log('[Rclone Nexus/service] rclone.conf not available yet; will retry.', xbmc.LOGWARNING)
                next_backend_refresh = now + (300 if backend is None else 60)

            if not playing:
                idle_minutes = setting_int('server_idle_minutes', 20, minimum=1, maximum=1440)
                try:
                    cleanup_managed_servers(max_idle_seconds=idle_minutes * 60)
                except Exception as exc:
                    xbmc.log('[Rclone Nexus/service] Error cleaning up servers: %s' % english(exc), xbmc.LOGWARNING)
                try:
                    library.process_pending_library_update()
                except Exception as exc:
                    xbmc.log('[Rclone Nexus/service] Could not process the pending Kodi library update: %s' % english(exc), xbmc.LOGWARNING)

            if now >= next_storage_cleanup:
                try:
                    if backend:
                        backend.cleanup_storage(automatic=True)
                    library.cleanup_library_storage()
                except Exception as exc:
                    xbmc.log('[Rclone Nexus/service] Error cleaning up storage: %s' % english(exc), xbmc.LOGWARNING)
                next_storage_cleanup = now + 6 * 3600

            if manager:
                manager.refresh_watchers()
                if setting_bool('auto_sync_enabled', False) and can_scan:
                    due = manager.due_dirty_entries()
                    if due:
                        try:
                            result = synchronize_entries(backend, due, monitor, low_impact=playing)
                            manager.apply_sync_result(result)
                        except RcloneCancelled:
                            if monitor.abortRequested():
                                break

            if backend and setting_bool('auto_sync_enabled', False) and can_scan and now >= next_fallback_at:
                entries = library.get_exports()
                fallback = [entry for entry in entries if not manager or not manager.has_notify_for(entry.get('remote'))]
                safety_hours = setting_int('safety_full_scan_hours', 24, minimum=6, maximum=168)
                safety_due = [entry for entry in entries if now - int(entry.get('last_sync') or 0) >= safety_hours * 3600]
                merged = {entry.get('id'): entry for entry in fallback + safety_due if entry.get('id')}
                if merged:
                    scheduled = list(merged.values())
                    manager.record_entries_for_retry(scheduled)
                    try:
                        result = synchronize_entries(backend, scheduled, monitor, low_impact=playing)
                        manager.apply_sync_result(result)
                    except RcloneCancelled:
                        if monitor.abortRequested():
                            break
                fallback_minutes = setting_int('auto_sync_interval_minutes', 180, minimum=30, maximum=10080)
                next_fallback_at = time.time() + fallback_minutes * 60
            elif not setting_bool('auto_sync_enabled', False):
                next_fallback_at = now

            if monitor.waitForAbort(30):
                break
    finally:
        if manager:
            manager.stop_all()
        try:
            cleanup_managed_servers(force=True)
        except Exception as exc:
            xbmc.log('[Rclone Nexus/service] Error shutting down servers: %s' % english(exc), xbmc.LOGWARNING)


if __name__ == '__main__':
    main()
