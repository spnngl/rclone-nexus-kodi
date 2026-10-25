"""Rclone Nexus 1.3.6 - Kodi video addon for local rclone remotes."""

import configparser
import datetime
import mimetypes
import os
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id') or 'plugin.ariostv'
ADDON_NAME = ADDON.getAddonInfo('name') or 'Rclone Nexus'
ADDON_DIR = ADDON.getAddonInfo('path')
sys.path.insert(0, os.path.join(ADDON_DIR, 'resources', 'lib'))

from ariostv.rclone_backend import (
    RcloneBackend, RcloneError, RcloneCancelled, find_rclone, import_manual_rclone,
    diagnostic_text, get_last_report, detected_android_arch, inspect_rclone,
    cleanup_managed_servers, MIN_RCLONE_VERSION_TEXT,
)
from ariostv import library_manager as library
from ariostv.i18n import tr
from ariostv.kodi import find_conf_path, setting_bool

HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ARGS = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)

VIDEO_EXTENSIONS = {
    '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.iso', '.m2ts', '.mpg',
    '.mpeg', '.wmv', '.flv', '.webm', '.vob', '.3gp', '.ogm', '.ogv', '.strm'
}
PREFERRED_REMOTE_TYPES = ('crypt', 'combine', 'union', 'alias', 'chunker', 'compress')
LOW_PRIORITY_REMOTE_TYPES = ('onedrive', 'drive', 'dropbox', 'box', 'pcloud')
_backend_cache = {}


def build_url(**kwargs):
    return BASE_URL + '?' + urllib.parse.urlencode(kwargs)


def plugin_url(**kwargs):
    return 'plugin://%s/?%s' % (ADDON_ID, urllib.parse.urlencode(kwargs))


def get_param(key, default=''):
    return ARGS.get(key, [default])[0]


def notify(message, heading=None, icon=xbmcgui.NOTIFICATION_INFO, milliseconds=4500):
    try:
        xbmcgui.Dialog().notification(heading or ADDON_NAME, message, icon, milliseconds)
    except Exception:
        pass


def require_conf_path(show_dialog=True):
    conf_path = find_conf_path()
    if not conf_path:
        if show_dialog:
            xbmcgui.Dialog().ok(ADDON_NAME, tr(30100))
        return ''
    return conf_path


def get_backend(show_dialog=True):
    conf_path = require_conf_path(show_dialog=show_dialog)
    if not conf_path:
        return None
    try:
        key = conf_path + ':' + str(os.path.getmtime(conf_path))
    except Exception:
        key = conf_path
    if key not in _backend_cache:
        _backend_cache.clear()
        _backend_cache[key] = RcloneBackend(conf_path)
    return _backend_cache[key]


def parse_config_remotes(conf_path):
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with open(conf_path, 'r', encoding='utf-8-sig') as fh:
            parser.read_file(fh)
    except Exception:
        return []
    remotes = []
    for section in parser.sections():
        cfg = dict(parser.items(section))
        remotes.append({'name': section, 'type': (cfg.get('type') or '').lower()})
    return remotes


def preferred_remotes(conf_path):
    remotes = parse_config_remotes(conf_path)
    if not remotes:
        try:
            backend = get_backend(show_dialog=False)
            names = backend.list_remotes() if backend else []
            return [{'name': name, 'type': ''} for name in names]
        except Exception:
            return []

    def score(item):
        remote_type = item.get('type', '')
        if remote_type in PREFERRED_REMOTE_TYPES:
            return (0, item['name'].lower())
        if remote_type in LOW_PRIORITY_REMOTE_TYPES:
            return (2, item['name'].lower())
        return (1, item['name'].lower())
    return sorted(remotes, key=score)


def is_video(name):
    return os.path.splitext((name or '').lower())[1] in VIDEO_EXTENSIONS


def guess_mime(name):
    mime, _ = mimetypes.guess_type(name or '')
    if mime:
        return mime
    extension = os.path.splitext((name or '').lower())[1]
    return {
        '.mkv': 'video/x-matroska',
        '.ts': 'video/mp2t',
        '.webm': 'video/webm',
        '.iso': 'application/octet-stream',
    }.get(extension, 'video/mp4')


def set_category(text):
    try:
        xbmcplugin.setPluginCategory(HANDLE, text)
    except Exception:
        pass


