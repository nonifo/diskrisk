# diskinfo — manual

**Version:** 1.2.1  
**Repo:** `diskinfo/diskinfo.sh`  
**Config:** optional — `DISKINFO_POOL`, and `SMART_RISK_URL` only if you want RISK columns  

## Standalone

**diskinfo does not require Diskrisk.** Core mode is a ZFS **vdev tree**:
structure → device → **serial** → size → RD/WR/CK → PARTUUID.

Groups shown: `mirror-N`, `raidz` / `raidz1`–`raidz3`, `draid` / `draid1`–`draid3`,
plus `special` / `spares` / `logs` / `cache` / `dedup`.

That alone is enough to find the right bay when you have no status LEDs.
Diskrisk enrichment (`--risk` / `SMART_RISK_URL`) is optional.

## Why use it

On DIY NAS builds, drives often sit in a cage **without status LEDs**. Kernel
names (`/dev/sdf`) change after reboot. `diskinfo` is the map from **pool
topology → serial on the drive label**, with SMART/risk on the same line so you
do not pull the wrong tray (healthy mirror partner, or a disk in another vdev).

## What it is

`diskinfo` shows the **ZFS pool as a vdev tree**: which physical disk belongs
to which `mirror-N` / `raidz2-N` / …, with serial, size, I/O error counters
(RD/WR/CK), and PARTUUID.

With Diskrisk enrichment it also adds **SMART** and **RISK** on the same row,
so you see vdev neighbours **and** findings without switching tools.

## Commands

```bash
diskinfo                 # ZFS tree (standalone); +RISK if SMART_RISK_URL is set
diskinfo tank            # explicit pool
diskinfo --risk          # force SMART/RISK columns (Diskrisk JSON)
diskinfo --live-smart    # if Diskrisk down, use local smartctl
diskinfo --no-smart      # ZFS + serial only
diskinfo --version
diskinfo -h
```

Environment / config:

| Env | Default | Meaning |
|---|---|---|
| `DISKINFO_POOL` | `tank` | Default pool (`tank` = usual FreeBSD/TrueNAS example name) |
| `SMART_RISK_URL` | *(empty)* | If set, enrich SMART/RISK from Diskrisk JSON |
| `DISKRISK_CONFIG` | (search path) | Optional shared `config.env` |

## Columns

Fixed-width columns — header and rows share the same widths.

| Column | Meaning |
|---|---|
| **ZFS STRUCTURE** | `ONLINE` / `AVAIL` / `UNUSED` (row); vdev headers (`mirror-0`, `raidz2-0`, …) on their own lines |
| **DEVICE** | Kernel name `/dev/sdX` (can change after reboot — trust **SERIAL**) |
| **SERIAL** | Stable identity |
| **SIZE** | Capacity |
| **RD / WR / CK** | ZFS vdev read/write/checksum errors (0 = good). Spares show `-` |
| **SMART** | `PASSED` / `FAILED` / `?` — preferably from Diskrisk (`state`); else `smartctl -H` |
| **RISK** | Compact risk attributes (see below). `.` = no risk attributes |
| **PARTUUID / INFO** | GPT partuuid in the pool, or `Not in ZFS pool` for UNUSED |

Colors: green = ONLINE/AVAIL, yellow = risk/growing, red = FAILED/FAULTED.

## RISK field

Preferably fetched from **Diskrisk**. If unreachable: live `smartctl` without
trend arrows.

| Notation | Meaning |
|---|---|
| `GrownDefect=815↑` | SAS grown defect list; **↑** = value grew since Diskrisk baseline |
| `Pending=9↑` | Current pending sectors (acute) |
| `OfflineUnc=3` | Offline uncorrectable |
| `Realloc=…` | Reallocated sectors |
| `MultiZone=108↑` | WD multi-zone error rate (**warn**, less acute than Pending) |
| `UDMA_CRC=…` | Bus/cable/HBA errors (**warn**) |
| `Scr≥2` | Scrutiny `device_status` ≥ 2 |
| `.` | Clean per Diskrisk / live scan |
| `↑` | **Growing** — same logic as the Diskrisk web UI |

### Attribute levels (same as Diskrisk)

| Level | Attributes | Interpretation |
|---|---|---|
| **Critical** | Pending, OfflineUnc, Realloc, GrownDefect, SpinRetry, … | Disk health / data at risk |
| **Warn** | UDMA_CRC, MultiZone, CmdTimeout | Often cable/HBA or soft noise; **growing** = watch |

**PASSED** only means the SMART health bit is OK. The risk column exists to
catch what the health bit misses.

Non-root operators often lack permission for `smartctl` on block devices.
With enrichment enabled, diskinfo prefers **SMART+RISK from Diskrisk JSON**.
Live `smartctl` is only a fallback (`--live-smart`). `?` = unknown
(e.g. permission denied), not FAILED.

## Same vdev (the important part)

Read **under the vdev header** — that is the failure domain:

```
mirror-1
  ONLINE   /dev/sdf     Z1X5K6VE…                   1.8T       0    0    0 PASSED  .
  ONLINE   /dev/sdt     Z1K0F5YV…                   1.8T       0    0    0 PASSED  GrownDefect=815↑
```

**Mirrors:** `sdf` and `sdt` are partners. The pool survives if *one* dies.
If **both** in the same `mirror-N` are yellow/red → urgent.

**raidz1 / raidz2 / raidz3 / draid:** every disk under `raidz2-0` (etc.) shares
that vdev’s parity budget. Know which serial is in which `raidzN-M` before you
pull a tray; multiple bad disks in the **same** raidz are much worse than the
same count spread across vdevs.

`special` = metadata (often mirrored SSDs).  
`spares` = hot spares (`AVAIL`).  
`UNUSED` = present in the chassis but not in the pool.

## Deploy

```bash
# CLI only on a ZFS host
sudo ./install.sh --diskinfo-only

# Full stack (Diskrisk service + diskinfo)
sudo ./install.sh
```

## Related

- [SMART-risk-manual.md](SMART-risk-manual.md) — how Diskrisk works  
- [disk-burn-in.md](disk-burn-in.md) — qualify disks before they join the pool  
