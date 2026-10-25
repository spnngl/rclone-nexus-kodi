"""rclone binary backend for Rclone Nexus on Kodi Android.

Design goals:
- Never download rclone from the network.
- Prefer a user-supplied path or a binary bundled in the addon.
- Keep playback storage bounded; disk VFS caching is disabled by default.
- Use small JSON caches only for interactive folder navigation.
"""

import csv
import errno
import gzip
import hashlib
import io
import json
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import english, tr
from .kodi import ADDON, ADDON_ID, exists, mkdirs, setting_bool, setting_int, setting_text, translate, xbmc
ANDROID_API_LEVEL = 21
ADDON_VERSION = ADDON.getAddonInfo('version') or 'dev'
MIN_RCLONE_VERSION = (1, 75, 0)
MIN_RCLONE_VERSION_TEXT = '1.75.0'
SERVER_IDENTITY_HEADER = 'X-Rclone-Nexus-Token'
VIDEO_EXTENSIONS = (
    '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.iso', '.m2ts', '.mpg',
    '.mpeg', '.wmv', '.flv', '.webm', '.vob', '.3gp', '.ogm', '.ogv'
)
_LAST_REPORT = []
_OWNED_PROCESSES = {}
_VERSION_RE = re.compile(r'(?im)^rclone\s+v(\d+)\.(\d+)\.(\d+)(?:[-+][^\s]+)?')
_OS_TYPE_RE = re.compile(r'(?im)^\s*-?\s*os/type:\s*([^\r\n]+)')
_OS_ARCH_RE = re.compile(r'(?im)^\s*-?\s*os/arch:\s*([^\r\n]+)')
_SAFE_EXTRA_SERVE_FLAGS = {
    '--bwlimit': True,
    '--tpslimit': True,
    '--tpslimit-burst': True,
    '--vfs-read-ahead': True,
    '--vfs-read-wait': True,
    '--vfs-write-wait': True,
    '--server-read-timeout': True,
    '--server-write-timeout': True,
    '--max-header-bytes': True,
    '--use-mmap': False,
}


def parse_rclone_version(text):
    match = _VERSION_RE.search(text or '')
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def format_version(version):
    return '.'.join(str(part) for part in (version or ()))


def rclone_version_supported(text_or_version):
    version = text_or_version if isinstance(text_or_version, tuple) else parse_rclone_version(text_or_version)
    return bool(version) and version >= MIN_RCLONE_VERSION


def parse_rclone_os_arch(text):
    """Return (os_type, architecture) from modern or legacy rclone output."""
    value = text or ''
    type_match = _OS_TYPE_RE.search(value)
    arch_match = _OS_ARCH_RE.search(value)
    os_name = type_match.group(1).strip().lower() if type_match else ''
    reported_arch = arch_match.group(1).strip().lower() if arch_match else ''
    # Before rclone 1.55 this could be emitted as "linux/amd64" on one line.
    if '/' in reported_arch:
        legacy_os, reported_arch = reported_arch.split('/', 1)
        os_name = os_name or legacy_os.strip().lower()
    return os_name, reported_arch.strip().lower()


def _rclone_arch_name(value):
    raw = (value or '').strip().lower()
    compact = re.sub(r'[^a-z0-9_]+', '', raw)
    if any(token in compact for token in ('armv8', 'aarch64', 'arm64')):
        return 'armv8a'
    if any(token in compact for token in ('armv7', 'armv6', 'armhf')) or compact == 'arm':
        return 'armv7a'
    if any(token in compact for token in ('x86_64', 'amd64', 'x64')):
        return 'x64'
    if compact in ('386', 'i386', 'i686', 'x86'):
        return 'x86'
    return ''


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _elf_arch(path):
    """Return the Kodi architecture name for an ELF executable, if recognizable."""
    try:
        with open(path, 'rb') as fh:
            header = fh.read(20)
        if len(header) < 20 or header[:4] != b'\x7fELF':
            return ''
        endian = 'little' if header[5] == 1 else 'big'
        machine = int.from_bytes(header[18:20], endian)
        return {3: 'x86', 40: 'armv7a', 62: 'x64', 183: 'armv8a'}.get(machine, '')
    except Exception:
        return ''


def _load_binary_manifest():
    manifest_path = os.path.join(addon_path(), 'resources', 'rclone_builds.json')
    try:
        with open(manifest_path, 'r', encoding='utf-8') as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _manifest_entry_for_path(path):
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(addon_path())).replace(os.sep, '/')
    except Exception:
        return {}
    builds = (_load_binary_manifest().get('builds') or {})
    for entry in builds.values():
        if not isinstance(entry, dict):
            continue
        candidates = [entry.get('addon_path'), entry.get('addon_gzip_path')]
        if relative in [candidate for candidate in candidates if candidate]:
            return entry
    return {}


def _verify_packaged_checksum(path):
    entry = _manifest_entry_for_path(path)
    if not entry:
        return True, ''
    expected = entry.get('sha256_gzip') if path.lower().endswith('.gz') else entry.get('sha256_binary')
    if not expected:
        return True, ''
    actual = _file_sha256(path)
    if actual.lower() != str(expected).lower():
        return False, 'Unexpected SHA-256: %s (expected %s)' % (actual, expected)
    return True, actual



def _validated_extra_serve_args(value):
    """Parse the legacy advanced field using a strict allowlist."""
    if not (value or '').strip():
        return []
    try:
        tokens = shlex.split(value)
    except Exception as exc:
        raise RcloneError(tr(30800, error=exc))
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith('--'):
            raise RcloneError(tr(30801, argument=token))
        flag, equals, inline_value = token.partition('=')
        requires_value = _SAFE_EXTRA_SERVE_FLAGS.get(flag)
        if requires_value is None:
            raise RcloneError(tr(30802, option=flag))
        result.append(flag)
        if requires_value:
            if equals:
                if not inline_value:
                    raise RcloneError(tr(30803, option=flag))
                result.append(inline_value)
            else:
                index += 1
                if index >= len(tokens) or tokens[index].startswith('--'):
                    raise RcloneError(tr(30803, option=flag))
                result.append(tokens[index])
        elif equals:
            normalized = inline_value.strip().lower()
            if normalized not in ('true', '1', 'yes', 'on'):
                raise RcloneError(tr(30804, option=flag))
        index += 1
    return result


def _random_token():
    try:
        return secrets.token_urlsafe(24).rstrip('=')
    except Exception:
        return hashlib.sha256(os.urandom(32)).hexdigest()[:32]


def _atomic_json(path, value):
    mkdirs(os.path.dirname(path))
    tmp = '%s.tmp.%s.%s.%s' % (
        path,
        os.getpid(),
        threading.get_ident(),
        _random_token()[:10],
    )
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            value = json.load(fh)
        return value
    except Exception:
        return {} if default is None else default



