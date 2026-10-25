"""STRM library export and incremental discovery for Rclone Nexus.

The module intentionally stores only tiny JSON manifests and .strm shortcuts.
Media files are never downloaded.
"""

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import xml.etree.ElementTree as ET

try:
    from .rclone_backend import RcloneCancelled
except Exception:
    class RcloneCancelled(RuntimeError):
        pass

from .i18n import tr
from .kodi import ADDON, ADDON_ID, mkdirs, setting_bool, setting_int, translate, xbmc
VIDEO_EXTENSIONS = {
    '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.iso', '.m2ts', '.mpg',
    '.mpeg', '.wmv', '.flv', '.webm', '.vob', '.3gp', '.ogm', '.ogv'
}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
REGISTRY_VERSION = 2
MANIFEST_VERSION = 3
EXPORT_MARKER = '.rclone-nexus-library.json'
LOCK_WAIT_SECONDS = 8


class LibraryBusy(RuntimeError):
    """Raised when another Kodi process is already changing the same library."""


def _is_within(path, parent):
    try:
        path_abs = os.path.abspath(path)
        parent_abs = os.path.abspath(parent)
        return os.path.commonpath([path_abs, parent_abs]) == parent_abs
    except Exception:
        return False


def _is_dangerous_destination(path):
    if not path:
        return True
    absolute = os.path.abspath(path)
    drive, tail = os.path.splitdrive(absolute)
    if absolute == os.path.abspath(os.sep) or (drive and tail in (os.sep, '')):
        return True
    protected = {
        os.path.abspath(profile_dir()),
        os.path.abspath(translate('special://profile/')),
    }
    return absolute in protected


def _marker_path(entry):
    return os.path.join(os.path.abspath(entry.get('destination', '')), EXPORT_MARKER)


def _expected_marker(entry):
    return {
        'schema': 1,
        'addon_id': ADDON_ID,
        'export_id': entry.get('id', ''),
        'remote': entry.get('remote', ''),
        'path': entry.get('path', ''),
    }


def _write_export_marker(entry):
    destination = os.path.abspath(entry.get('destination', ''))
    if _is_dangerous_destination(destination) or os.path.islink(destination):
        raise RuntimeError(tr(30600, path=destination))
    mkdirs(destination)
    marker_path = _marker_path(entry)
    expected = _expected_marker(entry)
    if os.path.lexists(marker_path):
        if os.path.islink(marker_path):
            raise RuntimeError(tr(30601, path=marker_path))
        existing = _load_json(marker_path, {})
        if not isinstance(existing, dict) or any(
            existing.get(key) != expected.get(key)
            for key in ('addon_id', 'export_id', 'remote', 'path')
        ):
            raise RuntimeError(tr(30602, path=destination))
        return
    _save_json(marker_path, expected)


def ensure_export_marker(entry, create_if_missing=False):
    destination = os.path.abspath(entry.get('destination', ''))
    if _is_dangerous_destination(destination) or os.path.islink(destination):
        raise RuntimeError(tr(30600, path=destination))
    expected_id = export_id(entry.get('remote', ''), entry.get('path', ''), destination)
    if entry.get('id') != expected_id:
        raise RuntimeError(tr(30603))
    marker_path = _marker_path(entry)
    if os.path.lexists(marker_path) and os.path.islink(marker_path):
        raise RuntimeError(tr(30601, path=marker_path))
    marker = _load_json(marker_path, {})
    if not marker and create_if_missing:
        _write_export_marker(entry)
        marker = _load_json(marker_path, {})
    expected = _expected_marker(entry)
    if not isinstance(marker, dict) or any(
        marker.get(key) != expected.get(key)
        for key in ('addon_id', 'export_id', 'remote', 'path')
    ):
        raise RuntimeError(tr(30604, path=destination))
    return True



def _safe_join(root, relative):
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, relative))
    if not _is_within(candidate, root_abs):
        raise RuntimeError(tr(30605, path=relative))
    # Do not follow a pre-existing symlink from the managed library into another tree.
    current = root_abs
    relative_parts = os.path.relpath(candidate, root_abs).split(os.sep)
    for part in relative_parts[:-1]:
        if part in ('', '.'):
            continue
        current = os.path.join(current, part)
        if os.path.lexists(current) and os.path.islink(current):
            raise RuntimeError(tr(30606, path=current))
    return candidate


def _unique_temp_path(path, label='tmp'):
    token = hashlib.sha1(os.urandom(24)).hexdigest()[:12]
    return '%s.%s.%s.%s' % (path, label, os.getpid(), token)



def _write_bytes_atomic(path, value):
    mkdirs(os.path.dirname(path))
    tmp = _unique_temp_path(path)
    try:
        with open(tmp, 'wb') as fh:
            fh.write(value)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _write_text_atomic(path, value):
    _write_bytes_atomic(path, value.encode('utf-8'))


