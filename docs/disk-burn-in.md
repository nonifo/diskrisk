# Disk assessment & burn-in (before production)

This is the recommended way to **qualify a drive before it joins a pool**, and
how that ties into Diskrisk’s later trend logic.

DIY builds often reuse second-hand disks and have **no bay LEDs**. A drive that
“looks fine” in a quick `smartctl -H` can still grow defects under load. Burn-in
gives you a **pre** and **post** SMART picture; Diskrisk then watches whether
those counters **keep growing in production**.

## How Diskrisk judges disks (runtime)

Once a disk is in Beszel (and optionally Scrutiny), Diskrisk does **not** only
trust PASSED/FAILED:

| Signal | Meaning |
|--------|---------|
| **Critical attrs** | Pending, OfflineUnc, Realloc, GrownDefect, … — data-path / media risk |
| **Warn attrs** | UDMA_CRC, MultiZone, CmdTimeout — often cable/HBA or soft noise |
| **Stable non-zero** | Often an old **scar** (survived burn-in or prior life) |
| **GROWING** (`↑`) | Value rose vs baseline / previous sample — **act** |
| **Scrutiny ≥ 2** | Extra severity bump if Scrutiny is enabled |

So: burn-in answers “is this disk usable **now**?”. Diskrisk answers “did it
**start getting worse after** we trusted it?”.

---

## Recommended burn-in chain (every new or used disk)

Run the **same order every time**. Do this on a spare machine / empty bay while
the disk is **not** in a production vdev (or is offline / not imported).

```
1) SMART short     — quick health + capture "pre" snapshot
2) SMART long      — full surface exercise (hours)
3) badblocks       — destructive or non-destructive write/read stress
4) SMART long      — "post" snapshot after stress
```

Compare **pre vs post**. If Pending / OfflineUnc / Realloc / GrownDefect
**increased**, or badblocks reported bad blocks → **do not** put the disk in
the pool. If post matches pre (or only harmless noise) → accept, label the
serial, and let Beszel/Diskrisk inherit that post-SMART as the operational
starting point.

### Why this order

| Step | Role |
|------|------|
| **SMART short** | Cheap gate; catches obvious FAIL before you burn hours |
| **SMART long (#1)** | Manufacturer self-test over the full surface; establishes **pre** counters |
| **badblocks** | Independent stress (OS-level). Finds media problems SMART might still call PASSED |
| **SMART long (#2)** | Forces the drive to update SMART after stress; your **post** / baseline |

The two long tests are the **pre** and **after** SMART anchors your scripts and
Diskrisk trends build on. Without a post-long, “GROWING” later can be confused
with “first time we ever looked”.

---

## Example commands (`smartctl` + `badblocks`)

Replace `/dev/sdX` with the real device. Prefer targeting by **serial** in your
own wrappers so names cannot reshuffle mid-run.

```bash
# Identify
smartctl -i /dev/sdX

# 1) Short self-test + save pre snapshot
smartctl -t short /dev/sdX
# wait until "Self-test execution status" is idle (smartctl -c /dev/sdX)
smartctl -a /dev/sdX | tee "smart-pre-$(date -u +%Y%m%dT%H%MZ).txt"
smartctl -x /dev/sdX | tee "smart-pre-x-$(date -u +%Y%m%dT%H%MZ).txt"

# 2) Long self-test #1
smartctl -t long /dev/sdX
# … wait (often many hours; check smartctl -c) …
smartctl -a /dev/sdX | tee "smart-long1-$(date -u +%Y%m%dT%H%MZ).txt"

# 3) badblocks (DESTRUCTIVE example — wipes the disk)
#    Use only on disks with no wanted data. Prefer -w or -n per your policy.
badblocks -wsv /dev/sdX
# Non-destructive read-only alternative (weaker):
# badblocks -sv /dev/sdX

# 4) Long self-test #2 (post)
smartctl -t long /dev/sdX
# … wait …
smartctl -a /dev/sdX | tee "smart-post-$(date -u +%Y%m%dT%H%MZ).txt"
smartctl -x /dev/sdX | tee "smart-post-x-$(date -u +%Y%m%dT%H%MZ).txt"
```

Diff the important counters by hand or with a small script: Pending,
OfflineUnc, Realloc, GrownDefect (SAS), UDMA_CRC, MultiZone.

**Pass criteria (practical):**

- badblocks: **0** bad blocks  
- SMART health: still PASSED / OK  
- Critical counters: **unchanged** (or zero) from pre → post  
- No new `when_failed` attributes  

**Fail / quarantine:** any badblocks hit, FAIL health, or growth in critical
counters during the chain.

---

## Hand-off to Diskrisk

After the disk is accepted and installed:

1. Put it in the pool / chassis; ensure the **Beszel agent** sees it.
2. Open Diskrisk once so `history.json` records the first samples — that becomes
   the production **baseline** (`first_value`). Ideally this matches your
   **post**-burn-in SMART (scars are OK if **stable**).
3. Use `diskinfo` on the NAS to confirm serial ↔ `mirror-N` partner before you
   walk to the rack.
4. From then on, trust **GROWING** more than absolute non-zero: a GrownDefect
   that was already 12 after burn-in and stays 12 is a scar; 12 → 40 is a
   replacement candidate.

Optional: keep the `smart-pre-*.txt` / `smart-post-*.txt` files next to the
serial in your inventory so humans can see the burn-in evidence later.

---

## Time expectations

| Step | Typical duration |
|------|------------------|
| SMART short | minutes |
| SMART long | hours (size / RPM dependent) |
| badblocks `-w` | many hours to >1 day on multi-TB drives |
| SMART long #2 | hours again |

Batch used disks; do not skip long #2 just because long #1 looked fine.

---

## Related

- [SMART-risk-manual.md](SMART-risk-manual.md) — runtime attribute levels & trends  
- [diskinfo-manual.md](diskinfo-manual.md) — map serial → mirror partner in the rack  
- Upstream tools: `smartctl` (smartmontools), `badblocks` (e2fsprogs)