def add_folder(label, url, icon='DefaultFolder.png', context=None, title=None):
    item = xbmcgui.ListItem(label, offscreen=True)
    item.setArt({'icon': icon, 'thumb': icon})
    if title:
        item.getVideoInfoTag().setTitle(title)
    if context:
        item.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, url, item, True)


def add_action(label, action, icon='DefaultAddonProgram.png', context=None):
    item = xbmcgui.ListItem(label, offscreen=True)
    item.setArt({'icon': icon, 'thumb': icon})
    if context:
        item.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, action, item, False)


def add_video_item(remote, item, prefix=''):
    name = item.get('name') or os.path.basename(item.get('path', ''))
    label = prefix + name
    li = xbmcgui.ListItem(label, offscreen=True)
    li.setProperty('IsPlayable', 'true')
    li.setArt({'icon': 'DefaultVideo.png', 'thumb': 'DefaultVideo.png'})
    li.getVideoInfoTag().setTitle(name)
    size = int(item.get('size', 0) or 0)
    try:
        if size:
            # 'size' has no InfoTagVideo setter and is not deprecated in setInfo().
            li.setInfo('video', {'size': size})
        li.setMimeType(guess_mime(name))
    except Exception:
        pass
    context = [
        (tr(30101), 'PlayMedia(%s)' % plugin_url(action='play', remote=remote, path=item.get('path', ''), name=name)),
        (tr(30102), 'Container.Refresh'),
    ]
    li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action='play', remote=remote, path=item.get('path', ''), name=name),
        li,
        False,
    )


def folder_context(remote, path, name):
    favorite_label = tr(30103) if library.is_favorite(remote, path) else tr(30104)
    return [
        (tr(30105), 'RunPlugin(%s)' % plugin_url(action='export_folder', remote=remote, path=path, name=name)),
        (tr(30106), 'RunPlugin(%s)' % plugin_url(action='scan_new', remote=remote, path=path, name=name)),
        (tr(30107), 'Container.Update(%s)' % plugin_url(action='search', remote=remote, path=path)),
        (favorite_label, 'RunPlugin(%s)' % plugin_url(action='toggle_favorite', remote=remote, path=path, name=name)),
        (tr(30108), 'RunPlugin(%s)' % plugin_url(action='refresh', remote=remote, path=path)),
    ]


def show_root():
    xbmcplugin.setContent(HANDLE, 'files')
    set_category(ADDON_NAME)
    conf_path = require_conf_path(show_dialog=False)
    if not conf_path:
        add_action('[COLOR orange]%s[/COLOR]' % tr(30109), build_url(action='settings'))
        add_folder(tr(30110), build_url(action='tools'), 'DefaultAddonProgram.png')
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return

    recent_count = len(library.get_recent_items())
    export_count = len(library.get_exports())
    favorite_count = len(library.load_favorites())
    add_folder(tr(30111, count=export_count), build_url(action='libraries'), 'DefaultVideoPlaylists.png')
    add_folder(tr(30112, count=recent_count), build_url(action='new_content'), 'DefaultRecentlyAddedMovies.png')
    add_folder(tr(30113, count=favorite_count), build_url(action='favorites'), 'DefaultFavourites.png')

    selected = (ADDON.getSetting('remote_name') or '').strip()
    remotes = preferred_remotes(conf_path)
    if selected:
        remotes.sort(key=lambda item: 0 if item['name'] == selected else 1)
    for item in remotes:
        name = item['name']
        remote_type = item.get('type') or 'remote'
        label = '%s  [COLOR gray](%s)[/COLOR]' % (name, remote_type)
        add_folder(
            label,
            build_url(action='browse', remote=name, path=''),
            'DefaultNetwork.png',
            context=folder_context(name, '', name),
        )
    add_folder(tr(30114), build_url(action='tools'), 'DefaultAddonProgram.png')
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def show_folder(remote, path=''):
    try:
        backend = get_backend()
        if not backend:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
            return
        items = backend.list_folder(remote, path or '')
    except Exception as exc:
        xbmc.log('[Rclone Nexus] Listing error: %s' % exc, xbmc.LOGERROR)
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30115, error=exc))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    location = '%s:/%s' % (remote, path.strip('/')) if path else '%s:/' % remote
    set_category(location)

    added = 0
    for item in items:
        name = item['name']
        item_path = item['path']
        if item['is_folder']:
            label = name
            if library.is_favorite(remote, item_path):
                label = '★ ' + label
            add_folder(
                label,
                build_url(action='browse', remote=remote, path=item_path),
                'DefaultFolder.png',
                context=folder_context(remote, item_path, name),
                title=name,
            )
            added += 1
        elif is_video(name):
            add_video_item(remote, item)
            added += 1

    if added == 0:
        add_action('[%s]' % tr(30116), build_url(action='refresh', remote=remote, path=path), 'DefaultFolder.png')

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE)
    try:
        xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_SIZE)
    except Exception:
        pass
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def play_item(remote, path, name):
    try:
        backend = get_backend()
        if not backend:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        url = backend.playback_url(remote, path)
    except Exception as exc:
        xbmc.log('[Rclone Nexus] Playback preparation error: %s' % exc, xbmc.LOGERROR)
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30117, error=exc))
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    li = xbmcgui.ListItem(path=url, offscreen=True)
    try:
        li.setMimeType(guess_mime(name or path))
        li.setContentLookup(False)
        li.getVideoInfoTag().setTitle(name or os.path.basename(path))
    except Exception:
        pass
    xbmc.log('[Rclone Nexus] Playing %s:%s via verified local server.' % (remote, path), xbmc.LOGINFO)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)