def free_space_bytes(path):
    candidate = os.path.abspath(path or profile_dir())
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    try:
        return int(shutil.disk_usage(candidate).free)
    except Exception:
        return None


def ensure_storage_reserve(path, extra_bytes=0):
    reserve = setting_int('storage_reserve_mb', 128, minimum=16, maximum=2048) * 1024 * 1024
    available = free_space_bytes(path)
    required = reserve + max(0, int(extra_bytes or 0))
    if available is not None and available < required:
        raise RuntimeError(tr(30607, free=format_bytes(available), reserve=format_bytes(reserve)))
    return available


def _snapshot_managed_file(path, snapshots, max_bytes=1024 * 1024):
    if path in snapshots:
        return
    if not os.path.lexists(path):
        snapshots[path] = None
        return
    if os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError(tr(30608, path=path))
    with open(path, 'rb') as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(tr(30609, limit=max_bytes, path=path))
    snapshots[path] = data


def _restore_managed_files(snapshots, root):
    errors = []
    for path, previous in reversed(list(snapshots.items())):
        try:
            if previous is None:
                if os.path.lexists(path):
                    if os.path.islink(path) or not os.path.isfile(path):
                        raise RuntimeError('Rollback did not remove a non-regular destination: %s' % path)
                    os.remove(path)
                    _prune_empty_dirs(path, root)
            else:
                if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
                    raise RuntimeError('Rollback did not restore a non-regular destination: %s' % path)
                _write_bytes_atomic(path, previous)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        _log('Incomplete STRM rollback: %s' % '; '.join(errors[:5]), xbmc.LOGERROR)


def _unique_destination(remote, remote_path, requested):
    requested = os.path.abspath(requested)
    for current in get_exports():
        current_destination = os.path.abspath(current.get('destination', ''))
        if current_destination != requested:
            continue
        if current.get('remote') == remote and current.get('path', '') == (remote_path or '').strip('/'):
            return requested
        label = sanitize_component(remote or 'remote')
        candidate = requested + ' [' + label + ']'
        if any(os.path.abspath(item.get('destination', '')) == os.path.abspath(candidate) for item in get_exports()):
            candidate += ' [' + hashlib.sha1(('%s\0%s' % (remote, remote_path)).encode('utf-8', 'replace')).hexdigest()[:6] + ']'
        return candidate
    return requested

def _log(message, level=xbmc.LOGINFO):
    try:
        xbmc.log('[Rclone Nexus/library] ' + str(message), level)
    except Exception:
        pass


def profile_dir():
    path = translate(ADDON.getAddonInfo('profile') or ('special://profile/addon_data/%s/' % ADDON_ID))
    mkdirs(path)
    return path


def locks_dir():
    path = os.path.join(profile_dir(), 'locks')
    mkdirs(path)
    return path


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class _ExclusiveLock:
    """Small cross-process lock based only on APIs available in Kodi Python."""

    def __init__(self, name, abort_callback=None, wait_seconds=LOCK_WAIT_SECONDS):
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]+', '_', name)
        self.path = os.path.join(locks_dir(), safe_name + '.lock')
        self.abort_callback = abort_callback
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.owned = False

    def __enter__(self):
        deadline = time.monotonic() + self.wait_seconds
        stale_after = max(3 * 3600, setting_int('library_scan_timeout', 900, 60, 7200) + 600)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, ('%d\n%d\n' % (os.getpid(), int(time.time()))).encode('ascii'))
                finally:
                    os.close(fd)
                self.owned = True
                return self
            except OSError:
                try:
                    with open(self.path, 'r', encoding='ascii', errors='replace') as fh:
                        owner_pid = int((fh.readline() or '0').strip() or 0)
                    age = time.time() - os.path.getmtime(self.path)
                    if age > stale_after or (owner_pid and not _pid_alive(owner_pid)):
                        os.remove(self.path)
                        continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
                if self.abort_callback and self.abort_callback():
                    raise RcloneCancelled(tr(30610))
                if time.monotonic() >= deadline:
                    raise LibraryBusy(tr(30611))
                time.sleep(0.2)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.owned:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                _log('Could not release lock %s: %s' % (self.path, exc), xbmc.LOGWARNING)
        self.owned = False


def _resource_lock(remote, path, abort_callback=None):
    raw = ('%s\0%s' % (remote or '', (path or '').strip('/'))).encode('utf-8', 'replace')
    return _ExclusiveLock('scan-' + hashlib.sha1(raw).hexdigest(), abort_callback=abort_callback)


def _registry_lock():
    return _ExclusiveLock('registry', wait_seconds=LOCK_WAIT_SECONDS)


def registry_path():
    return os.path.join(profile_dir(), 'library_registry.json')


def manifests_dir():
    path = os.path.join(profile_dir(), 'library-manifests')
    mkdirs(path)
    return path


def discovery_dir():
    path = os.path.join(profile_dir(), 'discovery')
    mkdirs(path)
    return path


def recent_path():
    return os.path.join(profile_dir(), 'recent_content.json')


