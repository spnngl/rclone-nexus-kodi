# Changelog

## 1.3.6

- The interface, settings, dialogs, notifications and error messages now follow the Kodi interface language instead of being hardcoded in Spanish.
- Added English (`en_gb`, fallback) and Spanish (`es_es`) language catalogues under `resources/language/`, shared with the `en_au`, `en_nz`, `en_us`, `es_ar` and `es_mx` variants.
- Fixed progress dialogs passing extra text lines, which Kodi 19 and later reject, when creating, syncing or searching libraries.
- Log messages and the diagnostic report are always in English.
- `scripts/validate.py` checks that every string ID used in code and settings exists in every catalogue, that no ID is unused, and that translations keep the same placeholders.

## 1.3.5

- Optional low-impact remote synchronization during playback, disabled by default.
- Scan processes use one Go CPU, one checker, two provider requests per second and a burst of one.
- Kodi library indexing and artwork updates are deferred until playback stops.
- Existing playback-server buffer, cache and limits remain unchanged.
- Includes the supplied 1.3.4 synchronization-recovery improvements and requires rclone 1.75.0 or newer.
- Published from the user-provided 1.3.5 ZIP; no functional/device tests run for publication.

## 1.3.1

- Translated the complete interface, settings, diagnostics, errors, documentation, and fanart to English (US).
- Kept legacy Spanish boolean and architecture aliases for backward compatibility with existing saved settings.
- Added a reproducible release builder, validation scripts, GitHub Actions workflows, and a Spanish publication guide.
- Standardized text files to UTF-8 without BOM and Unix line endings.

## 1.3.0

- Reorganized navigation into STRM libraries, New content, Favorites, remotes, and Tools.
- Added recursive STRM export without downloading media.
- Added incremental manifests, new-content detection, favorites, search, and optional background sync.
- Removed all automatic rclone downloads.
- Disabled VFS disk caching by default and added bounded cache, temporary-file, and log storage.