def show_tools():
    xbmcplugin.setContent(HANDLE, 'files')
    set_category('%s · %s' % (ADDON_NAME, tr(30118)))
    items = [
        (tr(30119, version=MIN_RCLONE_VERSION_TEXT), 'check_rclone'),
        (tr(30120), 'import_rclone'),
        (tr(30121), 'sync_all'),
        (tr(30122), 'cleanup_storage'),
        (tr(30123), 'clear_cache'),
        (tr(30124), 'stop_rclone'),
        (tr(30125), 'diagnostic'),
        (tr(30126), 'settings'),
    ]
    for label, action in items:
        add_action(label, build_url(action=action))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def show_libraries():
    xbmcplugin.setContent(HANDLE, 'files')
    set_category('%s · %s' % (ADDON_NAME, tr(30127)))
    exports = library.get_exports()
    add_action('[%s]' % tr(30128), build_url(action='sync_all'), 'DefaultAddonProgram.png')
    if not exports:
        add_action('[%s]' % tr(30129), build_url(action='root'), 'DefaultFolder.png')
    for entry in exports:
        last_sync = entry.get('last_sync', 0)
        last_text = datetime.datetime.fromtimestamp(last_sync).strftime('%Y-%m-%d %H:%M') if last_sync else tr(30130)
        label = '%s  [COLOR gray](%s)[/COLOR]' % (
            entry.get('name', tr(30131)), tr(30132, count=int(entry.get('file_count', 0) or 0), last_sync=last_text)
        )
        if entry.get('last_error'):
            label = '[COLOR red]⚠[/COLOR] ' + label
        context = [
            (tr(30133), 'RunPlugin(%s)' % plugin_url(action='sync_export', export_id=entry['id'])),
            (tr(30134), 'Container.Update(%s)' % plugin_url(action='browse', remote=entry['remote'], path=entry.get('path', ''))),
            (tr(30135), 'RunPlugin(%s)' % plugin_url(action='remove_export', export_id=entry['id'])),
            (tr(30136), 'RunPlugin(%s)' % plugin_url(action='remove_export', export_id=entry['id'], delete='1')),
        ]
        if entry.get('last_error'):
            context.insert(0, (tr(30137), 'RunPlugin(%s)' % plugin_url(action='export_error', export_id=entry['id'])))
        add_folder(label, build_url(action='browse', remote=entry['remote'], path=entry.get('path', '')), 'DefaultVideoPlaylists.png', context=context)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def show_new_content():
    xbmcplugin.setContent(HANDLE, 'videos')
    set_category('%s · %s' % (ADDON_NAME, tr(30138)))
    items = library.get_recent_items()
    add_action('[%s]' % tr(30139), build_url(action='clear_new'), 'DefaultAddonProgram.png')
    if not items:
        add_action('[%s]' % tr(30140), build_url(action='libraries'), 'DefaultRecentlyAddedMovies.png')
    for item in items:
        date_text = datetime.datetime.fromtimestamp(int(item.get('discovered_at', 0) or 0)).strftime('%Y-%m-%d')
        add_video_item(item.get('remote', ''), item, prefix='[COLOR lime]%s[/COLOR] %s · ' % (tr(30141), date_text))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def show_favorites():
    xbmcplugin.setContent(HANDLE, 'files')
    set_category('%s · %s' % (ADDON_NAME, tr(30142)))
    favorites = library.load_favorites()
    if not favorites:
        add_action('[%s]' % tr(30143), build_url(action='root'), 'DefaultFavourites.png')
    for item in favorites:
        remote = item.get('remote', '')
        path = item.get('path', '')
        name = item.get('name') or os.path.basename(path.rstrip('/')) or remote
        add_folder('%s  [COLOR gray](%s)[/COLOR]' % (name, remote), build_url(action='browse', remote=remote, path=path), 'DefaultFavourites.png', context=folder_context(remote, path, name))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)