def favorites_path():
    return os.path.join(profile_dir(), 'favorites.json')


def kodi_update_path():
    return os.path.join(profile_dir(), 'kodi_update_pending.json')


def service_sync_state_path():
    return os.path.join(profile_dir(), 'service-sync-state.json')


def default_library_root():
    configured = ADDON.getSetting('library_root')
    root = translate(configured) if configured else os.path.join(profile_dir(), 'library')
    mkdirs(root)
    return root


def _load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            value = json.load(fh)
        return value
    except Exception:
        return default



def _save_json(path, value):
    mkdirs(os.path.dirname(path))
    tmp = _unique_temp_path(path)
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass




def load_registry():
    data = _load_json(registry_path(), {'version': REGISTRY_VERSION, 'exports': []})
    if not isinstance(data, dict):
        return {'version': REGISTRY_VERSION, 'exports': []}
    data['version'] = REGISTRY_VERSION
    exports = data.get('exports', [])
    data['exports'] = exports if isinstance(exports, list) else []
    return data



def save_registry(data):
    _save_json(registry_path(), data)


def export_id(remote, path, destination=''):
    raw = ('%s\0%s\0%s' % (remote or '', path or '', destination or '')).encode('utf-8', 'replace')
    return hashlib.sha1(raw).hexdigest()[:16]


def manifest_path(entry):
    return os.path.join(manifests_dir(), entry['id'] + '.json')


def get_exports():
    return list(load_registry().get('exports') or [])


def find_export(entry_id):
    for entry in get_exports():
        if entry.get('id') == entry_id:
            return entry
    return None


def upsert_export(entry):
    with _registry_lock():
        data = load_registry()
        replaced = False
        for index, current in enumerate(data['exports']):
            if current.get('id') == entry.get('id'):
                data['exports'][index] = entry
                replaced = True
                break
        if not replaced:
            data['exports'].append(entry)
        save_registry(data)
    return entry



def remove_export(entry_id, delete_files=False):
    entry = find_export(entry_id)
    if not entry:
        return None
    with _resource_lock(entry.get('remote'), entry.get('path')):
        if delete_files:
            _remove_tree(entry)
        with _registry_lock():
            data = load_registry()
            data['exports'] = [current for current in data.get('exports', []) if current.get('id') != entry_id]
            save_registry(data)
        try:
            os.remove(manifest_path(entry))
        except Exception:
            pass
    return entry





def _remove_tree(entry):
    """Remove only files recorded in the add-on manifest, never arbitrary user files."""
    ensure_export_marker(entry, create_if_missing=False)
    root = os.path.abspath(entry.get('destination', ''))
    if _is_dangerous_destination(root) or os.path.islink(root):
        raise RuntimeError(tr(30612, path=root))
    manifest = _load_json(manifest_path(entry), {'files': {}})
    files = manifest.get('files', {}) if isinstance(manifest, dict) else {}
    errors = []
    for record in files.values():
        if not isinstance(record, dict):
            continue
        relative = str(record.get('strm') or '')
        if not relative or not relative.lower().endswith('.strm'):
            continue
        try:
            target = _safe_join(root, os.path.join(*relative.split('/')))
            if os.path.lexists(target):
                if os.path.islink(target) or not os.path.isfile(target):
                    raise RuntimeError(tr(30613, path=target))
                os.remove(target)
                _prune_empty_dirs(target, root)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError(tr(30614, errors='; '.join(errors[:5])))
    try:
        marker = _marker_path(entry)
        if os.path.islink(marker):
            raise RuntimeError(tr(30615, path=marker))
        if os.path.isfile(marker):
            os.remove(marker)
    except Exception as exc:
        raise RuntimeError(tr(30616, error=exc))
    try:
        os.rmdir(root)
    except OSError:
        # Unrelated files are intentionally preserved.
        pass




def sanitize_component(name):
    value = INVALID_FILENAME.sub('_', str(name or '')).strip().rstrip('. ')
    return value or '_'


def relative_from_root(root_path, full_path):
    root = (root_path or '').strip('/')
    full = (full_path or '').strip('/')
    if root and full.startswith(root + '/'):
        return full[len(root) + 1:]
    if full == root:
        return os.path.basename(full)
    return full


def strm_relative_path(root_path, remote_file_path):
    relative = relative_from_root(root_path, remote_file_path)
    parts = [sanitize_component(part) for part in relative.split('/') if part]
    if not parts:
        parts = ['video']
    keep_extension = setting_bool('strm_keep_extension')
    filename = parts[-1]
    if not keep_extension:
        filename = os.path.splitext(filename)[0]
    parts[-1] = filename + '.strm'
    return os.path.join(*parts)


def plugin_play_url(remote, path, name=''):
    query = urllib.parse.urlencode({
        'action': 'play',
        'remote': remote or '',
        'path': path or '',
        'name': name or os.path.basename(path or ''),
    })
    return 'plugin://%s/?%s' % (ADDON_ID, query)


