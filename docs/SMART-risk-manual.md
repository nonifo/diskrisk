# SMART risk (Diskrisk) — manual

**Version:** 1.3.0  
**Service:** Diskrisk  
**Code:** `smart_risk.py`  
**CLI:** `diskinfo/`  
**Config:** `config.example.env` → `config.env` or `/etc/diskrisk/config.env`  

## Why it exists

Homelab and DIY storage often means many identical disks and **no bay LEDs**.
When SMART or a scrub hints at trouble, you still need a stable identity
(**serial**) and a sense of urgency (**is the counter growing?**). Diskrisk
turns Beszel (and optionally Scrutiny) into that shortlist — then `diskinfo`
ties each finding back to a ZFS mirror partner so you replace the correct tray.

This manual is the authoritative description of **how Diskrisk works**, what the
attributes mean, how trends are computed, and how it ties to Scrutiny,
Beszel, and `diskinfo`.

For **qualifying a disk before production** (SMART short → long → badblocks →
long, pre/post SMART), see [disk-burn-in.md](disk-burn-in.md).

See also the [demo screenshots](images/) in the README.

---

## 1. Purpose

Beszel and `smartctl -H` often report **PASSED** even when a disk already has:

- pending / uncorrectable sectors
- a growing SAS grown-defect list
- cabling faults (UDMA_CRC)

Diskrisk surfaces **actionable attributes** and marks values that **grow over
time**. A stable non-zero value can be a historical “scar”. **GROWING** is acute.

---

## 2. Architecture

```
┌─────────────┐     attributes      ┌──────────────────┐
│ Beszel hub  │ ──────────────────► │ smart_risk.py    │
│             │   smart_devices API │ :8091            │
└─────────────┘                     │  + history.json  │
                                    └────────┬─────────┘
┌─────────────┐   device_status              │
│ Scrutiny    │ ─────────────────────────────┤
│ (optional)  │                              ▼
└─────────────┘                     Web / JSON / text
                                           │
                                           ▼
                                      diskinfo RISK
```

| Piece | Role |
|---|---|
| **Beszel agents** | Collect SMART per host into the hub |
| **Beszel API** | Diskrisk authenticates and reads `smart_devices` + `systems` |
| **Scrutiny** | Optional; Diskrisk reads `device_status` by serial |
| **history.json** | Trend baseline: first value + samples over time |

Keep secrets (`BESZEL_PASS`, etc.) in `config.env` — **not in git**.

---

## 3. Data sources in detail

### 3.1 Beszel `smart_devices`

Each device includes at least:

- `system` → hostname via the `systems` collection
- `name` (e.g. `/dev/sdt`)
- `serial`, `model`, `state` (PASSED/FAILED)
- `attributes[]` with fields like `n` (name), `rv`/`raw` (raw value), `v`/`t` (normalized / threshold)

Diskrisk maps raw values via `_raw_int()` (first integer in the raw string).

### 3.2 Scrutiny summary API

`GET {SCRUTINY_URL}/api/summary` → per serial: `device_status` (integer).

- `device_status ≥ 2` counts as flagged (raises severity even without Beszel findings).
- Leave `SCRUTINY_URL` empty to disable.

### 3.3 History

Default path: `/var/lib/diskrisk/history.json` (override with `SMART_RISK_HISTORY`).

Key: `{serial}|{attr_name}` (falls back to device name if serial is missing).

Per attribute the store keeps:

- `first_value` / `first_seen` — **baseline**
- `samples[]` — retained for `SMART_RISK_HISTORY_DAYS` (default 365), at most
  `SMART_RISK_HISTORY_PER_DAY` points per day (default 2); optional hard cap
  `SMART_RISK_HISTORY_SAMPLES`
- A sample is written when the value **changes**, or at least **once per calendar day**

Each report annotates findings with:

| Field | Meaning |
|---|---|
| `first_value` | Baseline |
| `prev_value` | Previous sample |
| `delta_total` | now − baseline |
| `delta_prev` | now − previous |
| `growing` | `true` if `delta_prev > 0` **or** (`delta_total > 0` and ≥2 samples) |

UI trend labels:

| Trend | Condition |
|---|---|
| **GROWING** | `growing` |
| **stable** | ≥2 samples and `delta_total == 0` |
| **baseline** | otherwise (early observations) |

---

## 4. Attribute classification

Code: `CRITICAL`, `WARN`, `NORM_THRESHOLD_ATTRS` in `smart_risk.py`.

### 4.1 Critical (raw value ≥ 1)

| SMART name | Short UI name | Meaning |
|---|---|---|
| `Reallocated_Sector_Ct` | Realloc | Reallocated sectors |
| `Current_Pending_Sector` | Pending | Waiting rewrite — **acute** |
| `Offline_Uncorrectable` | OfflineUnc | Failed offline sectors |
| `Reported_Uncorrect` | ReportedUnc | Reported uncorrectable |
| `Reallocated_Event_Count` | ReallocEvt | Reallocation events |
| `Spin_Retry_Count` | SpinRetry | Spin-up retries |
| `End-to-End_Error` | E2E | End-to-end / datapath errors |
| `Runtime_Bad_Block` | BadBlock | Runtime bad blocks |
| `GrownDefectList` | GrownDefect | SAS grown defect (e.g. HP/Seagate) |
| `*TotalUncorrectedErrors` | Read/Write/VerifyUnc | SAS error counters |

