# Third-party / runtime dependencies

Diskrisk and diskinfo are **original works** in this repository (MIT).
They do **not** vendor third-party source code. At runtime they talk to
external tools and APIs:

| Component | Role | License | Upstream |
|-----------|------|---------|----------|
| [Beszel](https://github.com/henrygd/beszel) | SMART attributes API (`smart_devices`) | MIT | henrygd/beszel |
| [Scrutiny](https://github.com/AnalogJ/scrutiny) | Optional `device_status` by serial | MIT | AnalogJ/scrutiny |
| [smartmontools](https://www.smartmontools.org/) (`smartctl`) | Optional live SMART fallback in `diskinfo` | GPL-2.0-or-later / GPL-3.0 | external binary |
| Python 3 stdlib | HTTP server, JSON, urllib | PSF | — |
| ZFS userland (`zpool`, `lsblk`) | Pool topology for `diskinfo` | CDDL / distro | host OS |

Consuming these over HTTP or as an external process does **not** relicense
Diskrisk/diskinfo. Keep their license notices when you redistribute *those*
projects themselves.

## Branding

Files under `branding/` are **optional UI assets**. Replace them for your own
deployment (`SMART_RISK_BRANDING`, `DISKRISK_LOGO`, `DISKRISK_BRAND_URL`).
Sample placeholders may ship for local demos only.