def is_video(path):
    return os.path.splitext((path or '').lower())[1] in VIDEO_EXTENSIONS


def _validate_remote_path(path):
    value = str(path or '')
    if '\x00' in value:
        raise RuntimeError(tr(30617))
    parts = value.replace('\\', '/').split('/')
    if any(part in ('.', '..') for part in parts):
        raise RuntimeError(tr(30618, path=value))
    return value



def create_export(remote, path, display_name, media_type='videos', destination=''):
    requested = destination or os.path.join(default_library_root(), sanitize_component(display_name or os.path.basename(path) or remote))
    root = translate(requested)
    root = _unique_destination(remote, path, root)
    if _is_dangerous_destination(root):
        raise RuntimeError(tr(30600, path=root))
    mkdirs(root)
    entry = {
        'id': export_id(remote, path, root),
        'remote': remote,
        'path': (path or '').strip('/'),
        'name': display_name or os.path.basename((path or '').rstrip('/')) or remote,
        'media_type': media_type or 'videos',
        'destination': root,
        'created_at': int(time.time()),
        'last_sync': 0,
        'file_count': 0,
        'last_added': 0,
        'last_removed': 0,
        'last_error': '',
    }
    _write_export_marker(entry)
    upsert_export(entry)
    return entry




def _file_signature(item):
    return {
        'size': int(item.get('size', 0) or 0),
        'modified': item.get('modified', '') or '',
        'id': item.get('id', '') or '',
        'orig_id': item.get('orig_id', '') or '',
    }



def _prune_empty_dirs(path, stop):
    current = os.path.dirname(path)
    stop = os.path.abspath(stop)
    while current and _is_within(current, stop) and os.path.abspath(current) != stop:
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)