### 4.2 Warn (raw value ≥ 1)

| SMART name | Short name | Meaning |
|---|---|---|
| `UDMA_CRC_Error_Count` | UDMA_CRC | Usually **cable/HBA/port**, not the platter |
| `Multi_Zone_Error_Rate` | MultiZone | WD “multi zone” — soft read noise; high **stable** = scar, **GROWING** = watch |
| `Command_Timeout` | CmdTimeout | Timeouts talking to the disk |

### 4.3 Normalized threshold (Seagate noise)

`Raw_Read_Error_Rate` / `Seek_Error_Rate` are flagged **only** if the
normalized value `v ≤ t` (threshold). Raw alone is usually meaningless on Seagate.

### 4.4 when_failed

If an attribute has `when_failed` set → critical finding regardless of tables.

---

## 5. Per-disk severity

```
severity = 2  if any finding.level == critical  OR  scrutiny_status ≥ 2
severity = 1  if any warn  OR  scrutiny_status == 1
severity = 0  otherwise
```

Report sort order: growing first, then severity, then host/serial.

A disk with no findings and no Scrutiny flag lands under **Clean**.

---

## 6. HTTP API

Service listens on `SMART_RISK_LISTEN` (default `0.0.0.0:8091`).

| Path | Response |
|---|---|
| `/` | HTML (Diskrisk UI) |
| `/text` | Plain-text report |
| `/json` | JSON (used by `diskinfo` and other consumers) |
| `/branding/…` | Logos / favicons |

### JSON shape (simplified)

```json
{
  "generated": "2026-09-06 08:16:27Z",
  "total_devices": 58,
  "clean": 40,
  "growing_count": 4,
  "scrutiny_flagged": 3,
  "history_path": "/var/lib/diskrisk/history.json",
  "risks": [
    {
      "system": "nas-01",
      "name": "/dev/sdt",
      "serial": "EXAMPLESERIAL",
      "model": "Example HDD",
      "state": "PASSED",
      "scrutiny_status": 0,
      "severity": 2,
      "growing": true,
      "findings": [
        {
          "level": "critical",
          "name": "GrownDefectList",
          "value": 815,
          "first_value": 725,
          "delta_total": 90,
          "delta_prev": 0,
          "growing": true,
          "samples": 28
        }
      ]
    }
  ],
  "clean_disks": []
}
```

---

## 7. Operations

```bash
sudo git clone https://github.com/nonifo/diskrisk.git /opt/diskrisk
cd /opt/diskrisk && sudo ./install.sh
sudoedit /etc/diskrisk/config.env
sudo systemctl enable --now diskrisk
curl -sS http://127.0.0.1:8091/json | head

# later
cd /opt/diskrisk && sudo ./update.sh   # keeps /etc/diskrisk/config.env
```

### Multi-server

Point `BESZEL_URL` at a hub that already has agents on every host. Diskrisk
lists all systems in one report (Host column). Run `diskinfo` on each ZFS
machine with the same `SMART_RISK_URL`.

| Variable | Purpose |
|---|---|
| `BESZEL_URL` | Beszel hub base URL |
| `BESZEL_USER` / `BESZEL_PASS` | Hub login |
| `SCRUTINY_URL` | Optional Scrutiny base URL |
| `SMART_RISK_LISTEN` | Bind address |
| `SMART_RISK_HISTORY` | Path to `history.json` |
| `SMART_RISK_HISTORY_DAYS` | Retain samples this many days (default `365`) |
| `SMART_RISK_HISTORY_PER_DAY` | Max points per calendar day (default `2`) |
| `SMART_RISK_HISTORY_SAMPLES` | Optional hard cap (default `DAYS × PER_DAY`) |
| `SMART_RISK_TOPOLOGY` | Per-host topology JSON dir |
| `DISKRISK_RISK_ENGINE` | `classic` (default) / `hybrid` / `stats` |
| `DISKRISK_INTERFACE_HISTORICAL_DAYS` | UDMA/interface scar window (default `14`) |
| `DISKRISK_MEDIA_SCAR_HISTORICAL_DAYS` | GrownDefect/Realloc scar window (default `30`) |
| `DISKRISK_SMART_DIR` | Optional `smart_collect.py` overlays (for `stats`) |
| `SMART_RISK_BRANDING` | Branding directory |
| `DISKRISK_PRODUCT` | UI product label |
| `DISKRISK_BRAND_URL` | Optional logo link target |
| `DISKRISK_LOGO` | Logo filename inside branding dir |

---

## 8. Practical interpretation

### ZFS vdevs (mirrors and raidz)

1. Open Diskrisk **or** run `diskinfo` on the NAS.
2. Find GROWING / Pending / GrownDefect.
3. With `diskinfo`: note the **same vdev** (`mirror-N`, `raidz2-N`, …).
4. **Mirror:** check the partner — healthy (`.`) → replace the bad disk before
   the partner also drifts; both bad → **urgent**.
