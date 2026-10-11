#!/usr/bin/env python3
"""Refresh SmartRisk physical inventory using an administrator-owned config.

Connection details and host identities belong in
``/etc/diskrisk/inventory-sync.json``, never in this public repository.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

LSBLK = ["lsblk", "-dn", "-P", "-o", "NAME,SERIAL,MODEL,TYPE"]
DEFAULT_CONFIG = "/etc/diskrisk/inventory-sync.json"


def run(args: list[str], timeout: int = 30) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
    return proc.stdout


def parse_lsblk(text: str) -> dict[str, dict[str, str]]:
    disks: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        fields = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if fields.get("TYPE") != "disk":
            continue
        model = fields.get("MODEL", "").strip().casefold()
        if model == "qemu harddisk" or "iscsi" in model:
            continue
        serial = re.sub(r"\s+", "", fields.get("SERIAL", "")).upper()
        if serial:
            disks[serial] = {
                "device": "/dev/" + fields.get("NAME", ""),
                "model": fields.get("MODEL", "").strip(),
            }
    if not disks:
        raise RuntimeError("no serial-numbered disks returned; refusing empty inventory")
    return disks


def snapshot(host: str, stdout: str) -> bytes:
    payload = {
        "host": host,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disks": parse_lsblk(stdout),
    }
    return (json.dumps(payload, indent=2) + "\n").encode()


def main() -> int:
    config_path = os.environ.get("DISKRISK_INVENTORY_SYNC_CONFIG", DEFAULT_CONFIG)
    with open(config_path, encoding="utf-8") as source:
        config = json.load(source)
    hosts = config.get("hosts")
    store_command = config.get("store_command")
    if not isinstance(hosts, list) or not hosts or not isinstance(store_command, list):
        raise RuntimeError("config must define non-empty hosts and store_command arrays")
    errors = 0
    for entry in hosts:
        host = str(entry["host"])
        try:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
                raise RuntimeError("host name contains unsupported characters")
            command = entry.get("command")
            if not isinstance(command, list) or not command:
                raise RuntimeError("host command is missing")
            stdout = run([str(part) for part in command])
            if entry.get("json_guest_agent"):
                response = json.loads(stdout)
                if int(response.get("exitcode", 1)) != 0 or response.get("exited", 1) != 1:
                    raise RuntimeError("guest agent did not complete lsblk successfully")
                stdout = str(response.get("out-data") or "")
            data = snapshot(host, stdout)
            command = [str(part).replace("{host}", host) for part in store_command]
            proc = subprocess.run(command, input=data, capture_output=True)
            if proc.returncode:
                raise RuntimeError(f"failed storing snapshot: {proc.stderr.decode(errors='replace')[:200]}")
            print(f"{host}: refreshed {len(json.loads(data)['disks'])} disk serials")
        except Exception as exc:
            errors += 1
            print(f"{host}: refresh failed: {exc}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