def _sync_export_unlocked(backend, entry, delete_missing=None, abort_callback=None):
    """Synchronize one registered folder with bounded scan memory and transactional STRM updates."""
    if not entry:
        raise ValueError(tr(30619))
    if delete_missing is None:
        delete_missing = setting_bool('library_delete_missing', True)

    ensure_export_marker(entry, create_if_missing=True)
    manifest_file = manifest_path(entry)
    old_manifest = _load_json(manifest_file, {'version': MANIFEST_VERSION, 'files': {}})
    old_files = old_manifest.get('files', {}) if isinstance(old_manifest, dict) else {}
    if not isinstance(old_files, dict):
        old_files = {}
    first_sync = not bool(old_manifest.get('synced_at')) if isinstance(old_manifest, dict) else True

    old_local_owners = {}
    for old_remote_path, record in old_files.items():
        if not isinstance(record, dict):
            continue
        relative = str(record.get('strm') or '').replace('\\', '/')
        if relative.lower().endswith('.strm'):
            old_local_owners.setdefault(relative.casefold(), set()).add(old_remote_path)

    existing_local_paths = set()
    root = os.path.abspath(entry['destination'])
    for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(current_root, name))]
        for filename in files:
            if not filename.lower().endswith('.strm'):
                continue
            absolute = os.path.join(current_root, filename)
            relative = os.path.relpath(absolute, root).replace(os.sep, '/')
            existing_local_paths.add(relative.casefold())

    iterator = backend.iter_recursive(
        entry['remote'], entry.get('path', ''), video_only=True, abort_callback=abort_callback
    ) if hasattr(backend, 'iter_recursive') else iter(
        backend.list_recursive(entry['remote'], entry.get('path', ''), video_only=True)
    )
    current = {}
    added_recent = []
    added_count = 0
    updated = 0
    unchanged = 0
    used_local_paths = {}
    file_snapshots = {}
    write_count = 0
    processed_count = 0
    low_impact = bool(getattr(backend, '_scan_low_impact', False))

    def rollback_files():
        _restore_managed_files(file_snapshots, entry['destination'])

    def candidate_conflicts(relative, remote_path, old_record):
        key = relative.replace(os.sep, '/').casefold()
        if key in used_local_paths and used_local_paths[key] != remote_path:
            return True
        owners = old_local_owners.get(key, set())
        if owners and remote_path not in owners:
            # Preserve paths owned by prior manifest records until those records are processed.
            return True
        if key in existing_local_paths and not old_record and not owners:
            # Never overwrite a .strm that is not owned by the current manifest.
            return True
        return False

    def choose_relative(remote_path, old_record):
        if old_record and old_record.get('strm'):
            relative = str(old_record.get('strm')).replace('/', os.sep)
            if not relative.lower().endswith('.strm'):
                raise RuntimeError(tr(30620, path=relative))
        else:
            relative = strm_relative_path(entry.get('path', ''), remote_path)
        if not candidate_conflicts(relative, remote_path, old_record):
            return relative
        stem, suffix = os.path.splitext(relative)
        source_ext = os.path.splitext(remote_path)[1].lower().lstrip('.')
        labels = [source_ext] if source_ext else []
        labels.append(hashlib.sha1(remote_path.encode('utf-8', 'replace')).hexdigest()[:6])
        for label in labels:
            candidate = '%s [%s]%s' % (stem, label, suffix)
            if not candidate_conflicts(candidate, remote_path, old_record):
                return candidate
        counter = 2
        while counter < 10000:
            candidate = '%s [%s-%d]%s' % (stem, labels[-1], counter, suffix)
            if not candidate_conflicts(candidate, remote_path, old_record):
                return candidate
            counter += 1
        raise RuntimeError(tr(30621, path=remote_path))

    try:
        for item in iterator:
            if abort_callback and abort_callback():
                raise RcloneCancelled(tr(30622))
            remote_path = _validate_remote_path(item.get('path', ''))
            if not remote_path or not is_video(remote_path):
                continue
            processed_count += 1
            if low_impact and processed_count % 256 == 0:
                time.sleep(0.05)
            old = old_files.get(remote_path)
            rel = choose_relative(remote_path, old)
            collision_key = rel.replace(os.sep, '/').casefold()
            used_local_paths[collision_key] = remote_path
            local_path = _safe_join(entry['destination'], rel)
            signature = _file_signature(item)
            current[remote_path] = {
                'strm': rel.replace(os.sep, '/'),
                'size': signature['size'],
                'modified': signature['modified'],
                'id': signature['id'],
                'orig_id': signature['orig_id'],
            }
            if os.path.lexists(local_path) and (os.path.islink(local_path) or not os.path.isfile(local_path)):
                raise RuntimeError(tr(30608, path=local_path))
            needs_write = not old or not os.path.exists(local_path)
            if needs_write:
                if write_count % 64 == 0:
                    ensure_storage_reserve(entry['destination'])
                _snapshot_managed_file(local_path, file_snapshots)
                _write_text_atomic(
                    local_path,
                    plugin_play_url(entry['remote'], remote_path, item.get('name', '')) + '\n',
                )
                write_count += 1
                if low_impact:
                    time.sleep(0.02)
                existing_local_paths.add(collision_key)
                if not old:
                    added_count += 1
                    if len(added_recent) < 500:
                        added_recent.append(item)
                else:
                    updated += 1
            elif any(
                old.get(key, '') != current[remote_path].get(key, '')
                for key in ('size', 'modified', 'id', 'orig_id')
            ):
                updated += 1
            else:
                unchanged += 1
    except Exception:
        rollback_files()
        raise
    finally:
        try:
            iterator.close()
        except Exception:
            pass

    if abort_callback and abort_callback():
        rollback_files()
        raise RcloneCancelled(tr(30622))

    if old_files and not current and not setting_bool('library_allow_empty_remote', False):
        rollback_files()
        raise RuntimeError(tr(30623, count=len(old_files)))

    current_local_paths = {
        str(record.get('strm') or '').replace('\\', '/').casefold()
        for record in current.values() if isinstance(record, dict)
    }
    removed = 0
    old_manifest_bytes = None
    old_manifest_exists = os.path.isfile(manifest_file) and not os.path.islink(manifest_file)
    if old_manifest_exists:
        with open(manifest_file, 'rb') as fh:
            old_manifest_bytes = fh.read()

    now = int(time.time())
    manifest = {
        'version': MANIFEST_VERSION,
        'remote': entry['remote'],
        'path': entry.get('path', ''),
        'synced_at': now,
        'files': current,
    }
    updated_entry = dict(entry)
    updated_entry.update({
        'last_sync': now,
        'file_count': len(current),
        'last_added': 0 if first_sync else added_count,
        'last_removed': 0,
        'last_error': '',
    })

    try:
        if delete_missing:
            for remote_path, old in old_files.items():
                if abort_callback and abort_callback():
                    raise RcloneCancelled(tr(30622))
                if remote_path in current or not isinstance(old, dict):
                    continue
                rel = str(old.get('strm') or '').replace('\\', '/')
                if not rel.lower().endswith('.strm') or rel.casefold() in current_local_paths:
                    continue
                local_path = _safe_join(entry['destination'], os.path.join(*rel.split('/')))
                _snapshot_managed_file(local_path, file_snapshots)
                if os.path.lexists(local_path):
                    if os.path.islink(local_path) or not os.path.isfile(local_path):
                        raise RuntimeError(tr(30624, path=local_path))
                    os.remove(local_path)
                    removed += 1
                    if low_impact and removed % 64 == 0:
                        time.sleep(0.05)
                    _prune_empty_dirs(local_path, entry['destination'])

        if abort_callback and abort_callback():
            raise RcloneCancelled(tr(30622))
        updated_entry['last_removed'] = removed
        ensure_storage_reserve(manifest_file, extra_bytes=max(1024 * 1024, len(current) * 160))
        _save_json(manifest_file, manifest)
        upsert_export(updated_entry)
    except Exception:
        rollback_files()
        try:
            if old_manifest_exists:
                _write_bytes_atomic(manifest_file, old_manifest_bytes)
            elif os.path.isfile(manifest_file) and not os.path.islink(manifest_file):
                os.remove(manifest_file)
        except Exception as restore_exc:
            _log('Could not restore the previous manifest: %s' % restore_exc, xbmc.LOGERROR)
        raise

    if added_recent and not first_sync:
        try:
            add_recent_items(entry['remote'], added_recent, entry_id=entry['id'])
        except Exception as exc:
            _log('Could not update the new content list: %s' % exc, xbmc.LOGWARNING)

    return {
        'entry': updated_entry,
        'first_sync': first_sync,
        'total': len(current),
        'added': 0 if first_sync else added_count,
        'updated': updated,
        'removed': removed,
        'unchanged': unchanged,
        'destination': entry['destination'],
    }