def _proc_cmdline_tokens(pid):
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as handle:
            raw = handle.read()
        return [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part]
    except Exception:
        return []




def _pid_alive_global(pid):
    try:
        pid = int(pid)
        os.kill(pid, 0)
        # A zombie still accepts kill(pid, 0), but no longer owns a usable server.
        try:
            with open('/proc/%d/stat' % pid, 'r', encoding='ascii', errors='replace') as fh:
                fields = fh.read().split()
            if len(fields) > 2 and fields[2] == 'Z':
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False



def _pid_owned_by_meta(meta):
    pid = int((meta or {}).get('pid') or 0)
    if not pid or not _pid_alive_global(pid):
        return False
    tokens = _proc_cmdline_tokens(pid)
    if not tokens:
        return False
    expected_binary = str((meta or {}).get('binary') or (meta or {}).get('rclone') or '')
    target = str((meta or {}).get('target') or '')
    baseurl = str((meta or {}).get('baseurl') or '')
    if not expected_binary or not target or not baseurl:
        return False
    try:
        expected_real = os.path.realpath(expected_binary)
        path_match = any(
            token and os.path.isabs(token) and os.path.realpath(token) == expected_real
            for token in tokens
        )
    except Exception:
        path_match = expected_binary in tokens
    return (
        path_match and
        'serve' in tokens and
        'http' in tokens and
        target in tokens and
        baseurl in tokens
    )


def _server_identity_matches(meta, timeout=1.0):
    token = str((meta or {}).get('token') or '')
    port = int((meta or {}).get('port') or 0)
    baseurl = str((meta or {}).get('baseurl') or '').strip('/')
    if not token or not port or not baseurl:
        return False
    url = 'http://127.0.0.1:%d/%s/__rclone_nexus_health__' % (port, baseurl)
    request = urllib.request.Request(url, headers={'User-Agent': 'Rclone-Nexus/%s' % ADDON_VERSION})
    response = None
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        headers = response.headers
        try:
            response.read(1)
        except Exception:
            pass
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    except Exception:
        return False
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
    return str(headers.get(SERVER_IDENTITY_HEADER, '')) == token



def _terminate_meta(meta, grace=2.0):
    pid = int((meta or {}).get('pid') or 0)
    process = _OWNED_PROCESSES.get(pid)
    if not pid or not _pid_alive_global(pid):
        if process is not None:
            try:
                process.poll()
            except Exception:
                pass
            _OWNED_PROCESSES.pop(pid, None)
        return False
    # A valid HTTP token identifies the server on the port, but it does not prove
    # that the PID in an old metadata file still belongs to that server. Only kill
    # a process we still own directly or whose exact command line matches.
    process_owned = process is not None and process.poll() is None
    if not (process_owned or _pid_owned_by_meta(meta)):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return False

    if process is not None:
        try:
            process.wait(timeout=max(0.2, float(grace)))
        except TypeError:
            process.wait()
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=1)
            except Exception:
                pass
        finally:
            _OWNED_PROCESSES.pop(pid, None)
        return process.poll() is not None

    deadline = time.time() + max(0.2, float(grace))
    while time.time() < deadline:
        if not _pid_alive_global(pid):
            return True
        time.sleep(0.05)
    if _pid_owned_by_meta(meta):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return not _pid_alive_global(pid)



def managed_server_dir():
    path = os.path.join(addon_profile(), 'servers')
    mkdirs(path)
    return path



def cleanup_managed_servers(max_idle_seconds=None, force=False):
    """Stop stale/idle owned rclone servers without trusting a recycled PID."""
    folder = managed_server_dir()
    stopped = 0
    now = time.time()
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if not name.endswith('.json'):
            continue
        path = os.path.join(folder, name)
        meta = _read_json(path, {})
        if not isinstance(meta, dict):
            try:
                os.remove(path)
            except Exception:
                pass
            continue
        pid = int(meta.get('pid') or 0)
        alive = bool(pid and _pid_alive_global(pid))
        idle = now - float(meta.get('last_used') or meta.get('started') or 0)
        should_stop = force or (
            max_idle_seconds is not None and max_idle_seconds > 0 and idle >= max_idle_seconds
        )
        if alive and should_stop:
            if _terminate_meta(meta):
                stopped += 1
                alive = False
            else:
                _report(
                    'Did not remove the record for PID %s because it could not be verified as an owned server.' % pid,
                    xbmc.LOGWARNING,
                )
                continue
        if not alive:
            try:
                os.remove(path)
            except Exception:
                pass
    return stopped



def _report(msg, level=xbmc.LOGINFO):
    text = str(msg)
    _LAST_REPORT.append(text)
    if len(_LAST_REPORT) > 120:
        del _LAST_REPORT[:-120]
    try:
        xbmc.log('[Rclone Nexus/rclone] ' + text, level)
    except Exception:
        pass


def get_last_report():
    return '\n'.join(_LAST_REPORT[-100:])


def addon_profile():
    path = translate(ADDON.getAddonInfo('profile') or ('special://profile/addon_data/%s/' % ADDON_ID))
    mkdirs(path)
    return path


def addon_path():
    return translate(ADDON.getAddonInfo('path') or '')