5. **raidz / draid:** each bad disk in the **same** vdev burns parity budget;
   several yellow/red under one `raidzN-M` is much worse than spread across vdevs.

### MultiZone vs Pending

| | MultiZone | Pending / GrownDefect |
|---|---|---|
| Level (classic engine) | Warn | Critical |
| Level (hybrid engine) | INFO advisory (scar); WARN only if GROWING **and** a media peer | Critical |
| Alone, stable | Watch / noise — not “dying disk” | Plan replacement |
| GROWING | Watch; hybrid raises only with media correlation | Replace soon |
| Same ZFS vdev as another critical | Raises concern | **Urgent** (esp. mirrors / thin raidz) |

---

## 8b. Risk philosophy (smartmontools decoder + Diskrisk policy)

**Decoder vs policy.** [smartmontools](https://www.smartmontools.org/) (and Beszel’s
agent wrapping `smartctl`) decode vendor attribute IDs via `drivedb.h` into
names and RAW values. Diskrisk does **not** ship its own 256-ID encyclopedia.
It consumes decoded names and applies a **risk policy** plus trend (Now /
Baseline / Δ / GROWING).

**Priority order** (when data is available):

1. **ATA Device Statistics** (T13 / ACS — e.g. Pending Error Count, reallocated
   logical sectors) — prefer over classic SMART IDs 5/197/198 when both exist.
2. **SMART overall FAILED** / prefail normalized value ≤ manufacturer threshold.
3. **Self-test log** failures.
4. **ATA error log** (new / non-zero entries).
5. **Classic SMART attributes**, typed as:
   - **media** — Pending, Offline Uncorrectable, Reallocated, GrownDefect, …
   - **interface** — UDMA_CRC, Command_Timeout (cabling / HBA / path)
   - **advisory** — Multi_Zone_Error_Rate and similar vendor counters
6. Advisory alone → INFO / watch, **not** “disk is failing”, unless trend
   correlates with media findings.

References: T13 TR-54 / ACS Device Statistics; smartmontools `smartctl -x -j`
and Device Statistics pages; Diskrisk `risk_engine.py`.

### Engines (`DISKRISK_RISK_ENGINE`)

| Value | Behaviour |
|---|---|
| `classic` (default) | Pre-1.4 maps: raw ≥ 1 on CRITICAL/WARN lists (incl. MultiZone as warn) |
| `hybrid` | Policy table below; A/B can include `classic_findings` in `/json` |
| `stats` | hybrid + merge `smart_collect.py` overlays (DevStat / self-test / error log) |

Keep production on **classic** until local A/B looks sane, then enable hybrid.

### Hybrid policy (Fas 2)

| Signal | Policy |
|---|---|
| MultiZone (advisory) | INFO if scar; WARN only if GROWING **and** Pending/Realloc/OfflineUnc also present |
| UDMA_CRC | INTERFACE; GROWING → cabling/HBA; **no increase for 14 days** → historical scar (not actionable). Tunable: `DISKRISK_INTERFACE_HISTORICAL_DAYS` |
| GrownDefect / Realloc (scar) | MEDIA; GROWING → critical; **no increase for 30 days** → historical scar (often early-life). Pending/OfflineUnc stay critical. Tunable: `DISKRISK_MEDIA_SCAR_HISTORICAL_DAYS` |
| Pending / OfflineUnc / ReportedUnc | HIGH/CRITICAL (no historical demotion) |
| Realloc scar (Δ=0, many samples) | Covered by GrownDefect/Realloc 30d rule above |
| `when_failed` / norm ≤ thresh (prefail) | CRITICAL |

### Device Statistics inventory (Beszel vs collector)

**Inventory (Beszel agent `smart.go`, upstream):** the agent parses
`ata_device_statistics` but today only pulls **Current Temperature** via
`findAtaDeviceStatisticsValue` when the normal temperature field is missing.
Pending Error Count / reallocated logical sectors are **not** injected into the
named SMART attribute list that the hub exposes to Diskrisk. Hub records remain
named attrs + values (plus overall SMART state) — not a full DevStat tree.

Until that changes upstream (or Diskrisk grows a richer Beszel client):

- Use **hybrid** on Beszel attrs (fixes MultiZone false positives without new data).
- Optionally run **`smart_collect.py`** on storage hosts → JSON under
  `/var/lib/diskrisk/smart/` (same idea as topology). Engine `stats` merges by
  serial.

---

## 9. Related tools

| Tool | What |
|---|---|
| [diskinfo-manual.md](diskinfo-manual.md) | Mirror tree + RISK columns |
| Beszel | Host metrics + SMART collection |
| Scrutiny | Optional disk health UI / AFR |

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Empty report / auth error | `BESZEL_PASS` in config; Beszel hub up |
| No trend (always baseline) | Is `history.json` writable? |
| `diskinfo` without ↑ | Is Diskrisk `/json` reachable? `SMART_RISK_URL` |
| PASSED but highlighted in Diskrisk | Expected — health bit ≠ risk attributes |