def sync_export(backend, entry, delete_missing=None, abort_callback=None):
    """Run one export at a time for a remote path, across threads and Kodi processes."""
    if not entry:
        raise ValueError(tr(30619))
    with _resource_lock(entry.get('remote'), entry.get('path'), abort_callback=abort_callback):
        return _sync_export_unlocked(
            backend, entry, delete_missing=delete_missing, abort_callback=abort_callback
        )




def mark_export_error(entry, exc):
    if not entry:
        return
    current = dict(find_export(entry.get('id')) or entry)
    current['last_error'] = str(exc)[:1000]
    current['last_attempt'] = int(time.time())
    upsert_export(current)


def sync_all(backend_factory):
    results = []
    for entry in get_exports():
        try:
            backend = backend_factory()
            results.append(sync_export(backend, entry))
        except Exception as exc:
            mark_export_error(entry, exc)
            results.append({'entry': entry, 'error': str(exc)})
    return results


def _folder_scan_path(remote, path):
    raw = ('%s\0%s' % (remote or '', path or '')).encode('utf-8', 'replace')
    return os.path.join(discovery_dir(), hashlib.sha1(raw).hexdigest() + '.json')




def _scan_new_content_unlocked(backend, remote, path='', abort_callback=None):
    state_path = _folder_scan_path(remote, path)
    old = _load_json(state_path, {'files': {}})
    old_files = old.get('files', {}) if isinstance(old, dict) else {}
    if not isinstance(old_files, dict):
        old_files = {}
    first_scan = not bool(old.get('scanned_at')) if isinstance(old, dict) else True
    iterator = backend.iter_recursive(
        remote, path, video_only=True, abort_callback=abort_callback
    ) if hasattr(backend, 'iter_recursive') else iter(
        backend.list_recursive(remote, path, video_only=True)
    )
    current = {}
    added_items = []
    added_count = 0
    try:
        for item in iterator:
            if abort_callback and abort_callback():
                raise RcloneCancelled(tr(30622))
            item_path = _validate_remote_path(item.get('path', ''))
            if not is_video(item_path):
                continue
            current[item_path] = _file_signature(item)
            if item_path not in old_files:
                added_count += 1
                if len(added_items) < 500:
                    added_items.append(item)
    finally:
        try:
            iterator.close()
        except Exception:
            pass
    if abort_callback and abort_callback():
        raise RcloneCancelled(tr(30622))
    if old_files and not current and not setting_bool('library_allow_empty_remote', False):
        raise RuntimeError(tr(30625, count=len(old_files)))
    removed = sum(1 for item_path in old_files if item_path not in current)
    ensure_storage_reserve(state_path, extra_bytes=max(1024 * 1024, len(current) * 120))
    _save_json(
        state_path,
        {'version': 2, 'remote': remote, 'path': path, 'scanned_at': int(time.time()), 'files': current},
    )
    if added_items and not first_scan:
        add_recent_items(remote, added_items)
    return {
        'first_scan': first_scan,
        'total': len(current),
        'added': 0 if first_scan else added_count,
        'removed': removed,
        'items': [] if first_scan else added_items,
    }


def scan_new_content(backend, remote, path='', abort_callback=None):
    with _resource_lock(remote, path, abort_callback=abort_callback):
        return _scan_new_content_unlocked(
            backend, remote, path=path, abort_callback=abort_callback
        )




def add_recent_items(remote, items, entry_id=''):
    data = _load_json(recent_path(), {'version': 1, 'items': []})
    existing = {(x.get('remote'), x.get('path')): x for x in data.get('items', []) if isinstance(x, dict)}
    now = int(time.time())
    for item in items:
        key = (remote, item.get('path'))
        existing[key] = {
            'remote': remote,
            'path': item.get('path', ''),
            'name': item.get('name') or os.path.basename(item.get('path', '')),
            'size': int(item.get('size', 0) or 0),
            'modified': item.get('modified', ''),
            'discovered_at': now,
            'entry_id': entry_id,
        }
    max_items = 500
    values = sorted(existing.values(), key=lambda x: int(x.get('discovered_at', 0)), reverse=True)[:max_items]
    _save_json(recent_path(), {'version': 1, 'items': values})


