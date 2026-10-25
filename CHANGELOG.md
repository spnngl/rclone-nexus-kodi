# Changelog

## 1.4.1

- Requires rclone 1.75.2 or newer; older binaries are rejected by the version check. The bundled build manifest and the download links point to the official 1.75.2 Android builds.

## 1.4.0

- The interface, settings, dialogs, notifications and error messages now follow the Kodi interface language instead of being hardcoded in Spanish.
- Added English (`en_gb`, fallback) and Spanish (`es_es`) language catalogues under `resources/language/`, shared with the `en_au`, `en_nz`, `en_us`, `es_ar` and `es_mx` variants.
- Fixed progress dialogs passing extra text lines, which Kodi 19 and later reject, when creating, syncing or searching libraries.
- Kodi 21 (Omega) is now the target; Kodi 19 is no longer supported (`xbmc.python` 3.0.1). Titles are set through `InfoTagVideo` instead of the deprecated `ListItem.setInfo()`, which logged a warning for every listed item, and list items are created offscreen for faster listings.
- Log messages and the diagnostic report are always in English.
- The rclone architecture and VFS cache mode settings are now pick lists instead of free text, so a typo can no longer silently fall back to automatic detection or turn the VFS cache off.
- The background service now finds `rclone.conf` in the same places as the add-on, including the Android `Downloads` folder.
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