def _run_getprop(prop):
    getprop = '/system/bin/getprop'
    if not os.path.exists(getprop):
        return ''
    try:
        proc = subprocess.Popen([getprop, prop], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, _ = proc.communicate(timeout=3)
        except TypeError:
            out, _ = proc.communicate()
    except Exception:
        return ''
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    return (out or '').strip()


def android_props():
    return {
        'sdk': _run_getprop('ro.build.version.sdk'),
        'release': _run_getprop('ro.build.version.release'),
        'model': _run_getprop('ro.product.model'),
        'device': _run_getprop('ro.product.device'),
        'manufacturer': _run_getprop('ro.product.manufacturer'),
        'cpu_abi': _run_getprop('ro.product.cpu.abi'),
        'cpu_abilist': _run_getprop('ro.product.cpu.abilist'),
    }


def _arch_from_abi_text(text):
    # ABI lists are ordered by preference. Parse each entry independently so a
    # value such as "arm64-v8a,armeabi-v7a" is not mistaken for 32-bit ARM.
    values = [item for item in re.split(r'[,;\s]+', (text or '').lower()) if item]
    for abi in values:
        if abi in ('arm64-v8a', 'aarch64', 'arm64') or 'arm64' in abi:
            return 'armv8a'
        if abi in ('armeabi-v7a', 'armv7', 'armv7l', 'arm') or 'armv7' in abi:
            return 'armv7a'
        if abi in ('x86_64', 'amd64', 'x64'):
            return 'x64'
        if abi in ('x86', 'i686', 'i386', '386'):
            return 'x86'
    return ''


def _apply_process_bitness(arch):
    """Downgrade a device ABI when the Kodi process itself is 32-bit."""
    try:
        bits = struct.calcsize('P') * 8
    except Exception:
        bits = 0
    if bits <= 32:
        if arch == 'armv8a':
            return 'armv7a'
        if arch == 'x64':
            return 'x86'
    return arch


def detected_android_arch():
    forced = (setting_text('rclone_arch', 'auto') or 'auto').strip().lower()
    aliases = {
        'auto': '', 'automatico': '', 'automático': '',
        'armv7': 'armv7a', 'armeabi-v7a': 'armv7a', '32': 'armv7a', '32bit': 'armv7a',
        'armv8': 'armv8a', 'arm64': 'armv8a', 'arm64-v8a': 'armv8a', 'aarch64': 'armv8a',
        '64': 'armv8a', '64bit': 'armv8a', 'x86_64': 'x64', 'amd64': 'x64',
    }
    forced = aliases.get(forced, forced)
    if forced in ('armv7a', 'armv8a', 'x86', 'x64'):
        return forced

    # /proc/self/exe describes the process that embeds Python/Kodi and is more
    # reliable than the physical device ABI on 64-bit devices running 32-bit Kodi.
    process_arch = _elf_arch('/proc/self/exe')
    if process_arch:
        return process_arch

    props = android_props()
    primary = _apply_process_bitness(_arch_from_abi_text(props.get('cpu_abi') or ''))
    if primary:
        return primary
    listed = _apply_process_bitness(_arch_from_abi_text(props.get('cpu_abilist') or ''))
    if listed:
        return listed
    machine = (platform.machine() or '').lower()
    arch = _apply_process_bitness(_arch_from_abi_text(machine))
    if arch:
        return arch
    try:
        return 'armv8a' if struct.calcsize('P') * 8 >= 64 else 'armv7a'
    except Exception:
        return 'armv7a'


def _is_android_like():
    lower = (sys.platform + ' ' + platform.platform()).lower()
    return 'android' in lower or os.path.exists('/system/bin/getprop') or os.path.exists('/system/build.prop')



def android_package_name():
    try:
        with open('/proc/self/cmdline', 'rb') as handle:
            raw = handle.read().split(b'\0')[0].decode('utf-8', 'replace')
        name = raw.split(':', 1)[0].strip()
        if '.' in name:
            return name
    except Exception:
        pass
    return ''



def _can_write_dir(path):
    try:
        mkdirs(path)
        test = os.path.join(path, '.rclone_nexus_write_test')
        with open(test, 'wb') as fh:
            fh.write(b'x')
        os.remove(test)
        return True
    except Exception:
        return False


def executable_base_dir():
    custom = setting_text('rclone_install_dir')
    candidates = []
    if custom:
        candidates.append(translate(custom))
    pkg = android_package_name()
    if pkg:
        candidates.extend([
            '/data/user/0/%s/files/rclone-nexus' % pkg,
            '/data/data/%s/files/rclone-nexus' % pkg,
        ])
    candidates.append(os.path.join(addon_profile(), 'exec'))
    for path in candidates:
        if _can_write_dir(path):
            return path
    return os.path.join(addon_profile(), 'exec')


def rclone_managed_path(arch=None):
    arch = arch or detected_android_arch()
    return os.path.join(executable_base_dir(), 'bin', 'android-%s' % arch, 'rclone')


def rclone_legacy_managed_paths():
    return [
        os.path.join(executable_base_dir(), 'bin', 'rclone'),
        os.path.join(addon_profile(), 'bin', 'rclone'),
    ]


def _abi_dirs(arch):
    if arch == 'armv7a':
        return ['armeabi-v7a', 'armv7a', 'arm']
    if arch == 'armv8a':
        return ['arm64-v8a', 'armv8a', 'arm64']
    if arch == 'x64':
        return ['x86_64', 'x64']
    return ['x86']


def _candidate_paths():
    arch = detected_android_arch()
    candidates = []
    setting = setting_text('rclone_path')
    if setting:
        candidates.append(translate(setting))
    candidates.append(rclone_managed_path(arch))
    candidates.extend(rclone_legacy_managed_paths())
    base = addon_path()
    for directory in _abi_dirs(arch):
        candidates.extend([
            os.path.join(base, 'resources', 'bin', 'android', directory, 'rclone'),
            os.path.join(base, 'resources', 'bin', 'android', directory, 'rclone.gz'),
        ])
    candidates.extend([
        '/data/data/com.termux/files/usr/bin/rclone',
        '/system/bin/rclone',
        '/system/xbin/rclone',
    ])
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        if folder:
            candidates.append(os.path.join(folder, 'rclone'))
    seen, result = set(), []
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _manual_import_candidates():
    arch = detected_android_arch()
    names = [
        'rclone-android-%d-%s.gz' % (ANDROID_API_LEVEL, arch),
        'rclone-android-%d-%s' % (ANDROID_API_LEVEL, arch),
        'rclone.gz', 'rclone', 'rclone.bin',
    ]
    roots = ['/sdcard/Download', '/storage/emulated/0/Download', '/sdcard/Downloads', '/storage/emulated/0/Downloads']
    return [os.path.join(root, name) for root in roots for name in names]


def _looks_gzip(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read(2) == b'\x1f\x8b'
    except Exception:
        return False


def _make_executable(path):
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception as exc:
        _report('Could not apply chmod to %s: %s' % (path, english(exc)), xbmc.LOGWARNING)



def _install_binary_from_file(src_path, gzipped=None):
    if not exists(src_path):
        raise RuntimeError('Does not exist: %s' % src_path)
    checksum_ok, checksum_detail = _verify_packaged_checksum(src_path)
    if not checksum_ok:
        raise RuntimeError('The bundled binary failed verification: %s' % checksum_detail)
    manifest_entry = _manifest_entry_for_path(src_path)
    expected_binary_hash = str((manifest_entry or {}).get('sha256_binary') or '').strip().lower()
    dest = rclone_managed_path()
    tmp = dest + '.tmp'
    mkdirs(os.path.dirname(dest))
    if gzipped is None:
        gzipped = src_path.endswith('.gz') or _looks_gzip(src_path)
    _report('Copying local rclone %s -> %s (gz=%s)' % (src_path, dest, gzipped), xbmc.LOGINFO)
    try:
        if gzipped:
            with gzip.open(src_path, 'rb') as src, open(tmp, 'wb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except Exception:
                    pass
        else:
            with open(src_path, 'rb') as src, open(tmp, 'wb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except Exception:
                    pass
        _make_executable(tmp)
        binary_hash = _file_sha256(tmp)
        if expected_binary_hash and binary_hash.lower() != expected_binary_hash:
            raise RuntimeError(
                'The decompressed executable failed SHA-256 verification: %s (expected %s)'
                % (binary_hash, expected_binary_hash)
            )
        os.replace(tmp, dest)
        _make_executable(dest)
        _atomic_json(dest + '.json', {
            'source': src_path,
            'source_sha256': checksum_detail or _file_sha256(src_path),
            'binary_sha256': binary_hash,
            'installed_at': int(time.time()),
            'arch': detected_android_arch(),
        })
        return dest
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass



def _run_raw(args, timeout=30):
    env = os.environ.copy()
    base = addon_profile()
    env.setdefault('HOME', base)
    env.setdefault('TMPDIR', os.path.join(base, 'tmp'))
    mkdirs(env['TMPDIR'])
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except TypeError:
        out, err = proc.communicate()
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        proc.communicate()
        raise RuntimeError(tr(30805, command=args[0]))
    out_s = out.decode('utf-8', 'replace') if isinstance(out, bytes) else (out or '')
    err_s = err.decode('utf-8', 'replace') if isinstance(err, bytes) else (err or '')
    return proc.returncode, out_s, err_s



def _try_exec(path):
    _make_executable(path)
    expected_arch = detected_android_arch()
    elf_arch = _elf_arch(path)
    if _is_android_like() and elf_arch and elf_arch != expected_arch:
        return False, tr(30806, elf_arch=elf_arch, expected_arch=expected_arch)
    code, out, err = _run_raw([path, 'version'], timeout=15)
    text = (out + '\n' + err).strip()
    if code != 0 or 'rclone' not in text.lower():
        return False, text or tr(30807, code=code)
    version = parse_rclone_version(text)
    if not version:
        return False, tr(30808, output=text)
    if version < MIN_RCLONE_VERSION:
        return False, tr(30809, version=format_version(version), minimum=MIN_RCLONE_VERSION_TEXT)
    _os_name, reported_arch = parse_rclone_os_arch(text)
    normalized = _rclone_arch_name(reported_arch)
    if _is_android_like() and normalized and normalized != expected_arch:
        return False, tr(30810, reported_arch=normalized, expected_arch=expected_arch)
    return True, text



def inspect_rclone(path):
    ok, detail = _try_exec(path)
    if not ok:
        raise RcloneError(detail)
    version = parse_rclone_version(detail)
    os_name, reported_arch = parse_rclone_os_arch(detail)
    result = {
        'path': path,
        'version': format_version(version),
        'version_tuple': version,
        'os': os_name,
        'reported_arch': reported_arch,
        'arch': _rclone_arch_name(reported_arch) or _elf_arch(path),
        'elf_arch': _elf_arch(path),
        'sha256': '',
        'raw': detail,
    }
    try:
        result['sha256'] = _file_sha256(path)
    except Exception:
        pass
    return result


def _needs_internal_copy(path):
    try:
        absolute = os.path.abspath(path)
        addon_absolute = os.path.abspath(addon_path())
        if absolute.startswith(addon_absolute + os.sep):
            return True
    except Exception:
        pass
    low = (path or '').lower()
    return low.startswith('/sdcard/') or low.startswith('/storage/emulated/') or low.endswith('.gz')


def _prepare_candidate(path):
    if _needs_internal_copy(path):
        return _install_binary_from_file(path, gzipped=None)
    return path


def import_manual_rclone():
    for src in _manual_import_candidates():
        if not exists(src):
            continue
        try:
            path = _install_binary_from_file(src, gzipped=None)
            ok, text = _try_exec(path)
            if ok:
                _report('rclone imported successfully from %s' % src, xbmc.LOGINFO)
                return path
            _report('Imported but not executable: %s :: %s' % (src, english(text)[:800]), xbmc.LOGWARNING)
        except Exception as exc:
            _report('Import failed for %s: %s' % (src, english(exc)), xbmc.LOGWARNING)
    return ''


def embedded_binary_locations():
    return [
        'resources/bin/android/armeabi-v7a/rclone',
        'resources/bin/android/arm64-v8a/rclone',
        'resources/bin/android/x86/rclone',
        'resources/bin/android/x86_64/rclone',
    ]



def find_rclone(allow_download=False):
    # allow_download is retained for API compatibility; network downloads are disabled.
    del _LAST_REPORT[:]
    props = android_props()
    _report('Searching for local rclone >= %s. AndroidLike=%s arch=%s sdk=%s model=%s abi=%s abilist=%s package=%s' % (
        MIN_RCLONE_VERSION_TEXT, _is_android_like(), detected_android_arch(), props.get('sdk'), props.get('model'),
        props.get('cpu_abi'), props.get('cpu_abilist'), android_package_name()
    ), xbmc.LOGINFO)
    for path in _candidate_paths():
        if not exists(path):
            continue
        try:
            run_path = _prepare_candidate(path)
            ok, detail = _try_exec(run_path)
            if ok:
                _report('rclone OK: %s (%s)' % (run_path, detail.splitlines()[0]), xbmc.LOGINFO)
                return run_path
            _report('Invalid candidate: %s :: %s' % (run_path, english(detail)[:1000]), xbmc.LOGWARNING)
        except Exception as exc:
            _report('Candidate is not executable %s: %s' % (path, english(exc)), xbmc.LOGWARNING)
    return ''



class RcloneError(RuntimeError):
    pass


class RcloneCancelled(RcloneError):
    """Raised when Kodi/user cancellation stops an rclone operation safely."""


def _iter_files(folder):
    if not os.path.isdir(folder):
        return
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            try:
                stat_result = os.stat(path)
                yield path, stat_result.st_size, stat_result.st_mtime
            except Exception:
                continue


def _remove_empty_dirs(folder):
    if not os.path.isdir(folder):
        return
    for root, dirs, _files in os.walk(folder, topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except Exception:
                pass


def _trim_file(path, max_bytes):
    try:
        size = os.path.getsize(path)
        if size <= max_bytes:
            return 0
        with open(path, 'rb') as fh:
            fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
        with open(path, 'wb') as fh:
            fh.write(data)
        return size - len(data)
    except Exception:
        return 0


def _cap_directory(folder, max_bytes, max_age_seconds=0):
    files = list(_iter_files(folder) or [])
    removed_bytes = 0
    now = time.time()
    kept = []
    for path, size, mtime in files:
        if max_age_seconds and now - mtime > max_age_seconds:
            try:
                os.remove(path)
                removed_bytes += size
                continue
            except Exception:
                pass
        kept.append((path, size, mtime))
    total = sum(x[1] for x in kept)
    if total > max_bytes:
        for path, size, _mtime in sorted(kept, key=lambda x: x[2]):
            if total <= max_bytes:
                break
            try:
                os.remove(path)
                total -= size
                removed_bytes += size
            except Exception:
                pass
    _remove_empty_dirs(folder)
    return removed_bytes


class RcloneBackend:
    def __init__(self, conf_path):
        self.conf_path = conf_path
        self.profile = addon_profile()
        self.cache_dir = os.path.join(self.profile, 'list-cache')
        self.pid_dir = os.path.join(self.profile, 'servers')
        self.rclone_cache_dir = os.path.join(self.profile, 'rclone-vfs-cache')
        self.tmp_dir = os.path.join(self.profile, 'tmp')
        self._scan_low_impact = False
        for folder in (self.cache_dir, self.pid_dir, self.rclone_cache_dir, self.tmp_dir):
            mkdirs(folder)
        self.cleanup_storage(automatic=True)
        self.rclone = find_rclone(allow_download=False)
        if not self.rclone:
            locations = '\n'.join('  - ' + x for x in embedded_binary_locations())
            raise RcloneError(tr(
                30811, minimum=MIN_RCLONE_VERSION_TEXT, report=get_last_report(), locations=locations
            ))
        self.rclone = os.path.abspath(self.rclone)
        self._version_text = ''
        self._version_tuple = ()
        try:
            self._version_text = self.version()
            self._version_tuple = parse_rclone_version(self._version_text)
        except Exception:
            pass


    def base_args(self, cache_dir=None):
        return [
            self.rclone,
            '--config', self.conf_path,
            '--cache-dir', cache_dir or self.rclone_cache_dir,
            '--retries', '2',
            '--low-level-retries', '5',
            '--contimeout', '15s',
            '--timeout', '1m',
        ]


    def set_scan_low_impact(self, enabled):
        """Limit recursive scan pressure while Kodi is actively playing video."""
        self._scan_low_impact = bool(enabled)


    def run(self, args, timeout=60):
        cmd = self.base_args() + args
        _report('run: %s' % ' '.join(cmd[:12] + (['...'] if len(cmd) > 12 else [])), xbmc.LOGDEBUG)
        code, out, err = _run_raw(cmd, timeout=timeout)
        if code != 0:
            raise RcloneError((err or out or tr(30812, code=code)).strip())
        return out

    def version(self):
        code, out, err = _run_raw([self.rclone, 'version'], timeout=15)
        if code != 0:
            raise RcloneError((err or out).strip())
        value = (out or err).strip()
        if not rclone_version_supported(value):
            found = format_version(parse_rclone_version(value)) or 'unknown'
            raise RcloneError('Unsupported rclone version: %s; minimum %s.' % (found, MIN_RCLONE_VERSION_TEXT))
        return value



    def backend_features(self, remote, path=''):
        """Return rclone backend feature metadata for a remote using a short-lived command."""
        target = self.remote_target(remote, path)
        out = self.run(['backend', 'features', target], timeout=45)
        try:
            value = json.loads(out or '{}')
        except Exception as exc:
            raise RcloneError('backend features returned invalid JSON: %s' % exc)
        return value if isinstance(value, dict) else {}

    def supports_change_notify(self, remote, path=''):
        try:
            features = self.backend_features(remote, path).get('Features') or {}
            return bool(features.get('ChangeNotify'))
        except Exception as exc:
            _report('Could not query ChangeNotify for %s: %s' % (remote, english(exc)), xbmc.LOGWARNING)
            return False

    def change_notify_command(self, remote, poll_interval='5m'):
        """Build a no-disk-log ChangeNotify watcher command."""
        return self.base_args() + [
            'test', 'changenotify', self.remote_target(remote),
            '--poll-interval', str(poll_interval or '5m'),
            '--log-level', 'NOTICE',
            '--log-format', '',
        ]

    def list_remotes(self):
        out = self.run(['listremotes'], timeout=45)
        result = []
        for line in out.splitlines():
            name = line.strip().rstrip(':')
            if name:
                result.append(name)
        return result

    def _cache_key(self, remote, path):
        try:
            mtime = str(os.path.getmtime(self.conf_path))
        except Exception:
            mtime = '0'
        raw = (self.conf_path + '\0' + mtime + '\0' + remote + '\0' + (path or '')).encode('utf-8', 'replace')
        return hashlib.sha1(raw).hexdigest() + '.json'

    def _cache_path(self, remote, path):
        return os.path.join(self.cache_dir, self._cache_key(remote, path))

    def clear_list_cache(self):
        count = 0
        if os.path.isdir(self.cache_dir):
            for name in os.listdir(self.cache_dir):
                if not name.endswith('.json'):
                    continue
                try:
                    os.remove(os.path.join(self.cache_dir, name))
                    count += 1
                except Exception:
                    pass
        return count

    def invalidate_list_cache(self, remote, path=''):
        cache_path = self._cache_path(remote, path)
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
                return 1
        except Exception:
            pass
        return 0

    @staticmethod
    def join_path(parent, child):
        parent = (parent or '').strip('/')
        child = (child or '').strip('/')
        return parent + '/' + child if parent and child else (parent or child)

    @staticmethod
    def remote_target(remote, path=''):
        remote = (remote or '').rstrip(':')
        path = (path or '').strip('/')
        return '%s:%s' % (remote, path) if path else '%s:' % remote

    def list_folder(self, remote, path=''):
        cache_minutes = setting_int('list_cache_minutes', 30, minimum=0, maximum=1440)
        cache_path = self._cache_path(remote, path)
        if cache_minutes > 0 and exists(cache_path):
            try:
                age = time.time() - os.path.getmtime(cache_path)
                if age < cache_minutes * 60:
                    with open(cache_path, 'r', encoding='utf-8') as fh:
                        return json.load(fh)
            except Exception:
                pass
        args = ['lsjson', self.remote_target(remote, path), '--no-mimetype']
        if setting_bool('skip_modtime', True):
            args.append('--no-modtime')
        out = self.run(args, timeout=setting_int('list_timeout', 120, minimum=30, maximum=900))
        try:
            raw_items = json.loads(out or '[]')
        except Exception as exc:
            raise RcloneError(tr(30813, error=exc))
        items = []
        for item in raw_items:
            raw_path = item.get('Path') or item.get('Name') or ''
            name = item.get('Name') or os.path.basename(raw_path.rstrip('/')) or raw_path
            if not name:
                continue
            items.append({
                'name': name,
                'path': self.join_path(path, name),
                'is_folder': bool(item.get('IsDir')),
                'size': int(item.get('Size', 0) or 0),
                'modified': item.get('ModTime', '') or '',
            })
        items.sort(key=lambda x: (not x['is_folder'], x['name'].lower()))
        if cache_minutes > 0:
            try:
                _atomic_json(cache_path, items)
            except Exception as exc:
                _report('Could not write listing cache: %s' % english(exc), xbmc.LOGWARNING)
        return items

    def _stream_env(self, low_impact=False):
        env = os.environ.copy()
        env.setdefault('HOME', self.profile)
        env.setdefault('TMPDIR', self.tmp_dir)
        if low_impact:
            # This affects only the child rclone scanner, never Kodi or playback.
            env['GOMAXPROCS'] = '1'
        mkdirs(env['TMPDIR'])
        return env

    def iter_recursive(self, remote, path='', video_only=False, abort_callback=None):
        """Yield recursive file records from rclone lsf without buffering all JSON in RAM."""
        args = [
            'lsf', self.remote_target(remote, path), '--recursive', '--files-only',
            '--csv', '--format', 'pstio', '--time-format', 'RFC3339',
        ]
        if setting_bool('library_fast_list', False):
            args.append('--fast-list')
        else:
            args.extend(['--disable', 'ListR'])
        if self._scan_low_impact:
            # Metadata scans do not transfer video bytes. Limiting provider calls
            # and rclone workers is more useful than a bandwidth cap here.
            args.extend(['--checkers', '1', '--tpslimit', '2', '--tpslimit-burst', '1'])
            _report('Scan during playback: low-impact profile active (1 CPU, 1 checker, 2 calls/s).', xbmc.LOGINFO)
        if video_only:
            args.append('--ignore-case')
            for extension in VIDEO_EXTENSIONS:
                args.extend(['--include', '**' + extension])
        cmd = self.base_args() + args
        _report('stream: %s' % ' '.join(cmd[:16] + (['...'] if len(cmd) > 16 else [])), xbmc.LOGDEBUG)
        error_fd, error_path = tempfile.mkstemp(prefix='rclone-nexus-lsf-', suffix='.log', dir=self.tmp_dir)
        error_file = os.fdopen(error_fd, 'wb')
        proc = None
        events = queue.Queue(maxsize=256)
        sentinel = object()
        stop_event = threading.Event()

        def enqueue(value):
            while not stop_event.is_set():
                try:
                    events.put(value, timeout=0.2)
                    return
                except queue.Full:
                    continue

        def reader_worker():
            try:
                with io.TextIOWrapper(proc.stdout, encoding='utf-8', errors='replace', newline='') as wrapper:
                    for row in csv.reader(wrapper):
                        enqueue(('row', row))
            except Exception as exc:
                if not stop_event.is_set():
                    enqueue(('error', exc))
            finally:
                enqueue(('end', sentinel))

        timeout = setting_int('library_scan_timeout', 900, minimum=60, maximum=7200)
        if self._scan_low_impact:
            # Throttled provider calls need more wall-clock time, not more resources.
            timeout = min(7200, timeout * 2)
        deadline = time.time() + timeout
        thread = None
        completed = False
        cancelled = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=error_file,
                env=self._stream_env(low_impact=self._scan_low_impact),
                close_fds=True,
            )
            error_file.close()
            thread = threading.Thread(target=reader_worker, name='RcloneNexusLsfReader')
            thread.daemon = True
            thread.start()
            while True:
                if abort_callback and abort_callback():
                    cancelled = True
                    raise RcloneCancelled(tr(30814))
                if time.time() >= deadline:
                    raise RcloneError(tr(30815, seconds=timeout))
                try:
                    kind, value = events.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None and thread is not None and not thread.is_alive():
                        break
                    continue
                if kind == 'error':
                    raise RcloneError(tr(30816, error=value))
                if kind == 'end':
                    break
                row = value
                if not row:
                    continue
                while len(row) < 5:
                    row.append('')
                raw_path, raw_size, modified, item_id, original_id = row[:5]
                raw_path = (raw_path or '').strip('/')
                full_path = self.join_path(path, raw_path)
                if not full_path:
                    continue
                if video_only and os.path.splitext(full_path.lower())[1] not in VIDEO_EXTENSIONS:
                    continue
                try:
                    size = int(raw_size or 0)
                except Exception:
                    size = 0
                yield {
                    'name': os.path.basename(full_path.rstrip('/')) or raw_path,
                    'path': full_path,
                    'is_folder': False,
                    'size': size,
                    'modified': modified or '',
                    'id': item_id or '',
                    'orig_id': original_id or '',
                }
            try:
                code = proc.wait(timeout=5)
            except TypeError:
                code = proc.wait()
            except subprocess.TimeoutExpired:
                code = None
            completed = code == 0
            if code not in (0, None):
                raise RcloneError(self._read_error_tail(error_path) or tr(30817, code=code))
            if code is None:
                raise RcloneError(tr(30818))
        finally:
            stop_event.set()
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
            if thread is not None:
                thread.join(timeout=1)
            try:
                if not error_file.closed:
                    error_file.close()
            except Exception:
                pass
            try:
                os.remove(error_path)
            except Exception:
                pass
            if not completed and cancelled:
                _report('Scan cancelled without replacing the manifest.', xbmc.LOGINFO)


    @staticmethod
    def _read_error_tail(path, limit=12000):
        try:
            with open(path, 'rb') as fh:
                data = fh.read()[-limit:]
            return data.decode('utf-8', 'replace').strip()
        except Exception:
            return ''

    def list_recursive(self, remote, path='', video_only=False, abort_callback=None):
        return list(self.iter_recursive(remote, path, video_only=video_only, abort_callback=abort_callback))


    def search_videos(self, remote, path, query, limit=300, abort_callback=None):
        query = (query or '').strip().lower()
        if not query:
            return []
        results = []
        iterator = self.iter_recursive(remote, path, video_only=True, abort_callback=abort_callback)
        try:
            for item in iterator:
                if query in item.get('name', '').lower() or query in item.get('path', '').lower():
                    results.append(item)
                    if len(results) >= limit:
                        break
        finally:
            try:
                iterator.close()
            except Exception:
                pass
        return results


    def _server_id(self, remote):
        raw = (self.conf_path + '\0' + remote).encode('utf-8', 'replace')
        return hashlib.sha1(raw).hexdigest()[:12]

    def _pid_file(self, remote):
        return os.path.join(self.pid_dir, self._server_id(remote) + '.json')

    def _server_start_lock_file(self):
        # A single lock serializes all server starts. This closes the small race
        # where two Kodi plugin invocations for different remotes could both see
        # the same port as free before either rclone process binds it.
        return os.path.join(self.pid_dir, 'server-start.lock')

    def _acquire_server_start_lock(self, timeout):
        lock_path = self._server_start_lock_file()
        deadline = time.time() + max(2.0, float(timeout))
        stale_after = max(180.0, float(timeout) * 3.0)
        while time.time() < deadline:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, ('%s\n%s\n' % (os.getpid(), time.time())).encode('ascii', 'replace'))
                finally:
                    os.close(fd)
                return lock_path
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise RcloneError(tr(30819, error=exc))
                try:
                    age = time.time() - os.path.getmtime(lock_path)
                    lock_pid = 0
                    with open(lock_path, 'r', encoding='ascii', errors='replace') as fh:
                        first_line = fh.readline().strip()
                    if first_line:
                        lock_pid = int(first_line)
                    if age > stale_after or (lock_pid and not _pid_alive_global(lock_pid)):
                        os.remove(lock_path)
                        continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
                time.sleep(0.15)
        raise RcloneError(tr(30820))

    @staticmethod
    def _release_server_start_lock(lock_path):
        try:
            if lock_path and os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass

    def _port_for_remote(self, remote):
        start = setting_int('serve_port_start', 28980, minimum=1024, maximum=64000)
        hashed = int(hashlib.sha1((self.conf_path + '\0' + remote).encode('utf-8', 'replace')).hexdigest()[:6], 16)
        return start + (hashed % 800)


    def _available_port(self, remote):
        first = self._port_for_remote(remote)
        for offset in range(96):
            port = 1024 + ((first - 1024 + offset) % (65535 - 1024))
            if not self._is_port_open(port):
                return port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()


    @staticmethod
    def _is_port_open(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            return sock.connect_ex(('127.0.0.1', int(port))) == 0
        finally:
            sock.close()

    @staticmethod
    def _pid_alive(pid):
        return _pid_alive_global(pid)


    def _server_logs_tail(self, remote, n=3000):
        sid = self._server_id(remote)
        parts = []
        for suffix in ('.err.log', '.out.log'):
            path = os.path.join(self.pid_dir, sid + suffix)
            try:
                if exists(path):
                    with open(path, 'rb') as fh:
                        parts.append(fh.read()[-n:].decode('utf-8', 'replace'))
            except Exception:
                pass
        return '\n'.join(x for x in parts if x)

    def _serve_flags(self, addr, token, baseurl):
        mode = (setting_text('vfs_cache_mode', 'off') or 'off').strip().lower()
        if mode not in ('off', 'minimal', 'writes', 'full'):
            mode = 'off'
        cache_mb = setting_int('vfs_cache_max_size_mb', 256, minimum=64, maximum=4096)
        buffer_mb = setting_int('buffer_size_mb', 8, minimum=0, maximum=128)
        chunk_mb = setting_int('read_chunk_size_mb', 16, minimum=1, maximum=256)
        chunk_limit_mb = max(chunk_mb, setting_int('read_chunk_limit_mb', 128, minimum=16, maximum=2048))
        chunk_streams = setting_int('read_chunk_streams', 0, minimum=0, maximum=16)
        flags = [
            '--read-only',
            '--addr', addr,
            '--baseurl', baseurl,
            '--response-header', '%s: %s' % (SERVER_IDENTITY_HEADER, token),
            '--disable-dir-list',
            '--disable-zip',
            '--max-header-bytes', '16384',
            '--server-read-timeout', '24h',
            '--server-write-timeout', '24h',
            '--vfs-cache-mode', mode,
            '--vfs-cache-max-size', '%dM' % cache_mb,
            '--vfs-cache-max-age', '1h',
            '--vfs-cache-poll-interval', '1m',
            '--vfs-read-chunk-size', '%dM' % chunk_mb,
            '--vfs-read-chunk-size-limit', '%dM' % chunk_limit_mb,
            '--buffer-size', '%dM' % buffer_mb,
            '--dir-cache-time', '10m',
            '--poll-interval', '0',
            '--stats', '0',
        ]
        if chunk_streams > 0:
            flags.extend(['--vfs-read-chunk-streams', str(chunk_streams)])
        flags.extend(_validated_extra_serve_args(setting_text('rclone_serve_extra_args', '')))
        return flags


    def _configuration_fingerprint(self, remote):
        try:
            conf_stat = os.stat(self.conf_path)
            conf_value = {
                'path': os.path.abspath(self.conf_path),
                'size': conf_stat.st_size,
                'mtime': conf_stat.st_mtime_ns if hasattr(conf_stat, 'st_mtime_ns') else conf_stat.st_mtime,
                # rclone.conf is normally small. Hashing it avoids reusing a
                # server when content changes within a coarse filesystem mtime.
                'sha256': _file_sha256(self.conf_path),
            }
        except Exception:
            conf_value = self.conf_path
        try:
            binary_stat = os.stat(self.rclone)
            binary_value = {
                'path': os.path.abspath(self.rclone),
                'size': binary_stat.st_size,
                'mtime': binary_stat.st_mtime_ns if hasattr(binary_stat, 'st_mtime_ns') else binary_stat.st_mtime,
            }
        except Exception:
            binary_value = self.rclone
        profile = {
            'addon': ADDON_VERSION,
            'remote': remote,
            'conf': conf_value,
            'binary': binary_value,
            'version': format_version(self._version_tuple),
            'mode': setting_text('vfs_cache_mode', 'off'),
            'cache_mb': setting_text('vfs_cache_max_size_mb', '256'),
            'buffer_mb': setting_text('buffer_size_mb', '8'),
            'chunk_mb': setting_text('read_chunk_size_mb', '16'),
            'chunk_limit_mb': setting_text('read_chunk_limit_mb', '128'),
            'chunk_streams': setting_text('read_chunk_streams', '0'),
            'extra': setting_text('rclone_serve_extra_args', ''),
        }
        raw = json.dumps(profile, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()


    def _load_server_meta(self, remote):
        value = _read_json(self._pid_file(remote), {})
        return value if isinstance(value, dict) else {}


    def _save_server_meta(self, remote, meta):
        _atomic_json(self._pid_file(remote), meta)


    def _stop_server_meta(self, remote, meta):
        stopped = _terminate_meta(meta)
        try:
            os.remove(self._pid_file(remote))
        except Exception:
            pass
        return stopped


    def ensure_http_server(self, remote):
        fingerprint = self._configuration_fingerprint(remote)
        existing = self._load_server_meta(remote)
        if existing:
            valid = (
                existing.get('remote') == remote and
                existing.get('fingerprint') == fingerprint and
                self._pid_alive(existing.get('pid')) and
                _server_identity_matches(existing)
            )
            if valid:
                existing['last_used'] = time.time()
                self._save_server_meta(remote, existing)
                return existing
        timeout = setting_int('serve_start_timeout', 30, minimum=5, maximum=120)
        lock_path = self._acquire_server_start_lock(timeout + 5)
        try:
            # Another Kodi invocation may have completed startup while this one
            # waited for the cross-process lock.
            fingerprint = self._configuration_fingerprint(remote)
            existing = self._load_server_meta(remote)
            if existing:
                valid = (
                    existing.get('remote') == remote and
                    existing.get('fingerprint') == fingerprint and
                    self._pid_alive(existing.get('pid')) and
                    _server_identity_matches(existing)
                )
                if valid:
                    existing['last_used'] = time.time()
                    self._save_server_meta(remote, existing)
                    return existing
                self._stop_server_meta(remote, existing)

            port = self._available_port(remote)
            target = self.remote_target(remote, '')
            addr = '127.0.0.1:%d' % port
            token = _random_token()
            baseurl = '/rn/%s' % token
            server_cache = os.path.join(self.rclone_cache_dir, self._server_id(remote))
            mkdirs(server_cache)
            cmd = self.base_args(cache_dir=server_cache) + ['serve', 'http', target] + self._serve_flags(addr, token, baseurl)
            env = self._stream_env()
            sid = self._server_id(remote)
            stdout_path = os.path.join(self.pid_dir, sid + '.out.log')
            stderr_path = os.path.join(self.pid_dir, sid + '.err.log')
            stdout = open(stdout_path, 'wb')
            stderr = open(stderr_path, 'wb')
            _report('Starting rclone serve http for %s on loopback:%d' % (target, port), xbmc.LOGINFO)
            try:
                proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env=env, close_fds=True)
                _OWNED_PROCESSES[proc.pid] = proc
            finally:
                stdout.close()
                stderr.close()
            now = time.time()
            meta = {
                'schema': 2,
                'pid': proc.pid,
                'remote': remote,
                'target': target,
                'port': port,
                'token': token,
                'baseurl': baseurl,
                'started': now,
                'last_used': now,
                'fingerprint': fingerprint,
                'binary': os.path.abspath(self.rclone),
                'rclone_version': format_version(self._version_tuple),
                'cache_dir': server_cache,
            }
            self._save_server_meta(remote, meta)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if proc.poll() is not None:
                    try:
                        os.remove(self._pid_file(remote))
                    except Exception:
                        pass
                    raise RcloneError(tr(30821, logs=self._server_logs_tail(remote)))
                if _server_identity_matches(meta, timeout=0.8):
                    return meta
                time.sleep(0.25)
            self._stop_server_meta(remote, meta)
            raise RcloneError(tr(30822, seconds=timeout, logs=self._server_logs_tail(remote)))
        finally:
            self._release_server_start_lock(lock_path)



    def playback_url(self, remote, path):
        meta = self.ensure_http_server(remote)
        encoded = urllib.parse.quote((path or '').strip('/'), safe='/')
        return 'http://127.0.0.1:%d/%s/%s' % (int(meta['port']), str(meta['baseurl']).strip('/'), encoded)


    def stop_servers(self):
        stopped = 0
        if not os.path.isdir(self.pid_dir):
            return 0
        for name in os.listdir(self.pid_dir):
            if not name.endswith('.json'):
                continue
            path = os.path.join(self.pid_dir, name)
            meta = _read_json(path, {})
            if isinstance(meta, dict) and _terminate_meta(meta):
                stopped += 1
            try:
                os.remove(path)
            except Exception:
                pass
        return stopped


    def cleanup_storage(self, automatic=False, aggressive=False):
        """Remove old unlimited-cache leftovers and enforce small hard caps."""
        removed = 0
        marker = os.path.join(self.profile, '.storage_policy_v4')
        mode = (setting_text('vfs_cache_mode', 'off') or 'off').strip().lower()
        first_run_policy = not os.path.exists(marker)
        if aggressive or first_run_policy or mode == 'off':
            # This specifically clears multi-gigabyte data left by version 1.2.0.
            for path, size, _mtime in list(_iter_files(self.rclone_cache_dir) or []):
                try:
                    os.remove(path)
                    removed += size
                except Exception:
                    pass
            _remove_empty_dirs(self.rclone_cache_dir)
            try:
                with open(marker, 'w', encoding='utf-8') as fh:
                    fh.write('v4\n')
            except Exception:
                pass
        else:
            cap_mb = setting_int('vfs_cache_max_size_mb', 256, minimum=64, maximum=4096)
            removed += _cap_directory(self.rclone_cache_dir, cap_mb * 1024 * 1024, max_age_seconds=2 * 3600)
        # Folder listing JSON should remain tiny even on devices with very limited storage.
        removed += _cap_directory(self.cache_dir, 4 * 1024 * 1024, max_age_seconds=3 * 86400)
        removed += _cap_directory(self.tmp_dir, 8 * 1024 * 1024, max_age_seconds=3 * 3600)
        for filename in os.listdir(self.pid_dir) if os.path.isdir(self.pid_dir) else []:
            if filename.endswith('.log'):
                removed += _trim_file(os.path.join(self.pid_dir, filename), 64 * 1024)
        if removed:
            _report('Storage cleanup: %.1f MB freed' % (removed / 1024.0 / 1024.0), xbmc.LOGINFO)
        return removed

    def storage_usage(self):
        result = {'list_cache': 0, 'vfs_cache': 0, 'logs': 0, 'tmp': 0, 'total': 0}
        for key, folder in (
            ('list_cache', self.cache_dir), ('vfs_cache', self.rclone_cache_dir),
            ('logs', self.pid_dir), ('tmp', self.tmp_dir),
        ):
            result[key] = sum(size for _path, size, _mtime in (_iter_files(folder) or []))
        result['total'] = sum(result[key] for key in ('list_cache', 'vfs_cache', 'logs', 'tmp'))
        return result



def diagnostic_text():
    props = android_props()
    lines = [
        'Rclone Nexus %s - diagnostics' % ADDON_VERSION,
        'Minimum rclone: %s' % MIN_RCLONE_VERSION_TEXT,
        'Android-like: %s' % _is_android_like(),
        'Package/process: %s' % (android_package_name() or '(not detected)'),
        'Selected architecture: %s' % detected_android_arch(),
        'SDK: %s' % props.get('sdk'),
        'Android/Fire OS: %s' % props.get('release'),
        'Model: %s' % props.get('model'),
        'Primary ABI: %s' % props.get('cpu_abi'),
        'ABI list: %s' % props.get('cpu_abilist'),
        'Executable base: %s' % executable_base_dir(),
        'Managed path: %s' % rclone_managed_path(),
        'Kodi profile: %s' % addon_profile(),
        'Automatic download: permanently disabled',
        'VFS cache mode: %s' % setting_text('vfs_cache_mode', 'off'),
        'ListR/fast-list: %s' % ('fast-list' if setting_bool('library_fast_list', False) else 'low memory (--disable ListR)'),
        'Scan during playback: %s' % (
            'enabled (1 CPU, 1 checker, 2 calls/s)' if setting_bool('sync_during_playback', False)
            else 'disabled'
        ),
        'Servers: loopback + random base URL + identity header',
        'Accepted bundled locations:',
    ]
    lines.extend('  - ' + item for item in embedded_binary_locations())
    lines.append('Existing local candidates:')
    found = False
    for candidate in _candidate_paths() + _manual_import_candidates():
        if exists(candidate):
            lines.append('  - %s' % candidate)
            found = True
    if not found:
        lines.append('  - none')
    server_files = [name for name in os.listdir(managed_server_dir()) if name.endswith('.json')] if os.path.isdir(managed_server_dir()) else []
    lines.append('Registered servers: %d' % len(server_files))
    if _LAST_REPORT:
        lines.append('\nLast report:')
        lines.append(get_last_report())
    return '\n'.join(lines)