def get_recent_items():
    data = _load_json(recent_path(), {'items': []})
    days = 30
    try:
        days = setting_int('new_content_days', 30, minimum=1)
    except Exception:
        pass
    cutoff = int(time.time()) - days * 86400
    items = [x for x in data.get('items', []) if int(x.get('discovered_at', 0) or 0) >= cutoff]
    return sorted(items, key=lambda x: int(x.get('discovered_at', 0)), reverse=True)


def clear_recent_items():
    _save_json(recent_path(), {'version': 1, 'items': []})


def load_favorites():
    data = _load_json(favorites_path(), {'items': []})
    return data.get('items', []) if isinstance(data, dict) else []


def is_favorite(remote, path):
    return any(x.get('remote') == remote and x.get('path', '') == (path or '') for x in load_favorites())


def toggle_favorite(remote, path, name=''):
    items = load_favorites()
    found = False
    kept = []
    for item in items:
        if item.get('remote') == remote and item.get('path', '') == (path or ''):
            found = True
        else:
            kept.append(item)
    if not found:
        kept.append({'remote': remote, 'path': path or '', 'name': name or os.path.basename((path or '').rstrip('/')) or remote})
    _save_json(favorites_path(), {'version': 1, 'items': kept})
    return not found



def _ensure_sources_tree(path):
    if os.path.exists(path):
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            if root.tag != 'sources':
                raise ValueError(tr(30626, tag=root.tag))
            return tree, root
        except Exception as exc:
            raise RuntimeError(tr(30627, error=exc))
    root = ET.Element('sources')
    for section in ('programs', 'video', 'music', 'pictures', 'files', 'games'):
        element = ET.SubElement(root, section)
        default = ET.SubElement(element, 'default')
        default.set('pathversion', '1')
    return ET.ElementTree(root), root


def _write_sources_tree(tree, path):
    tmp = _unique_temp_path(path, 'rclone-nexus-tmp')
    backup = path + '.rclone-nexus.bak'
    try:
        tree.write(tmp, encoding='UTF-8', xml_declaration=True)
        ET.parse(tmp)
        if os.path.exists(path):
            try:
                shutil.copy2(path, backup)
            except Exception as exc:
                raise RuntimeError(tr(30628, error=exc))
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass




def register_kodi_video_source(entry):
    """Add the local STRM folder to sources.xml without touching scraper settings."""
    ensure_export_marker(entry, create_if_missing=True)
    sources_path = translate('special://profile/sources.xml')
    mkdirs(os.path.dirname(sources_path))
    tree, root = _ensure_sources_tree(sources_path)
    video = root.find('video')
    if video is None:
        video = ET.SubElement(root, 'video')
    destination = os.path.abspath(entry['destination'])
    destination_with_sep = destination if destination.endswith(os.sep) else destination + os.sep
    for source in video.findall('source'):
        path_node = source.find('path')
        if path_node is not None and os.path.abspath((path_node.text or '').rstrip('/\\')) == destination.rstrip('/\\'):
            return False
    source = ET.SubElement(video, 'source')
    name = ET.SubElement(source, 'name')
    name.text = 'Rclone Nexus - %s' % entry.get('name', 'Library')
    path_node = ET.SubElement(source, 'path')
    path_node.set('pathversion', '1')
    path_node.text = destination_with_sep
    allow = ET.SubElement(source, 'allowsharing')
    allow.text = 'true'
    try:
        ET.indent(tree, space='  ')
    except Exception:
        pass
    _write_sources_tree(tree, sources_path)
    return True





def unregister_kodi_video_source(entry):
    sources_path = translate('special://profile/sources.xml')
    if not os.path.exists(sources_path):
        return False
    try:
        tree, root = _ensure_sources_tree(sources_path)
        video = root.find('video')
        if video is None:
            return False
        destination = os.path.abspath(entry.get('destination', '')).rstrip('/\\')
        removed = False
        for source in list(video.findall('source')):
            path_node = source.find('path')
            candidate = os.path.abspath((path_node.text or '').rstrip('/\\')) if path_node is not None else ''
            if candidate == destination:
                video.remove(source)
                removed = True
        if removed:
            try:
                ET.indent(tree, space='  ')
            except Exception:
                pass
            _write_sources_tree(tree, sources_path)
        return removed
    except Exception as exc:
        _log('Could not remove the Kodi source: %s' % exc, xbmc.LOGWARNING)
        return False


def _kodi_is_scanning():
    try:
        return bool(
            xbmc.getCondVisibility('Library.IsScanningVideo') or
            xbmc.getCondVisibility('Library.IsScanning')
        )
    except Exception:
        return False


def queue_library_update(path=''):
    with _ExclusiveLock('kodi-update', wait_seconds=3):
        state = _load_json(kodi_update_path(), {'pending': False, 'paths': []})
        paths = list(state.get('paths') or []) if isinstance(state, dict) else []
        clean = str(path or '').replace('"', '')
        if clean and clean not in paths and len(paths) < 20:
            paths.append(clean)
        _save_json(kodi_update_path(), {
            'pending': True,
            'paths': paths,
            'queued_at': int(time.time()),
        })


