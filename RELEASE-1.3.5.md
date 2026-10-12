# Rclone Nexus 1.3.5 for Kodi Android

- Optional low-impact remote scans during playback; disabled by default.
- Remote scan processes use one Go CPU and one checker, with provider calls limited to two per second and a burst of one.
- Periodic processing yields and spaced STRM writes reduce background pressure.
- Kodi's heavier library indexing and artwork processing wait until playback ends.
- Playback-server buffer/cache configuration is unchanged.

The base `plugin.ariostv-1.3.5.zip` is the exact user-provided package. It does not
bundle rclone binaries: configure or import **rclone 1.75.0 or newer** matching
your Kodi application's ABI. Actual execution depends on Kodi's target SDK and
Android/Fire OS restrictions. This mode cannot guarantee interruption-free playback
on constrained devices/connections; disable it if necessary.

Archive structure, addon identity and version inspected. No functional/device tests
performed for this publication. The addon retains its supplied interface languages.

[Support development through PayPal](https://www.paypal.com/paypalme/ITARIOS).
