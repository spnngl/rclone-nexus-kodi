"""Localized user-facing strings.

Every text shown to the user lives in
``resources/language/resource.language.<lang>/strings.po`` and is looked up by
numeric ID, so Kodi shows the translation matching its interface language and
falls back to ``en_gb`` when a translation is missing.

Conventions (enforced by ``scripts/validate.py``):

* Call ``tr`` with a literal integer ID such as 30123; never compute IDs,
  otherwise the validator cannot prove the ID exists.
* Placeholders are ``str.format`` named fields (``"{count} files"``) so
  translators may reorder them. Literal braces must be doubled in the catalogue.
* Kodi markup such as ``[COLOR]`` stays in code; catalogues hold plain text.
* Log messages and the diagnostic report are English-only and not localized.
  ``tr`` returns a ``Text`` that remembers its ID and fields, so a localized
  message, or an exception raised with one, can still be logged in English
  through ``english()``.

ID ranges:

* 30000-30099 ``resources/settings.xml``
* 30100-30499 ``addon.py``
* 30500-30599 ``service.py``
* 30600-30799 ``resources/lib/ariostv/library_manager.py``
* 30800-30999 ``resources/lib/ariostv/rclone_backend.py``
"""

import os
import re

_ENGLISH_PO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir,
    'language', 'resource.language.en_gb', 'strings.po',
)
_ENTRY = re.compile(r'^msgctxt "#(\d+)"\nmsgid "((?:[^"\\]|\\.)*)"$', re.M)
_ESCAPE = re.compile(r'\\(.)')
_english = {}


def _unescape(text):
    return _ESCAPE.sub(lambda match: '\n' if match.group(1) == 'n' else match.group(1), text)


def _english_lookup(string_id):
    """Return the en_gb source text of ``string_id``, or '' if the catalogue cannot be read."""
    if not _english:
        try:
            with open(_ENGLISH_PO, 'r', encoding='utf-8') as handle:
                _english.update((int(key), _unescape(value)) for key, value in _ENTRY.findall(handle.read()))
        except OSError:
            return ''
    return _english.get(string_id, '')


try:
    import xbmcaddon

    _ADDON = xbmcaddon.Addon()

    def _lookup(string_id):
        return _ADDON.getLocalizedString(string_id)

except Exception:  # outside Kodi (local tests): use the English source catalogue
    _lookup = _english_lookup


class Text(str):
    """Localized text that remembers its string ID and fields so ``english()`` can re-render it."""

    def __new__(cls, string_id, fields):
        text = super().__new__(cls, _lookup(string_id).format(**fields))
        text.string_id = string_id
        text.fields = fields
        return text


def tr(string_id, **fields):
    """Return the localized string ``string_id``, formatted with ``fields`` if given."""
    return Text(string_id, fields)


def english(value):
    """Return ``value`` as text for logs, rendering localized ``Text`` (also inside exceptions) in English."""
    if isinstance(value, BaseException) and len(value.args) == 1 and isinstance(value.args[0], Text):
        value = value.args[0]
    if not isinstance(value, Text):
        return str(value)
    template = _english_lookup(value.string_id)
    if not template:
        return str(value)
    fields = {
        key: english(field) if isinstance(field, (Text, BaseException)) else field
        for key, field in value.fields.items()
    }
    return template.format(**fields)