def export_folder_action(remote, path, name):
    choices = [tr(30144), tr(30145), tr(30146)]
    media_values = ['movies', 'tvshows', 'videos']
    selected = xbmcgui.Dialog().select(tr(30147), choices)
    if selected < 0:
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    destination_root = library.default_library_root()
    folder_name = library.sanitize_component(name or os.path.basename(path.rstrip('/')) or remote)
    destination = os.path.join(destination_root, folder_name)
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, '%s\n%s' % (tr(30148), folder_name))
    entry = None
    try:
        entry = library.create_export(remote, path, name or folder_name, media_values[selected], destination)
        progress.update(5, '%s\n%s' % (tr(30149), folder_name))
        backend = get_backend()
        result = library.sync_export(backend, entry, abort_callback=progress.iscanceled)
        progress.update(90, '%s\n%s' % (tr(30150), result['destination']))
        source_added = library.register_kodi_video_source(result['entry'])
        if setting_bool('auto_update_kodi_library', True):
            library.request_library_update(result['destination'])
    except RcloneCancelled:
        if entry:
            try:
                library.remove_export(entry.get('id'), delete_files=True)
            except Exception as cleanup_exc:
                xbmc.log('[Rclone Nexus] Could not roll back a cancelled export: %s' % cleanup_exc, xbmc.LOGWARNING)
        notify(tr(30151))
    except Exception as exc:
        if entry:
            library.mark_export_error(entry, exc)
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30152, error=exc))
    else:
        if source_added:
            message = tr(30153, total=result['total'], destination=result['destination'])
        else:
            message = tr(30154, total=result['total'], destination=result['destination'])
        xbmcgui.Dialog().ok(ADDON_NAME, message)
    finally:
        try:
            progress.close()
        except Exception:
            pass
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)






def sync_export_action(entry_id):
    entry = library.find_export(entry_id)
    if not entry:
        notify(tr(30155), icon=xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, '%s\n%s' % (tr(30156), entry.get('name', tr(30131))))
    try:
        result = library.sync_export(get_backend(), entry, abort_callback=progress.iscanceled)
        progress.update(100, tr(30157))
        if setting_bool('auto_update_kodi_library', True) and (result['added'] or result['removed'] or result['updated']):
            library.request_library_update(result['destination'])
        notify(tr(30158, added=result['added'], removed=result['removed'], total=result['total']))
    except RcloneCancelled:
        notify(tr(30159))
    except Exception as exc:
        library.mark_export_error(entry, exc)
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30160, error=exc))
    finally:
        try:
            progress.close()
        except Exception:
            pass
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def export_error_action(entry_id):
    entry = library.find_export(entry_id)
    if not entry or not entry.get('last_error'):
        notify(tr(30161))
    else:
        attempted = int(entry.get('last_attempt') or 0)
        attempted_text = datetime.datetime.fromtimestamp(attempted).strftime('%Y-%m-%d %H:%M:%S') if attempted else tr(30162)
        xbmcgui.Dialog().textviewer(
            '%s · %s' % (entry.get('name', tr(30131)), tr(30163)),
            tr(30164, time=attempted_text, error=entry.get('last_error')),
        )
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)






