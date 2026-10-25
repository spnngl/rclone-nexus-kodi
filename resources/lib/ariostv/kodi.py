"""Kodi modules, add-on settings and paths shared by every Rclone Nexus entry point.

``addon.py``, ``service.py`` and the library modules read settings and resolve
paths through these helpers so they always agree (for example on where
``rclone.conf`` is found).

Outside Kodi (local syntax checks and tests) ``xbmc``, ``xbmcaddon`` and
``xbmcvfs`` are replaced by minimal stand-ins: every setting is empty, logs go
to stdout and ``special://`` paths map to ``/tmp``.
"""

import os

try:
    import xbmc
    import xbmcaddon
    import xbmcvfs
except ImportError:  # outside Kodi
    class xbmc:
        LOGDEBUG = 0
        LOGINFO = 1
        LOGWARNING = 2
        LOGERROR = 3
        @staticmethod
        def log(msg, level=0):
            print(msg)
        @staticmethod
        def executebuiltin(command):
            print(command)
        @staticmethod
        def executeJSONRPC(payload):
            return '{}'
        @staticmethod
        def getCondVisibility(condition):
            return False
    class _Addon:
        _INFO = {'id': 'plugin.ariostv', 'profile': '/tmp/plugin.ariostv-profile/', 'path': os.getcwd()}
        def getAddonInfo(self, key):
            return self._INFO.get(key, '')
        def getSetting(self, key):
            return ''
    class xbmcaddon:
        Addon = _Addon
    class xbmcvfs:
        @staticmethod
        def translatePath(path):
            return path.replace('special://profile/', '/tmp/kodi-profile/').replace('special://home/', '/tmp/kodi-home/')
        @staticmethod
        def exists(path):
            return os.path.exists(path)
        @staticmethod
        def mkdirs(path):
            os.makedirs(path, exist_ok=True)

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id') or 'plugin.ariostv'
_TRUE_VALUES = ('true', '1', 'yes', 'on', 'si', 'sí')
_CONF_FALLBACKS = (
    'special://masterprofile/rclone.conf',
    'special://profile/rclone.conf',
    '/sdcard/Download/rclone.conf',
    '/storage/emulated/0/Download/rclone.conf',
    '/sdcard/Downloads/rclone.conf',
    '/storage/emulated/0/Downloads/rclone.conf',
)


def setting_text(key, default=''):
    """Return the setting ``key``, or ``default`` when it is empty."""
    value = ADDON.getSetting(key)
    return value if value not in (None, '') else default


def setting_bool(key, default=False):
    """Return the setting ``key`` as a bool; Spanish 'si'/'sí' count as true for hand-edited values."""
    value = ADDON.getSetting(key)
    if value in (None, ''):
        return bool(default)
    return str(value).strip().lower() in _TRUE_VALUES


def setting_int(key, default, minimum=None, maximum=None):
    """Return the setting ``key`` as an int clamped to [minimum, maximum]; ``default`` if empty or invalid."""
    try:
        value = int(setting_text(key, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def translate(path):
    try:
        return xbmcvfs.translatePath(path)
    except Exception:
        return path


def exists(path):
    try:
        return bool(path) and (os.path.exists(path) or xbmcvfs.exists(path))
    except Exception:
        return bool(path) and os.path.exists(path)


def mkdirs(path):
    if not path:
        return
    try:
        xbmcvfs.mkdirs(path)
    except Exception:
        os.makedirs(path, exist_ok=True)


def find_conf_path():
    """Return the configured rclone.conf, else the first one found in the usual places; '' if none exists."""
    configured = ADDON.getSetting('conf_path')
    candidates = [configured] if configured else []
    for path in (translate(candidate) for candidate in candidates + list(_CONF_FALLBACKS)):
        if exists(path):
            return path
    return ''