def request_library_update(path=''):
    """Request a Kodi scan, or persist it for the service if Kodi is busy."""
    try:
        if _kodi_is_scanning():
            queue_library_update(path)
            _log('Kodi is already scanning; update saved as pending.', xbmc.LOGINFO)
            return False
        if path:
            safe = path.replace('"', '')
            xbmc.executebuiltin('UpdateLibrary(video,"%s")' % safe)
        else:
            xbmc.executebuiltin('UpdateLibrary(video)')
        return True
    except Exception as exc:
        try:
            queue_library_update(path)
        except Exception:
            pass
        _log('Could not request UpdateLibrary: %s' % exc, xbmc.LOGWARNING)
        return False


def process_pending_library_update():
    """Submit one persisted Kodi update after the current scanner becomes idle."""
    if _kodi_is_scanning() or not os.path.exists(kodi_update_path()):
        return False
    with _ExclusiveLock('kodi-update', wait_seconds=1):
        state = _load_json(kodi_update_path(), {'pending': False})
        if not isinstance(state, dict) or not state.get('pending') or _kodi_is_scanning():
            return False
        # A full scan safely coalesces updates for several export destinations.
        xbmc.executebuiltin('UpdateLibrary(video)')
        try:
            os.remove(kodi_update_path())
        except FileNotFoundError:
            pass
        return True


def _tree_usage(path):
    result = {'logical': 0, 'allocated': 0, 'files': 0}
    if not os.path.isdir(path):
        return result
    for current_root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(current_root, name))]
        for name in files:
            target = os.path.join(current_root, name)
            try:
                stat_result = os.stat(target, follow_symlinks=False)
                result['logical'] += stat_result.st_size
                blocks = getattr(stat_result, 'st_blocks', 0)
                result['allocated'] += blocks * 512 if blocks else stat_result.st_size
                result['files'] += 1
            except Exception:
                pass
    return result


def library_storage_usage():
    destinations = []
    for entry in get_exports():
        destination = os.path.abspath(entry.get('destination') or '')
        if destination and destination not in destinations:
            destinations.append(destination)
    if not destinations:
        destinations.append(default_library_root())
    strm_usage = {'logical': 0, 'allocated': 0, 'files': 0}
    for destination in destinations:
        usage = _tree_usage(destination)
        for key in strm_usage:
            strm_usage[key] += usage[key]
    result = {
        'strm': strm_usage,
        'manifests': _tree_usage(manifests_dir()),
        'discovery': _tree_usage(discovery_dir()),
    }
    metadata = {'logical': 0, 'allocated': 0, 'files': 0}
    for path in (registry_path(), recent_path(), favorites_path(), kodi_update_path(), service_sync_state_path()):
        try:
            stat_result = os.stat(path, follow_symlinks=False)
            metadata['logical'] += stat_result.st_size
            blocks = getattr(stat_result, 'st_blocks', 0)
            metadata['allocated'] += blocks * 512 if blocks else stat_result.st_size
            metadata['files'] += 1
        except Exception:
            pass
    result['metadata'] = metadata
    result['logical'] = sum(item['logical'] for item in result.values())
    result['allocated'] = sum(item['allocated'] for item in result.values() if isinstance(item, dict))
    result['files'] = sum(item['files'] for item in result.values() if isinstance(item, dict))
    free_values = [free_space_bytes(path) for path in destinations]
    free_values = [value for value in free_values if value is not None]
    result['free'] = min(free_values) if free_values else None
    return result


def cleanup_library_storage():
    """Remove only orphan/old metadata; managed STRM files are never pruned here."""
    removed = 0
    active_ids = {entry.get('id') for entry in get_exports() if entry.get('id')}
    folder = manifests_dir()
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if name.endswith('.json') and name[:-5] not in active_ids:
            target = os.path.join(folder, name)
            try:
                removed += os.path.getsize(target)
                os.remove(target)
            except Exception:
                pass
    discovery_files = []
    now = time.time()
    for name in os.listdir(discovery_dir()) if os.path.isdir(discovery_dir()) else []:
        target = os.path.join(discovery_dir(), name)
        try:
            stat_result = os.stat(target)
            if os.path.isfile(target):
                discovery_files.append((target, stat_result.st_size, stat_result.st_mtime))
        except Exception:
            pass
    # Old ad-hoc discovery baselines are expendable; an eventual scan recreates them.
    for target, size, mtime in discovery_files:
        if now - mtime > 90 * 86400:
            try:
                os.remove(target)
                removed += size
            except Exception:
                pass
    return removed


def format_bytes(size):
    value = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024.0 or unit == 'TB':
            return ('%.1f %s' % (value, unit)) if unit != 'B' else ('%d B' % value)
        value /= 1024.0