def sync_all_action():
    entries = library.get_exports()
    if not entries:
        notify(tr(30165))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, tr(30166))
    totals = {'added': 0, 'removed': 0, 'errors': 0, 'files': 0}
    canceled = False
    for index, entry in enumerate(entries):
        if progress.iscanceled():
            canceled = True
            break
        progress.update(int(index * 100 / max(1, len(entries))), entry.get('name', tr(30131)))
        try:
            result = library.sync_export(get_backend(), entry, abort_callback=progress.iscanceled)
            totals['added'] += result['added']
            totals['removed'] += result['removed']
            totals['files'] += result['total']
        except RcloneCancelled:
            canceled = True
            break
        except Exception as exc:
            totals['errors'] += 1
            library.mark_export_error(entry, exc)
    if not canceled:
        progress.update(100, tr(30167))
    progress.close()
    if setting_bool('auto_update_kodi_library', True) and (totals['added'] or totals['removed']):
        library.request_library_update()
    if canceled:
        summary = tr(30168, added=totals['added'], removed=totals['removed'], files=totals['files'], errors=totals['errors'])
    else:
        summary = tr(30169, added=totals['added'], removed=totals['removed'], files=totals['files'], errors=totals['errors'])
    xbmcgui.Dialog().ok(ADDON_NAME, summary)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)






def scan_new_action(remote, path, name):
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, '%s\n%s' % (tr(30170), name or path or remote))
    try:
        result = library.scan_new_content(get_backend(), remote, path, abort_callback=progress.iscanceled)
    except RcloneCancelled:
        notify(tr(30171))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30172, error=exc))
    else:
        if result['first_scan']:
            text_value = tr(30173, total=result['total'])
        else:
            text_value = tr(30174, added=result['added'], removed=result['removed'], total=result['total'])
        xbmcgui.Dialog().ok(ADDON_NAME, text_value)
    finally:
        try:
            progress.close()
        except Exception:
            pass
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)






def search_action(remote, path):
    query = xbmcgui.Dialog().input(tr(30175, location=path or remote), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, '%s\n%s' % (tr(30176), query))
    try:
        items = get_backend().search_videos(remote, path, query, abort_callback=progress.iscanceled)
    except RcloneCancelled:
        notify(tr(30177))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30178, error=exc))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    finally:
        try:
            progress.close()
        except Exception:
            pass
    xbmcplugin.setContent(HANDLE, 'videos')
    set_category(tr(30179, query=query))
    if not items:
        add_action('[%s]' % tr(30180), build_url(action='browse', remote=remote, path=path), 'DefaultVideo.png')
    for item in items:
        add_video_item(remote, item)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)




def refresh_action(remote, path):
    try:
        backend = get_backend()
        backend.invalidate_list_cache(remote, path)
        notify(tr(30181))
    except Exception as exc:
        notify(tr(30182, error=exc), icon=xbmcgui.NOTIFICATION_ERROR)
    xbmc.executebuiltin('Container.Refresh')
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def toggle_favorite_action(remote, path, name):
    added = library.toggle_favorite(remote, path, name)
    notify(tr(30183) if added else tr(30184))
    xbmc.executebuiltin('Container.Refresh')
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def remove_export_action(entry_id, delete_files=False):
    entry = library.find_export(entry_id)
    if not entry:
        notify(tr(30155))
    else:
        if delete_files:
            question = tr(30186, name=entry.get('name', tr(30131)))
        else:
            question = tr(30185, name=entry.get('name', tr(30131)))
        if xbmcgui.Dialog().yesno(ADDON_NAME, question):
            try:
                removed = library.remove_export(entry_id, delete_files=delete_files)
                if removed:
                    library.unregister_kodi_video_source(removed)
            except Exception as exc:
                xbmcgui.Dialog().ok(ADDON_NAME, tr(30187, error=exc))
            else:
                notify(tr(30188))
                xbmc.executebuiltin('Container.Refresh')
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)




def check_rclone_action():
    try:
        path = find_rclone(allow_download=False)
        if not path:
            raise RcloneError(
                tr(30189, arch=detected_android_arch(), version=MIN_RCLONE_VERSION_TEXT, report=get_last_report())
            )
        details = inspect_rclone(path)
        remotes = []
        backend = get_backend(show_dialog=False)
        if backend:
            remotes = backend.list_remotes()
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            tr(
                30193,
                path=path,
                kodi_arch=detected_android_arch(),
                rclone_arch=details.get('arch') or tr(30190),
                version=details.get('version'),
                sha256=details.get('sha256') or tr(30191),
                remotes=', '.join(remotes[:12]) or tr(30192),
            )
        )
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30194, error=exc, version=MIN_RCLONE_VERSION_TEXT))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def import_rclone_action():
    try:
        path = import_manual_rclone()
        if not path:
            raise RcloneError(tr(30195))
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30196, path=path))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30197, error=exc, report=get_last_report()))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def cleanup_storage_action():
    removed = 0
    before = {'total': 0}
    after = {'total': 0}
    try:
        backend = get_backend()
        before = backend.storage_usage()
        removed += backend.cleanup_storage(aggressive=True)
        after = backend.storage_usage()
    except Exception as exc:
        xbmc.log('[Rclone Nexus] Partial temporary cleanup: %s' % exc, xbmc.LOGWARNING)
    try:
        removed += library.cleanup_library_storage()
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            tr(
                30198,
                before=library.format_bytes(before['total']),
                freed=library.format_bytes(removed),
                after=library.format_bytes(after['total']),
            )
        )
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30199, error=exc))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def clear_cache_action():
    try:
        count = get_backend().clear_list_cache()
        notify(tr(30200, count=count))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30201, error=exc))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def stop_rclone_action():
    try:
        count = cleanup_managed_servers(force=True)
        notify(tr(30202, count=count))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, tr(30203, error=exc))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)



