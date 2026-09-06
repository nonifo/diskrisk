#!/usr/bin/env python3
"""Collect local storage topology keyed by disk serial.

Supports:
  - ZFS pools (mirror / raidz / draid / …)
  - mergerfs pools over per-disk btrfs (or other) branches
  - snapraid parity / hotspare roles (path heuristics)

Output JSON (stdout or --out FILE) for Diskrisk to ingest:

{
  "host": "storage-01",
  "updated": "…Z",
  "disks": {
    "SERIAL": {
      "kind": "zfs"|"mergerfs"|"btrfs"|"other",
      "pool": "iron",
      "vdev": "mirror-1",
      "role": "member"|"data"|"parity"|"hotspare"|"…",
      "peers": ["OTHERSERIAL", …],
      "device": "/dev/sdt",
      "label": "…"
    }
  }
}

Usage:
  python3 topology_collect.py
  python3 topology_collect.py --host storage-01 --out /var/lib/diskrisk/topology/storage-01.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _norm_serial(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip()).upper()


def _lsblk_disks() -> dict[str, dict[str, str]]:
    """name → {serial, size, fstype, label, pkname, type} for disks and parts."""
    out = _run(
        [
            "lsblk",
            "-P",
            "-o",
            "NAME,TYPE,PKNAME,SERIAL,SIZE,FSTYPE,LABEL,MOUNTPOINT",
        ]
    )
    rows: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for token in re.findall(r'(\w+)="([^"]*)"', line):
            fields[token[0]] = token[1]
        name = fields.get("NAME") or ""
        if not name:
            continue
        rows[name] = fields
    return rows


def _disk_serial(rows: dict[str, dict[str, str]], name: str) -> str:
    """Resolve serial for a disk or partition name."""
    cur = name
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        f = rows.get(cur) or {}
        ser = _norm_serial(f.get("SERIAL") or "")
        if ser:
            return ser
        pk = (f.get("PKNAME") or "").strip()
        if pk:
            cur = pk
            continue
        # strip partition suffix: sda1 → sda, nvme0n1p1 → nvme0n1
        m = re.match(r"^(nvme\d+n\d+)p\d+$", cur)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^([a-z]+)\d+$", cur)
        if m and m.group(1) in rows:
            cur = m.group(1)
            continue
        break
    return ""


def _part_to_disk(rows: dict[str, dict[str, str]], part: str) -> str:
    f = rows.get(part) or {}
    pk = (f.get("PKNAME") or "").strip()
    if pk:
        return pk
    m = re.match(r"^(nvme\d+n\d+)p\d+$", part)
    if m:
        return m.group(1)
    m = re.match(r"^([a-z]+)\d+$", part)
    if m:
        return m.group(1)
    return part


def collect_zfs(rows: dict[str, dict[str, str]], disks: dict[str, dict[str, Any]]) -> None:
    pools = _run(["zpool", "list", "-H", "-o", "name"]).split()
    vdev_re = re.compile(r"^(mirror|raidz[123]?|draid[123]?)-[0-9]+$")
    for pool in pools:
        status = _run(["zpool", "status", "-P", pool])
        current_vdev = "root"
        members: dict[str, list[str]] = {}
        order: list[str] = []
        for raw in status.splitlines():
            line = raw.rstrip()
            if not line.strip() or line.strip().startswith("errors:"):
                continue
            # First token may be path or vdev name
            tok = line.split()[0] if line.split() else ""
            clean = tok.strip()
            if clean == pool:
                current_vdev = "root"
                continue
            if clean in ("special", "spares", "logs", "cache", "dedup") or vdev_re.match(clean):
                current_vdev = clean
                if current_vdev not in members:
                    members[current_vdev] = []
                    order.append(current_vdev)
                continue
            if "/dev/" in clean or clean.startswith("/dev/"):
                # /dev/disk/by-partuuid/… or /dev/sdX
                path = clean
                # resolve to block name
                if "by-partuuid" in path or "by-id" in path or "by-path" in path:
                    pk = _run(["lsblk", "-no", "PKNAME", path]).strip()
                    disk_name = pk or _run(["lsblk", "-no", "NAME", path]).strip().split("\n")[0]
                else:
                    base = Path(path).name
                    disk_name = _part_to_disk(rows, base)
                ser = _disk_serial(rows, disk_name)
                if not ser:
                    # try SERIAL of the path itself
                    ser = _norm_serial(_run(["lsblk", "-dno", "SERIAL", path]).strip())
                if not ser:
                    continue
                members.setdefault(current_vdev, [])
                if current_vdev not in order:
                    order.append(current_vdev)
                if ser not in members[current_vdev]:
                    members[current_vdev].append(ser)
                disks[ser] = {
                    "kind": "zfs",
                    "pool": pool,
                    "vdev": current_vdev,
                    "role": "spare"
                    if current_vdev == "spares"
                    else ("log" if current_vdev == "logs" else "member"),
                    "peers": [],  # filled below
                    "device": f"/dev/{disk_name}" if disk_name else path,
                    "label": pool,
                }
        for vdev, sers in members.items():
            for ser in sers:
                if ser in disks and disks[ser].get("pool") == pool and disks[ser].get("vdev") == vdev:
                    disks[ser]["peers"] = [p for p in sers if p != ser]


def _role_from_mount(mnt: str) -> str:
    low = mnt.lower()
    if "parity" in low:
        return "parity"
    if "hotspare" in low or low.endswith("/hotspare"):
        return "hotspare"
    if "/disks/" in low or "/disk/" in low or "/data" in low:
        return "data"
    return "member"


def _mergerfs_pools_from_fstab() -> list[tuple[str, list[str]]]:
    """Return [(mount_target, [branch_paths…]), …] from /etc/fstab."""
    pools: list[tuple[str, list[str]]] = []
    try:
        text = Path("/etc/fstab").read_text(encoding="utf-8")
    except OSError:
        return pools
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        src, target, fstype = parts[0], parts[1], parts[2]
        if fstype not in ("fuse.mergerfs", "mergerfs"):
            continue
        branches = [b for b in src.split(":") if b.startswith("/")]
        if branches:
            pools.append((target, branches))
    return pools


def collect_mergerfs(rows: dict[str, dict[str, str]], disks: dict[str, dict[str, Any]]) -> None:
    """mergerfs: branch list from fstab (SOURCE in findmnt is often just fsname)."""
    pools = _mergerfs_pools_from_fstab()
    # Also discover live mergerfs mounts (target only) so we still attach parity siblings
    live_targets = set()
    for line in _run(["findmnt", "-t", "fuse.mergerfs", "-n", "-o", "TARGET"]).splitlines():
        t = line.strip()
        if t:
            live_targets.add(t)
    if not pools and live_targets:
        # no fstab parse — fall back to treating each /…/disks/* under parent as branches
        for target in live_targets:
            parent = str(Path(target).parent)
            branches = []
            for _name, f in rows.items():
                mnt = f.get("MOUNTPOINT") or ""
                if mnt.startswith(parent + "/disks/") or "/disks/" in mnt:
                    branches.append(mnt)
            if branches:
                pools.append((target, branches))

    for target, branches in pools:
        pool = Path(target).name or target
        branch_serials: list[str] = []
        branch_meta: list[tuple[str, str, str]] = []  # ser, role, device
        for br in branches:
            src = _run(["findmnt", "-n", "-o", "SOURCE", br]).strip()
            if not src:
                for name, f in rows.items():
                    if (f.get("MOUNTPOINT") or "") == br:
                        src = f"/dev/{name}"
                        break
            if not src:
                continue
            base = Path(src).name
            disk_name = _part_to_disk(rows, base)
            ser = _disk_serial(rows, disk_name) or _norm_serial(
                _run(["lsblk", "-dno", "SERIAL", src]).strip()
            )
            if not ser:
                continue
            role = _role_from_mount(br)
            branch_serials.append(ser)
            branch_meta.append((ser, role, f"/dev/{disk_name}" if disk_name else src))

        parent = str(Path(target).parent)
        extra: list[tuple[str, str, str]] = []
        for name, f in rows.items():
            mnt = f.get("MOUNTPOINT") or ""
            if not mnt or mnt == target:
                continue
            if not (mnt.startswith(parent + "/") or mnt.startswith(parent)):
                continue
            role = _role_from_mount(mnt)
            if role not in ("parity", "hotspare"):
                continue
            disk_name = name if (f.get("TYPE") == "disk") else _part_to_disk(rows, name)
            ser = _disk_serial(rows, disk_name)
            if not ser or ser in branch_serials:
                continue
            extra.append((ser, role, f"/dev/{disk_name}"))

        data_peers = [s for s, r, _ in branch_meta if r == "data"]
        if not data_peers:
            data_peers = [s for s, _, _ in branch_meta]

        for ser, role, dev in branch_meta + extra:
            peers = (
                [p for p in data_peers if p != ser]
                if role == "data"
                else [p for p, r, _ in branch_meta + extra if p != ser and r == role]
            )
            existing = disks.get(ser)
            if existing and existing.get("kind") == "zfs":
                continue
            label = ""
            for n, f in rows.items():
                if _disk_serial(rows, n) == ser and f.get("LABEL"):
                    label = f["LABEL"]
                    break
            disks[ser] = {
                "kind": "mergerfs",
                "pool": pool,
                "vdev": role if role in ("parity", "hotspare") else "data",
                "role": role,
                "peers": peers,
                "device": dev,
                "label": label or pool,
                "mount": target if role == "data" else "",
            }


def collect_loose_btrfs(rows: dict[str, dict[str, str]], disks: dict[str, dict[str, Any]]) -> None:
    """Single-disk btrfs mounts not already claimed."""
    for name, f in rows.items():
        if (f.get("FSTYPE") or "") != "btrfs":
            continue
        if (f.get("TYPE") or "") not in ("part", "disk", "crypt"):
            continue
        ser = _disk_serial(rows, name)
        if not ser or ser in disks:
            continue
        mnt = f.get("MOUNTPOINT") or ""
        label = f.get("LABEL") or ""
        disk_name = name if f.get("TYPE") == "disk" else _part_to_disk(rows, name)
        disks[ser] = {
            "kind": "btrfs",
            "pool": label or mnt or "btrfs",
            "vdev": "single",
            "role": _role_from_mount(mnt) if mnt else "member",
            "peers": [],
            "device": f"/dev/{disk_name}",
            "label": label,
            "mount": mnt,
        }


def collect(host: str) -> dict[str, Any]:
    rows = _lsblk_disks()
    disks: dict[str, dict[str, Any]] = {}
    collect_zfs(rows, disks)
    collect_mergerfs(rows, disks)
    collect_loose_btrfs(rows, disks)
    return {
        "host": host,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disks": disks,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect storage topology by serial")
    ap.add_argument("--host", default=os.environ.get("HOSTNAME") or os.uname().nodename)
    ap.add_argument("--out", help="Write JSON to file (default: stdout)")
    args = ap.parse_args()
    report = collect(args.host.split(".")[0])
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(report['disks'])} disks)", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
