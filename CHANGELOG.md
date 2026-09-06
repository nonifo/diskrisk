# Changelog

## [1.4.0-rc6] — 2026-09-06

### Diskrisk
- History retention by days: `SMART_RISK_HISTORY_DAYS=365` (default)
- Max `SMART_RISK_HISTORY_PER_DAY=2` samples per calendar day
- Hard cap `SMART_RISK_HISTORY_SAMPLES` defaults to days × per-day
- `first_seen` / `first_value` kept when old samples age out of the window

## [1.4.0-rc5] — 2026-09-06

### Diskrisk
- Trend badge shows scar progress: **stable 16d/14d** (days stable / days needed)
- `/json` findings include `stable_days` + `scar_need_days`

## [1.4.0-rc4] — 2026-09-06

### Diskrisk
- Surface risk policy from config in UI meta/legend and `/json` → `policy`
  (`engine`, `interface_historical_days`, `media_scar_historical_days`)
- `config.example.env` documents the knobs explicitly

## [1.4.0-rc3] — 2026-09-06

### Diskrisk
- Hybrid: **GrownDefect / Realloc** stable ≥ **30 days** → historical media scar
  (INFO, off risk list). GROWING stays critical. Pending/OfflineUnc unchanged.
  Tunable: `DISKRISK_MEDIA_SCAR_HISTORICAL_DAYS`

## [1.4.0-rc2] — 2026-09-06

### Diskrisk
- Hybrid: **UDMA_CRC / interface** with no increase for **14 days** → historical scar
  (INFO, dropped from risk list). GROWING still warns. Tunable:
  `DISKRISK_INTERFACE_HISTORICAL_DAYS`

## [1.4.0-rc1] — 2026-09-06

### Diskrisk — risk engine (local-first)
- New `risk_engine.py`: **classic** (default) vs **hybrid** / **stats** policy
- Feature flag `DISKRISK_RISK_ENGINE=classic|hybrid|stats`
- Hybrid: MultiZone → advisory (INFO scar; WARN only if GROWING + media peer);
  UDMA_CRC → interface; media counters stay critical
- `/json` includes `engine` and per-disk `classic_findings` for A/B when not classic
- UI: media / interface / advisory kind chips + legend (“MultiZone advisory ≠ FAIL”)
- Docs: SMART-risk-manual §8b (TR-54 / DevStat / smartmontools decoder)

### smart_collect.py (track B)
- Optional `smartctl -x -j` collector → `/var/lib/diskrisk/smart/` overlays
  (Device Statistics, SMART FAILED, self-test, ATA error log) for engine=`stats`
- Beszel hub surface still lacks raw DevStat trees; hybrid works on named attrs alone

## [1.3.7] — 2026-09-06

### Diskrisk
- Topology: **box each vdev / role** (mirror, raidz, mergerfs, mdadm, spare) with a clear header — easier to see which disks share a failure domain
- Docs screenshots refreshed to match live UI (list / topology / print / history)

## [1.3.6] — 2026-09-06

### Diskrisk
- **Topology** view: risk disks show the full Attribute / Now / Baseline / Δ / Trend / Hist table (same as Disk list)

## [1.3.5] — 2026-09-06

### Diskrisk
- Clearer **Topology** hierarchy: Host → Pool → vdev with indented tree lines, disk counts, and risk highlighting
- **Print sheet** view: checklist for the server room (attention list + all disks by topology); Print… hides UI chrome
- Subtle footer credit (`DISKRISK_BRAND_FOOTER` / brand URL): e.g. `bagoly.se · 2026 · Diskrisk · MIT` (no ©)
- Docs screenshots updated (list / topology / print sheet / history + diskinfo with raidz)
- `scripts/release-assets.sh` — source tarball + **SHA-256** checksums for GitHub Releases (prefer over MD5)

## [1.3.4] — 2026-09-06

### Diskrisk
- Fix attribute table column alignment (Now / Baseline / Δ / Trend); History in its own Hist column

## [1.3.3] — 2026-09-06

### Diskrisk
- **History** button on each risk attribute: when it was first seen, sparkline, and
  each sample with Δ (grew / baseline)
- JSON API: `/history` (optional `?serial=&attr=`), and `history[]` on findings in `/json`

## [1.3.2] — 2026-09-06

### topology_collect.py
- Group **mdadm** Linux software RAID (raid0/1/5/6/10/…): pool name, level as vdev, peers + spares

## [1.3.1] — 2026-09-06

### Diskrisk
- Hover tooltips on risk chips (GrownDefect, UDMA_CRC, MultiZone, …) with plain-language meaning
- Same for GROWING/stable/baseline badges and topology status chips

## [1.3.0] — 2026-09-06

### Diskrisk
- **View toggle:** keep the classic **Disk list**, or switch to **Topology**
  (host → pool → vdev / mergerfs roles)
- Ingest per-host topology JSON (`SMART_RISK_TOPOLOGY`, from `topology_collect.py`)
- List view shows pool/vdev subtitle under serial when mapped

### topology_collect.py
- Collect ZFS (mirror/raidz/draid), **mdadm** RAID, and mergerfs+btrfs (+ snapraid parity/hotspare)

## [1.2.2] — 2026-09-06

### diskinfo
- **other / standalone** section: ext4, btrfs, xfs, LUKS, LVM, etc. with label/mount
- Other ZFS pools shown as `ZFS` / `pool=… (other)`; blank disks as `EMPTY`
- Enumerate all `TYPE=disk` devices (sd / nvme / …), not only `sd*`

## [1.2.1] — 2026-09-06

### diskinfo
- Show **raidz / raidz1–3** and **draid / draid1–3** vdev headers (not only mirrors)
- Docs: treat “same vdev” as the failure domain for mirrors and raidz

## [1.2.0] — 2026-09-06

First public release.

**Note:** development used Cursor, OpenAI Codex, Anthropic Claude, and other
LLM assistants — see [AI.md](AI.md).

### diskinfo
- Standalone ZFS vdev CLI by default (no Diskrisk required)
- Optional SMART/RISK enrichment via `SMART_RISK_URL` or `--risk`
- `--live-smart` fallback to local `smartctl` when Diskrisk is unreachable
- Install with `./install.sh --diskinfo-only`

### Diskrisk
- HTTP service: HTML UI, `/json`, `/text`
- Beszel hub SMART ingest; optional Scrutiny
- Trend history (baseline / Now / GROWING)
- Git-based install (`install.sh` / `update.sh`) with config under `/etc/diskrisk/`

[1.3.7]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.7
[1.3.6]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.6
[1.3.5]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.5
[1.3.4]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.4
[1.3.3]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.3
[1.3.2]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.2
[1.3.1]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.1
[1.3.0]: https://github.com/nonifo/diskrisk/releases/tag/v1.3.0
[1.2.2]: https://github.com/nonifo/diskrisk/releases/tag/v1.2.2
[1.2.1]: https://github.com/nonifo/diskrisk/releases/tag/v1.2.1
[1.2.0]: https://github.com/nonifo/diskrisk/releases/tag/v1.2.0
