#!/usr/bin/env python3
"""Write a current physical block-device inventory keyed by disk serial.

Run on the storage guest itself. This inventory is deliberately distinct from
pool topology: it includes all present disks, including unused spares and boot
disks. The consumer can use it to suppress stale monitoring records.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def collect(host: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["lsblk", "-dn", "-P", "-o", "NAME,SERIAL,MODEL,TYPE"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lsblk failed with exit status {proc.returncode}")
    disks: dict[str, dict[str, str]] = {}
    for line in proc.stdout.splitlines():
        fields = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if fields.get("TYPE") != "disk":
            continue
        model = fields.get("MODEL", "").strip().casefold()
        if model == "qemu harddisk" or "iscsi" in model:
            continue
        serial = re.sub(r"\s+", "", fields.get("SERIAL", "")).upper()
        if not serial:
            continue
        disks[serial] = {
            "device": "/dev/" + fields.get("NAME", ""),
            "model": fields.get("MODEL", "").strip(),
        }
    if not disks:
        raise RuntimeError("lsblk returned no serial-numbered physical disks; refusing empty inventory")
    return {
        "host": host.strip().casefold(),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disks": disks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOSTNAME") or os.uname().nodename)
    parser.add_argument("--out", help="Atomic JSON destination; stdout if omitted")
    args = parser.parse_args()
    data = json.dumps(collect(args.host), indent=2) + "\n"
    if not args.out:
        sys.stdout.write(data)
        return
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    print(f"wrote {target} ({len(json.loads(data)['disks'])} disks)", file=sys.stderr)


if __name__ == "__main__":
    main()
