#!/usr/bin/env python3
"""Optional SMART collector for Diskrisk (track B): smartctl -x -j → JSON.

Run on storage hosts (or a jump host with ssh). Writes one file per host under
DISKRISK_SMART_DIR (default /var/lib/diskrisk/smart/), same pattern as topology.

Diskrisk (engine=stats|hybrid) merges overlays by serial:
  - ata_device_statistics → Pending Error Count, reallocated logical sectors, …
  - smart_status failed
  - self-test failures
  - ATA error log count

Usage:
  python3 smart_collect.py --once
  python3 smart_collect.py --host storage-01 --devices /dev/sda,/dev/sdb
  DISKRISK_SMART_SSH=1 python3 smart_collect.py --hosts storage-01,storage-02

Does not replace Beszel; enriches risk when hub attrs lack Device Statistics.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SMART_DIR = Path(os.environ.get("DISKRISK_SMART_DIR", "/var/lib/diskrisk/smart"))
USE_SSH = os.environ.get("DISKRISK_SMART_SSH", "").strip() in ("1", "true", "yes")


# Names we inject as synthetic SMART-like attrs (n / rv) for risk_engine.
DEVSTAT_NAME_MAP = {
    "Pending Error Count": "Pending_Error_Count",
    "Number of Reallocation Candidate Logical Sectors": (
        "Number_of_Reallocation_Candidate_Logical_Sectors"
    ),
    "Number of Reallocated Logical Sectors": "Number_of_Reallocated_Logical_Sectors",
    "Number of Reported Uncorrectable Errors": "Number_of_Reported_Uncorrectable_Errors",
    "Number of Mechanical Start Failures": "Number_of_Mechanical_Start_Failures",
}


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _smartctl_json(device: str, host: str | None = None) -> dict[str, Any] | None:
    base = ["smartctl", "-x", "-j", device]
    if host and USE_SSH:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host] + base
    else:
        cmd = base
    rc, out, _err = _run(cmd)
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    # smartctl often exits non-zero when SMART has issues — still parse JSON
    return data if isinstance(data, dict) else None


def _list_block_devices(host: str | None = None) -> list[str]:
    cmd = ["lsblk", "-dn", "-o", "NAME,TYPE"]
    if host and USE_SSH:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host] + cmd
    rc, out, _ = _run(cmd)
    devices: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "disk":
            devices.append(f"/dev/{parts[0]}")
    return devices


def _extract_devstat_attrs(data: dict[str, Any]) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = []
    pages = (
        data.get("ata_device_statistics")
        or data.get("ata_smart_device_statistics")
        or {}
    )
    if isinstance(pages, dict):
        page_list = pages.get("pages") or pages.get("table") or []
    elif isinstance(pages, list):
        page_list = pages
    else:
        page_list = []

    for page in page_list or []:
        if not isinstance(page, dict):
            continue
        for entry in page.get("table") or page.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            label = (
                entry.get("name")
                or entry.get("description")
                or entry.get("page_name")
                or ""
            )
            mapped = DEVSTAT_NAME_MAP.get(label)
            if not mapped:
                # fuzzy: normalize spaces
                for k, v in DEVSTAT_NAME_MAP.items():
                    if k.lower() == str(label).lower():
                        mapped = v
                        break
            if not mapped:
                continue
            val = entry.get("value")
            if val is None:
                val = entry.get("raw")
            try:
                iv = int(val)
            except (TypeError, ValueError):
                continue
            attrs.append({"n": mapped, "rv": iv, "source": "devstat"})
    return attrs


def _selftest_failed(data: dict[str, Any]) -> tuple[bool, str | None]:
    log = data.get("ata_smart_self_test_log") or {}
    if isinstance(log, dict):
        standard = log.get("standard") or log.get("extended") or {}
        table = standard.get("table") if isinstance(standard, dict) else None
        if table is None:
            table = log.get("table")
        for row in table or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or row.get("status_string") or "").lower()
            if "fail" in status and "without" not in status:
                return True, status
            passed = row.get("passed")
            if passed is False:
                return True, status or "failed"
    return False, None


def _ata_error_count(data: dict[str, Any]) -> int | None:
    err = data.get("ata_smart_error_log") or data.get("ata_error_log") or {}
    if not isinstance(err, dict):
        return None
    summary = err.get("summary") or err.get("extended") or err
    if isinstance(summary, dict):
        for key in ("count", "error_count", "number_of_errors"):
            if key in summary:
                try:
                    return int(summary[key])
                except (TypeError, ValueError):
                    pass
        table = summary.get("table")
        if isinstance(table, list):
            return len(table)
    return None


def parse_smartctl(data: dict[str, Any], device: str) -> dict[str, Any] | None:
    serial = (
        (data.get("serial_number") or data.get("serial") or "")
        .strip()
        .upper()
        .replace("-", "")
        .replace(" ", "")
    )
    if not serial:
        return None
    model = data.get("model_name") or data.get("model") or ""
    smart_status = data.get("smart_status") or {}
    passed = smart_status.get("passed")
    if passed is True:
        state = "PASSED"
    elif passed is False:
        state = "FAILED"
    else:
        state = str(smart_status.get("value") or smart_status or "UNKNOWN")

    failed_st, st_status = _selftest_failed(data)
    return {
        "serial": serial,
        "model": model,
        "device": device,
        "smart_status": state,
        "devstat_attrs": _extract_devstat_attrs(data),
        "selftest_failed": failed_st,
        "selftest_status": st_status,
        "ata_error_count": _ata_error_count(data),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def collect_host(
    host: str,
    devices: list[str] | None = None,
) -> dict[str, Any]:
    use_remote = bool(host) and host not in ("localhost", "local", ".")
    ssh_host = host if use_remote and USE_SSH else None
    local_label = host if host else "localhost"
    devs = devices or _list_block_devices(ssh_host if ssh_host else None)
    disks: list[dict[str, Any]] = []
    for d in devs:
        data = _smartctl_json(d, ssh_host)
        if not data:
            continue
        parsed = parse_smartctl(data, d)
        if parsed:
            disks.append(parsed)
    return {
        "host": local_label,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "disks": disks,
    }


def write_host(payload: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    host = payload.get("host") or "localhost"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host)
    path = out_dir / f"{safe}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_smart_index(smart_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """serial → overlay dict for risk_engine."""
    root = smart_dir or SMART_DIR
    index: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return index
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for disk in data.get("disks") or []:
            serial = (
                str(disk.get("serial") or "")
                .strip()
                .upper()
                .replace("-", "")
                .replace(" ", "")
            )
            if serial:
                index[serial] = disk
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect smartctl -x -j for Diskrisk")
    ap.add_argument("--once", action="store_true", help="Collect and exit")
    ap.add_argument("--interval", type=int, default=3600, help="Loop seconds")
    ap.add_argument("--host", default="", help="Single host label / SSH target")
    ap.add_argument("--hosts", default="", help="Comma-separated hosts")
    ap.add_argument("--devices", default="", help="Comma-separated /dev/…")
    ap.add_argument("--out", default=str(SMART_DIR), help="Output directory")
    args = ap.parse_args()

    hosts: list[str] = []
    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    elif args.host:
        hosts = [args.host.strip()]
    else:
        hosts = ["localhost"]

    devices = [d.strip() for d in args.devices.split(",") if d.strip()] or None
    out_dir = Path(args.out)

    def run_once() -> None:
        for h in hosts:
            payload = collect_host(h, devices)
            path = write_host(payload, out_dir)
            print(f"wrote {path} ({len(payload.get('disks') or [])} disks)", flush=True)

    if args.once:
        run_once()
        return 0
    while True:
        run_once()
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