def diagnostic_action():
    text = diagnostic_text()
    try:
        backend = get_backend(show_dialog=False)
        if backend:
            usage = backend.storage_usage()
            text += '\n\nTemporary add-on storage:\n  Total: %s\n  VFS: %s\n  Listings: %s\n  Logs: %s' % (
                library.format_bytes(usage['total']), library.format_bytes(usage['vfs_cache']),
                library.format_bytes(usage['list_cache']), library.format_bytes(usage['logs'])
            )
    except Exception:
        pass
    try:
        library_usage = library.library_storage_usage()
        text += '\n\nLocal libraries:\n  Allocated: %s\n  Logical content: %s\n  Files: %d\n  STRM: %s\n  Manifests: %s\n  Discovery: %s\n  Free: %s' % (
            library.format_bytes(library_usage['allocated']),
            library.format_bytes(library_usage['logical']),
            library_usage['files'],
            library.format_bytes(library_usage['strm']['allocated']),
            library.format_bytes(library_usage['manifests']['allocated']),
            library.format_bytes(library_usage['discovery']['allocated']),
            library.format_bytes(library_usage['free']) if library_usage['free'] is not None else 'unknown',
        )
    except Exception as exc:
        text += '\n\nCould not calculate library storage: %s' % exc
    try:
        xbmcgui.Dialog().textviewer('%s · %s' % (ADDON_NAME, tr(30204)), text)
    except Exception:
        xbmcgui.Dialog().ok(ADDON_NAME, text[:4000])
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def main():
    action = get_param('action', 'root')
    if action == 'root':
        show_root()
    elif action == 'browse':
        show_folder(get_param('remote'), get_param('path'))
    elif action == 'play':
        play_item(get_param('remote'), get_param('path'), get_param('name'))
    elif action == 'tools':
        show_tools()
    elif action == 'libraries':
        show_libraries()
    elif action == 'new_content':
        show_new_content()
    elif action == 'favorites':
        show_favorites()
    elif action == 'export_folder':
        export_folder_action(get_param('remote'), get_param('path'), get_param('name'))
    elif action == 'sync_export':
        sync_export_action(get_param('export_id'))
    elif action == 'export_error':
        export_error_action(get_param('export_id'))
    elif action == 'sync_all':
        sync_all_action()
    elif action == 'scan_new':
        scan_new_action(get_param('remote'), get_param('path'), get_param('name'))
    elif action == 'search':
        search_action(get_param('remote'), get_param('path'))
    elif action == 'refresh':
        refresh_action(get_param('remote'), get_param('path'))
    elif action == 'toggle_favorite':
        toggle_favorite_action(get_param('remote'), get_param('path'), get_param('name'))
    elif action == 'remove_export':
        remove_export_action(get_param('export_id'), get_param('delete') == '1')
    elif action == 'clear_new':
        library.clear_recent_items()
        notify(tr(30205))
        xbmc.executebuiltin('Container.Refresh')
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    elif action == 'settings':
        ADDON.openSettings()
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    elif action == 'check_rclone':
        check_rclone_action()
    elif action == 'import_rclone':
        import_rclone_action()
    elif action == 'cleanup_storage':
        cleanup_storage_action()
    elif action == 'clear_cache':
        clear_cache_action()
    elif action == 'stop_rclone':
        stop_rclone_action()
    elif action == 'diagnostic':
        diagnostic_action()
    else:
        xbmc.log('[Rclone Nexus] Unknown action: %s' % action, xbmc.LOGWARNING)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


if __name__ == '__main__':
    main()
