#!/usr/bin/env python3
"""Diskrisk — surface actionable SMART attrs beyond PASS/FAIL.

Beszel/smartctl often report PASSED while critical counters are already non-zero.
This report highlights attributes that actually predict dying disks / bad cabling.

Trend: static non-zero counters are often historical scars; growth is acute risk.

Seagate Raw_Read_Error_Rate / Seek_Error_Rate raw values are NOT treated as
errors unless the normalized value has crossed the manufacturer threshold.
"""
from __future__ import annotations

import html
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

__version__ = "1.3.4"

_REPO_ROOT = Path(__file__).resolve().parent


def _load_config_file() -> Path | None:
    """Load KEY=VALUE from a config file into os.environ (does not override existing env).

    Search order:
      1. $DISKRISK_CONFIG
      2. ./config.env (cwd)
      3. <repo>/config.env
      4. /etc/diskrisk/config.env
    """
    candidates: list[Path] = []
    env_path = os.environ.get("DISKRISK_CONFIG", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path.cwd() / "config.env",
            _REPO_ROOT / "config.env",
            Path("/etc/diskrisk/config.env"),
        ]
    )
    seen: set[Path] = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
        return path
    return None


_CONFIG_PATH = _load_config_file()

BESZEL_URL = os.environ.get("BESZEL_URL", "http://127.0.0.1:8090").rstrip("/")
BESZEL_USER = os.environ.get("BESZEL_USER", "admin@example.com")
BESZEL_PASS = os.environ.get("BESZEL_PASS", "")
SCRUTINY_URL = os.environ.get("SCRUTINY_URL", "").rstrip("/")
LISTEN = os.environ.get("SMART_RISK_LISTEN", "0.0.0.0:8091")
HISTORY_PATH = Path(
    os.environ.get("SMART_RISK_HISTORY", "/var/lib/diskrisk/history.json")
)
HISTORY_MAX_SAMPLES = int(os.environ.get("SMART_RISK_HISTORY_SAMPLES", "120"))
BRANDING_DIR = Path(
    os.environ.get("SMART_RISK_BRANDING", str(_REPO_ROOT / "branding"))
)
PRODUCT_NAME = os.environ.get("DISKRISK_PRODUCT", "Diskrisk")
BRAND_URL = os.environ.get("DISKRISK_BRAND_URL", "").strip()
LOGO_FILE = os.environ.get("DISKRISK_LOGO", "logo.svg").strip() or "logo.svg"
TOPOLOGY_DIR = Path(
    os.environ.get(
        "SMART_RISK_TOPOLOGY",
        str(Path(os.environ.get("SMART_RISK_HISTORY", "/var/lib/diskrisk/history.json")).parent / "topology"),
    )
)

# Always actionable when raw/value > 0 (or above threshold).
CRITICAL = {
    "Reallocated_Sector_Ct": 1,
    "Current_Pending_Sector": 1,
    "Offline_Uncorrectable": 1,
    "Reported_Uncorrect": 1,
    "Reallocated_Event_Count": 1,
    "Spin_Retry_Count": 1,
    "End-to-End_Error": 1,
    "Runtime_Bad_Block": 1,
    "GrownDefectList": 1,
    "ReadTotalUncorrectedErrors": 1,
    "WriteTotalUncorrectedErrors": 1,
    "VerifyTotalUncorrectedErrors": 1,
}

# Useful but often cabling / bus — warn, not "disk dying".
WARN = {
    "UDMA_CRC_Error_Count": 1,
    "Multi_Zone_Error_Rate": 1,
    "Command_Timeout": 1,
}

# Only flag if normalized value has breached threshold (Seagate raw is noisy).
NORM_THRESHOLD_ATTRS = {
    "Raw_Read_Error_Rate",
    "Seek_Error_Rate",
}

SHORT = {
    "Reallocated_Sector_Ct": "Realloc",
    "Current_Pending_Sector": "Pending",
    "Offline_Uncorrectable": "OfflineUnc",
    "Reported_Uncorrect": "ReportedUnc",
    "Reallocated_Event_Count": "ReallocEvt",
    "Spin_Retry_Count": "SpinRetry",
    "End-to-End_Error": "E2E",
    "Runtime_Bad_Block": "BadBlock",
    "GrownDefectList": "GrownDefect",
    "ReadTotalUncorrectedErrors": "ReadUnc",
    "WriteTotalUncorrectedErrors": "WriteUnc",
    "VerifyTotalUncorrectedErrors": "VerifyUnc",
    "UDMA_CRC_Error_Count": "UDMA_CRC",
    "Multi_Zone_Error_Rate": "MultiZone",
    "Command_Timeout": "CmdTimeout",
    "Raw_Read_Error_Rate": "RawRead",
    "Seek_Error_Rate": "SeekErr",
}

# Plain-language hover text for attribute chips (and related badges).
ATTR_HELP = {
    "Reallocated_Sector_Ct": (
        "Reallocated sectors: the drive remapped bad sectors to spare area. "
        "Non-zero means media damage; GROWING means it is still getting worse."
    ),
    "Current_Pending_Sector": (
        "Pending sectors: sectors that failed read and are waiting to be remapped. "
        "Acute risk — often the strongest early warning of a dying disk."
    ),
    "Offline_Uncorrectable": (
        "Offline uncorrectable: sectors that could not be read even offline. "
        "Data may already be lost on those blocks."
    ),
    "Reported_Uncorrect": (
        "Reported uncorrectable: read/write errors the drive could not correct. "
        "Treat like OfflineUnc — plan replacement if growing."
    ),
    "Reallocated_Event_Count": (
        "Reallocation events: how many times remapping happened (not sector count). "
        "Rising events mean ongoing remaps."
    ),
    "Spin_Retry_Count": (
        "Spin retries: the spindle failed to spin up and had to retry. "
        "Often power, cable, or mechanical wear."
    ),
    "End-to-End_Error": (
        "End-to-end (E2E) errors: data integrity mismatch between host and drive cache. "
        "Can be drive firmware or path issues."
    ),
    "Runtime_Bad_Block": (
        "Runtime bad block: bad block detected during operation. "
        "Media problem — watch for growth."
    ),
    "GrownDefectList": (
        "Grown defect list (SAS): defects found after manufacturing. "
        "A scar can sit stable for years; GROWING (↑) means new defects are appearing now."
    ),
    "ReadTotalUncorrectedErrors": (
        "Uncorrected read errors (SAS): reads that failed permanently. "
        "Rising values threaten readable data."
    ),
    "WriteTotalUncorrectedErrors": (
        "Uncorrected write errors (SAS): writes that failed permanently. "
        "Rising values threaten stored data."
    ),
    "VerifyTotalUncorrectedErrors": (
        "Uncorrected verify errors (SAS): verify/check commands that failed. "
        "Often appears with other media errors."
    ),
    "UDMA_CRC_Error_Count": (
        "UDMA CRC: checksum errors on the cable/HBA path (not always the platter). "
        "Often a loose cable, bad port, or backplane — reseat before condemning the disk. "
        "GROWING still means the link is noisy right now."
    ),
    "Multi_Zone_Error_Rate": (
        "Multi-Zone error rate (often WD): soft error / zone noise counter. "
        "Warn-level alone can be historical; GROWING deserves a closer look with Pending/Realloc."
    ),
    "Command_Timeout": (
        "Command timeouts: the drive did not answer in time. "
        "Can be load, cable, enclosure, or a disk starting to stall."
    ),
    "Raw_Read_Error_Rate": (
        "Raw read error rate: Seagate raw values are often noisy and not counted as failures here "
        "unless the normalized value has crossed the manufacturer threshold."
    ),
    "Seek_Error_Rate": (
        "Seek error rate: head positioning noise. Seagate raw is noisy; "
        "only flagged here if normalized value crossed the threshold."
    ),
}

TREND_HELP = {
    "GROWING": "Value increased since baseline — acute change, prioritize this disk.",
    "stable": "Same as the first recorded baseline — often an old scar, not an active climb.",
    "baseline": "First sample in history — no trend yet; wait for more samples.",
}

STATUS_HELP = {
    "clean": "No actionable SMART risk attributes on this disk.",
    "ok": "No elevated severity for this row.",
    "risk": "Critical SMART attributes present (Pending, Realloc, GrownDefect, …).",
    "warn": "Warn-level attributes (often cable/bus noise like UDMA_CRC or MultiZone).",
    "GROWING": "At least one risk attribute is still climbing versus baseline.",
    "Scrutiny flagged": (
        "Scrutiny device_status ≥ 2 for this serial — open Scrutiny for details. "
        "Independent of Beszel SMART attributes."
    ),
}


def _attr_help(name: str) -> str:
    return ATTR_HELP.get(name, f"SMART attribute: {name}")


def _chip(level: str, label: str, help_text: str) -> str:
    """Risk/status chip with native browser tooltip (title)."""
    return (
        f'<span class="chip {html.escape(level)}" '
        f'title="{html.escape(help_text, quote=True)}">{html.escape(label)}</span>'
    )


@dataclass
class Finding:
    level: str  # critical | warn | info
    name: str
    value: Any
    note: str = ""
    # trend (filled after history update)
    first_value: Any = None
    prev_value: Any = None
    delta_total: int | None = None
    delta_prev: int | None = None
    growing: bool = False
    first_seen: str = ""
    samples: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)  # [{ts, value}, …]


@dataclass
class TopologyInfo:
    kind: str = ""  # zfs | mergerfs | btrfs | …
    pool: str = ""
    vdev: str = ""
    role: str = ""
    peers: list[str] = field(default_factory=list)
    device: str = ""
    label: str = ""
    host: str = ""  # topology file host hint

    @property
    def summary(self) -> str:
        if not self.pool and not self.vdev:
            return ""
        bits = [self.pool] if self.pool else []
        if self.vdev and self.vdev not in ("root", "single", "data"):
            bits.append(self.vdev)
        elif self.vdev == "data" and self.kind == "mergerfs":
            bits.append("data")
        elif self.role in ("parity", "hotspare", "spare"):
            bits.append(self.role)
        return " / ".join(bits)

    @property
    def peer_note(self) -> str:
        n = len(self.peers)
        if n == 0:
            return ""
        if n == 1:
            return f"peer {self.peers[0]}"
        # show one example + count
        return f"{n} peers · e.g. {self.peers[0]}"


@dataclass
class DiskRisk:
    system: str
    name: str
    serial: str
    model: str
    state: str
    findings: list[Finding] = field(default_factory=list)
    scrutiny_status: int | None = None
    topology: TopologyInfo | None = None

    @property
    def severity(self) -> int:
        if any(f.level == "critical" for f in self.findings) or (self.scrutiny_status or 0) >= 2:
            return 2
        if any(f.level == "warn" for f in self.findings) or (self.scrutiny_status or 0) == 1:
            return 1
        return 0

    @property
    def any_growing(self) -> bool:
        return any(f.growing for f in self.findings)


@dataclass
class CleanDisk:
    system: str
    name: str
    serial: str
    model: str
    state: str
    topology: TopologyInfo | None = None


def _http_json(url: str, data: dict | None = None, headers: dict | None = None) -> Any:
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def beszel_token() -> str:
    if not BESZEL_PASS:
        raise SystemExit("BESZEL_PASS is required")
    for path in (
        f"{BESZEL_URL}/api/collections/_superusers/auth-with-password",
        f"{BESZEL_URL}/api/collections/users/auth-with-password",
    ):
        try:
            r = _http_json(path, {"identity": BESZEL_USER, "password": BESZEL_PASS})
            return r["token"]
        except urllib.error.HTTPError:
            continue
    raise SystemExit("Beszel auth failed")


def beszel_records(token: str, collection: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        url = (
            f"{BESZEL_URL}/api/collections/{collection}/records"
            f"?page={page}&perPage=200"
        )
        data = _http_json(url, headers={"Authorization": token})
        items.extend(data.get("items") or [])
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return items


def scrutiny_status_by_serial() -> dict[str, int]:
    if not SCRUTINY_URL:
        return {}
    try:
        data = _http_json(f"{SCRUTINY_URL}/api/summary")
    except Exception:
        return {}
    out: dict[str, int] = {}
    for _wwn, v in (data.get("data") or {}).get("summary", {}).items():
        sn = (v.get("device") or {}).get("serial_number") or ""
        st = (v.get("device") or {}).get("device_status")
        if sn and st is not None:
            out[sn] = int(st)
    return out


def _norm_serial(s: str) -> str:
    return "".join((s or "").split()).upper()


def load_topology() -> dict[str, TopologyInfo]:
    """Load serial → TopologyInfo from SMART_RISK_TOPOLOGY/*.json (or a single file)."""
    out: dict[str, TopologyInfo] = {}
    path = TOPOLOGY_DIR
    files: list[Path] = []
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.glob("*.json"))
    else:
        return out

    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        host = (data.get("host") or fp.stem or "").strip()
        disks = data.get("disks") or {}
        if not isinstance(disks, dict):
            continue
        for serial, meta in disks.items():
            if not isinstance(meta, dict):
                continue
            key = _norm_serial(serial)
            if not key:
                continue
            peers = meta.get("peers") or []
            if not isinstance(peers, list):
                peers = []
            info = TopologyInfo(
                kind=str(meta.get("kind") or ""),
                pool=str(meta.get("pool") or ""),
                vdev=str(meta.get("vdev") or ""),
                role=str(meta.get("role") or ""),
                peers=[_norm_serial(p) for p in peers if p],
                device=str(meta.get("device") or ""),
                label=str(meta.get("label") or ""),
                host=host,
            )
            # Prefer richer entries (more peers / non-empty pool)
            prev = out.get(key)
            if prev is None or (len(info.peers) >= len(prev.peers) and info.pool):
                out[key] = info
    return out


def _lookup_topology(topo: dict[str, TopologyInfo], serial: str) -> TopologyInfo | None:
    key = _norm_serial(serial)
    if not key:
        return None
    if key in topo:
        return topo[key]
    # prefix / containment match for truncated SMART serials
    for k, info in topo.items():
        if key.startswith(k) or k.startswith(key):
            if min(len(key), len(k)) >= 8:
                return info
    return None


def _raw_int(attr: dict) -> int | None:
    for key in ("rv", "raw", "raw_value"):
        if key in attr and attr[key] is not None:
            try:
                return int(str(attr[key]).split()[0])
            except ValueError:
                continue
    rs = attr.get("rs") or attr.get("raw_string")
    if rs is not None:
        try:
            return int(str(rs).split()[0])
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).split()[0])
    except (TypeError, ValueError):
        return None


def evaluate_attrs(attrs: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for a in attrs or []:
        name = a.get("n") or a.get("name") or ""
        if not name:
            continue
        raw = _raw_int(a)
        v = a.get("v")
        t = a.get("t")

        if name in NORM_THRESHOLD_ATTRS:
            if v is not None and t is not None:
                try:
                    if int(v) <= int(t):
                        findings.append(
                            Finding(
                                "critical",
                                name,
                                f"norm={v} thresh={t} raw={raw}",
                                "normalized value at/under threshold",
                            )
                        )
                except (TypeError, ValueError):
                    pass
            continue

        if name in CRITICAL and raw is not None and raw >= CRITICAL[name]:
            findings.append(Finding("critical", name, raw))
            continue

        if name in WARN and raw is not None and raw >= WARN[name]:
            note = "often cabling/HBA" if name == "UDMA_CRC_Error_Count" else ""
            findings.append(Finding("warn", name, raw, note))
            continue

        wf = (a.get("wf") or a.get("when_failed") or "").strip()
        if wf:
            findings.append(
                Finding("critical", name, raw if raw is not None else wf, f"when_failed={wf}")
            )

    return findings


def load_history() -> dict[str, Any]:
    try:
        return json.loads(HISTORY_PATH.read_text())
    except Exception:
        return {"version": 1, "attrs": {}}


def save_history(hist: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(hist, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(HISTORY_PATH.parent), prefix=".hist-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def history_key(serial: str, name: str, device_name: str) -> str:
    ident = (serial or device_name or "?").strip()
    return f"{ident}|{name}"


def update_history(risks: list[DiskRisk]) -> dict[str, Any]:
    """Record numeric finding values and annotate findings with trend."""
    hist = load_history()
    attrs: dict[str, Any] = hist.setdefault("attrs", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for r in risks:
        for f in r.findings:
            cur = _as_int(f.value)
            if cur is None:
                continue
            key = history_key(r.serial, f.name, r.name)
            entry = attrs.get(key) or {
                "serial": r.serial,
                "system": r.system,
                "device": r.name,
                "attr": f.name,
                "first_seen": now,
                "first_value": cur,
                "samples": [],
            }
            samples: list[dict] = list(entry.get("samples") or [])
            last = samples[-1] if samples else None
            # Store when value changes, or at least once per calendar day.
            day = now[:10]
            if last is None or last.get("value") != cur or str(last.get("ts", ""))[:10] != day:
                samples.append({"ts": now, "value": cur})
            samples = samples[-HISTORY_MAX_SAMPLES:]
            entry["samples"] = samples
            entry["first_value"] = entry.get("first_value", samples[0]["value"])
            entry["first_seen"] = entry.get("first_seen") or samples[0]["ts"]
            entry["system"] = r.system
            entry["device"] = r.name
            entry["serial"] = r.serial
            entry["attr"] = f.name
            entry["last_value"] = cur
            entry["last_seen"] = now
            attrs[key] = entry

            first = _as_int(entry.get("first_value"))
            prev = _as_int(samples[-2]["value"]) if len(samples) >= 2 else first
            f.first_value = first
            f.prev_value = prev
            f.first_seen = str(entry.get("first_seen") or "")
            f.samples = len(samples)
            f.history = [{"ts": s.get("ts"), "value": s.get("value")} for s in samples]
            if first is not None:
                f.delta_total = cur - first
            if prev is not None:
                f.delta_prev = cur - prev
            f.growing = bool(
                (f.delta_prev is not None and f.delta_prev > 0)
                or (f.delta_total is not None and f.delta_total > 0 and len(samples) >= 2)
            )
            if f.growing and "growing" not in (f.note or ""):
                f.note = (f.note + "; " if f.note else "") + "growing"

    hist["updated"] = now
    save_history(hist)
    return hist


def build_report() -> dict[str, Any]:
    token = beszel_token()
    systems = {s["id"]: s.get("name") or s["id"] for s in beszel_records(token, "systems")}
    devices = beszel_records(token, "smart_devices")
    scr = scrutiny_status_by_serial()
    topo = load_topology()

    risks: list[DiskRisk] = []
    clean: list[CleanDisk] = []
    for d in devices:
        findings = evaluate_attrs(d.get("attributes") or [])
        serial = (d.get("serial") or "").strip()
        risk = DiskRisk(
            system=systems.get(d.get("system"), d.get("system") or "?"),
            name=d.get("name") or "",
            serial=serial,
            model=d.get("model") or "",
            state=d.get("state") or "",
            findings=findings,
            scrutiny_status=scr.get(serial) if serial else None,
            topology=_lookup_topology(topo, serial),
        )
        if risk.scrutiny_status is None and serial:
            risk.scrutiny_status = scr.get(_norm_serial(serial))
        if risk.severity or risk.findings:
            risks.append(risk)
        else:
            clean.append(
                CleanDisk(
                    system=risk.system,
                    name=risk.name,
                    serial=risk.serial,
                    model=risk.model,
                    state=risk.state,
                    topology=risk.topology,
                )
            )

    update_history(risks)
    risks.sort(key=lambda r: (-int(r.any_growing), -r.severity, r.system, r.serial))
    clean.sort(key=lambda c: (c.system, c.serial or c.name))

    growing_count = sum(1 for r in risks if r.any_growing)
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "total_devices": len(devices),
        "clean": len(clean),
        "clean_disks": clean,
        "risks": risks,
        "growing_count": growing_count,
        "scrutiny_flagged": sum(1 for r in risks if (r.scrutiny_status or 0) >= 2),
        "history_path": str(HISTORY_PATH),
        "topology_path": str(TOPOLOGY_DIR),
        "topology_mapped": sum(1 for r in risks if r.topology) + sum(1 for c in clean if c.topology),
    }


def _trend_label(f: Finding) -> str:
    if f.growing:
        return "GROWING"
    if f.samples >= 2 and (f.delta_total or 0) == 0:
        return "stable"
    return "baseline"


def _fmt_delta(n: int | None) -> str:
    if n is None:
        return "—"
    if n > 0:
        return f"+{n}"
    return str(n)


def _finding_trend(f: Finding) -> str:
    """Compact one-liner for text/JSON consumers."""
    short = SHORT.get(f.name, f.name)
    return (
        f"{short} now={f.value} base={f.first_value if f.first_value is not None else '—'} "
        f"Δ={_fmt_delta(f.delta_total)} [{_trend_label(f)}]"
    )


def _sparkline_svg(samples: list[dict[str, Any]], width: int = 280, height: int = 56) -> str:
    """Tiny SVG line chart for history samples."""
    vals: list[int] = []
    for s in samples:
        v = _as_int(s.get("value"))
        if v is not None:
            vals.append(v)
    if len(vals) < 1:
        return f'<svg width="{width}" height="{height}"></svg>'
    if len(vals) == 1:
        vals = [vals[0], vals[0]]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 1)
    pad = 4
    pts = []
    n = len(vals)
    for i, v in enumerate(vals):
        x = pad + (width - 2 * pad) * (i / (n - 1))
        y = height - pad - (height - 2 * pad) * ((v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="value over time">'
        f'<polyline fill="none" stroke="#007d8a" stroke-width="2" points="{poly}"/>'
        f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="3" fill="#c44a32"/>'
        f"</svg>"
    )


def _history_payload(r: DiskRisk, f: Finding) -> dict[str, Any]:
    short = SHORT.get(f.name, f.name)
    return {
        "system": r.system,
        "serial": r.serial,
        "device": r.name,
        "attr": f.name,
        "short": short,
        "help": _attr_help(f.name),
        "level": f.level,
        "growing": f.growing,
        "first_seen": f.first_seen,
        "first_value": f.first_value,
        "value": f.value,
        "delta_total": f.delta_total,
        "samples": f.history,
    }


def _finding_attr_row(r: DiskRisk, f: Finding) -> str:
    """HTML row for one risk attribute with visible trend columns + history opener."""
    short = SHORT.get(f.name, f.name)
    help_text = _attr_help(f.name)
    if f.note:
        help_text = f"{help_text} [{f.note}]"
    if f.first_seen:
        help_text = f"{help_text} · first seen {f.first_seen}"
    help_text = f"{short} ({f.name}): {help_text}"
    trend = _trend_label(f)
    trend_cls = {"GROWING": "growing", "stable": "stable", "baseline": "base"}[trend]
    trend_help = TREND_HELP.get(trend, trend)
    lvl = "growing" if f.growing else f.level
    payload = html.escape(json.dumps(_history_payload(r, f), ensure_ascii=False), quote=True)
    hist_btn = (
        f'<button type="button" class="hist-btn" data-hist="{payload}" '
        f'title="Open history: when this value appeared and how it changed">'
        f"Hist</button>"
    )
    return (
        f'<tr class="attr {lvl}">'
        f'<td class="attr-name">{_chip(lvl, short, help_text)}</td>'
        f'<td class="num" title="{html.escape(help_text, quote=True)}">'
        f"{html.escape(str(f.value))}</td>"
        f'<td class="num">{html.escape(str(f.first_value if f.first_value is not None else "—"))}</td>'
        f'<td class="num">{html.escape(_fmt_delta(f.delta_total))}</td>'
        f'<td class="num">{html.escape(_fmt_delta(f.delta_prev))}</td>'
        f'<td class="trend"><span class="badge {trend_cls}" title="{html.escape(trend_help, quote=True)}">'
        f"{trend}</span></td>"
        f'<td class="hist">{hist_btn}</td>'
        f"</tr>"
    )


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"{PRODUCT_NAME} — {report['generated']}",
        f"Devices: {report['total_devices']}  clean: {report['clean']}  "
        f"with findings: {len(report['risks'])}  growing: {report.get('growing_count', 0)}  "
        f"Scrutiny device_status≥2: {report['scrutiny_flagged']}",
        "",
        "=== RISK ===",
        "",
    ]
    for r in report["risks"]:
        scr = f" scrutiny={r.scrutiny_status}" if r.scrutiny_status else ""
        grow = " GROWING" if r.any_growing else ""
        lines.append(
            f"[{r.system}] {r.serial or r.name}  {r.model}  smart={r.state}{scr}{grow}"
        )
        for f in r.findings:
            note = f" ({f.note})" if f.note else ""
            lines.append(f"  {f.level:8} {_finding_trend(f)}{note}")
        if not r.findings and (r.scrutiny_status or 0) >= 2:
            lines.append("  critical Scrutiny device_status flagged (see Scrutiny UI)")
        lines.append("")

    lines.append("=== CLEAN ===")
    lines.append("")
    for c in report.get("clean_disks") or []:
        lines.append(f"[{c.system}] {c.serial or c.name}  {c.model}  smart={c.state}")
    if not report.get("clean_disks"):
        lines.append("(none)")
    lines.append("")
    return "\n".join(lines)


def _serialize_finding(f: Finding) -> dict[str, Any]:
    return {
        "level": f.level,
        "name": f.name,
        "value": f.value,
        "note": f.note,
        "first_value": f.first_value,
        "prev_value": f.prev_value,
        "delta_total": f.delta_total,
        "delta_prev": f.delta_prev,
        "growing": f.growing,
        "first_seen": f.first_seen,
        "samples": f.samples,
        "history": f.history,
    }


def _serialize_topo(t: TopologyInfo | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {
        "kind": t.kind,
        "pool": t.pool,
        "vdev": t.vdev,
        "role": t.role,
        "peers": t.peers,
        "device": t.device,
        "label": t.label,
        "host": t.host,
        "summary": t.summary,
    }


def _serialize_risk(r: DiskRisk) -> dict[str, Any]:
    return {
        "system": r.system,
        "name": r.name,
        "serial": r.serial,
        "model": r.model,
        "state": r.state,
        "scrutiny_status": r.scrutiny_status,
        "severity": r.severity,
        "growing": r.any_growing,
        "findings": [_serialize_finding(f) for f in r.findings],
        "topology": _serialize_topo(r.topology),
    }


def _serialize_clean(c: CleanDisk) -> dict[str, Any]:
    return {
        "system": c.system,
        "name": c.name,
        "serial": c.serial,
        "model": c.model,
        "state": c.state,
        "topology": _serialize_topo(c.topology),
    }


def serialize_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated": report["generated"],
        "total_devices": report["total_devices"],
        "clean": report["clean"],
        "growing_count": report.get("growing_count", 0),
        "scrutiny_flagged": report["scrutiny_flagged"],
        "history_path": report.get("history_path"),
        "topology_path": report.get("topology_path"),
        "topology_mapped": report.get("topology_mapped", 0),
        "risks": [_serialize_risk(r) for r in report["risks"]],
        "clean_disks": [_serialize_clean(c) for c in report.get("clean_disks") or []],
    }


def _disk_risk_chip(r: DiskRisk | CleanDisk, is_risk: bool) -> str:
    if not is_risk:
        return _chip("ok", "clean", STATUS_HELP["clean"])
    assert isinstance(r, DiskRisk)
    if r.any_growing:
        return _chip("growing", "GROWING", STATUS_HELP["GROWING"])
    if r.severity >= 2:
        return _chip("critical", "risk", STATUS_HELP["risk"])
    if r.severity == 1:
        return _chip("warn", "warn", STATUS_HELP["warn"])
    return _chip("ok", "ok", STATUS_HELP["ok"])


def _render_topology_view(report: dict[str, Any]) -> str:
    """Group disks by host → pool → vdev for the Topology view."""
    by_serial: dict[str, tuple[Any, bool]] = {}
    for r in report.get("risks") or []:
        by_serial[_norm_serial(r.serial) or r.name] = (r, True)
    for c in report.get("clean_disks") or []:
        key = _norm_serial(c.serial) or c.name
        by_serial.setdefault(key, (c, False))

    # host → pool → vdev → [entries]
    tree: dict[str, dict[str, dict[str, list[tuple[Any, bool]]]]] = {}
    unmapped: dict[str, list[tuple[Any, bool]]] = {}

    for _key, (disk, is_risk) in by_serial.items():
        host = disk.system
        t = disk.topology
        if not t or not t.pool:
            unmapped.setdefault(host, []).append((disk, is_risk))
            continue
        pool = t.pool
        vdev = t.vdev or t.role or "members"
        tree.setdefault(host, {}).setdefault(pool, {}).setdefault(vdev, []).append((disk, is_risk))

    blocks: list[str] = []
    if not tree and not unmapped:
        return '<p class="muted">No topology data yet. Run <code>topology_collect.py</code> on each storage host and place JSON under the topology directory.</p>'

    for host in sorted(tree.keys()):
        blocks.append(f'<section class="topo-host"><h3>{html.escape(host)}</h3>')
        for pool in sorted(tree[host].keys()):
            # kind from first disk
            sample = next(iter(tree[host][pool].values()))[0][0]
            kind = (sample.topology.kind if sample.topology else "") or ""
            blocks.append(
                f'<div class="topo-pool"><div class="topo-pool-h">'
                f'<span class="chip kind">{html.escape(kind or "pool")}</span> '
                f'<strong>{html.escape(pool)}</strong></div>'
            )
            for vdev in sorted(tree[host][pool].keys()):
                members = tree[host][pool][vdev]
                blocks.append(f'<div class="topo-vdev"><div class="topo-vdev-h">{html.escape(vdev)}</div><ul>')
                # sort: growing/risk first
                def _sk(item: tuple[Any, bool]) -> tuple:
                    d, ir = item
                    grow = int(getattr(d, "any_growing", False)) if ir else 0
                    sev = int(getattr(d, "severity", 0)) if ir else 0
                    return (-grow, -sev, d.serial or d.name)

                for disk, is_risk in sorted(members, key=_sk):
                    chip = _disk_risk_chip(disk, is_risk)
                    role = ""
                    if disk.topology and disk.topology.role:
                        role = f' <span class="muted">({html.escape(disk.topology.role)})</span>'
                    findings = ""
                    if is_risk and getattr(disk, "findings", None):
                        parts = []
                        for f in disk.findings[:4]:
                            short = SHORT.get(f.name, f.name)
                            arrow = "↑" if f.growing else ""
                            lvl = "growing" if f.growing else f.level
                            help_text = _attr_help(f.name)
                            help_text = f"{short} ({f.name}): {help_text}"
                            parts.append(_chip(lvl, f"{short}={f.value}{arrow}", help_text))
                        if parts:
                            findings = " " + " ".join(parts)
                    blocks.append(
                        "<li>"
                        f"{chip} <code>{html.escape(disk.serial or disk.name)}</code>"
                        f"{role}"
                        f'<div class="sub">{html.escape(disk.name)} · {html.escape(disk.model)}</div>'
                        f"{findings}"
                        "</li>"
                    )
                blocks.append("</ul></div>")
            blocks.append("</div>")
        blocks.append("</section>")

    if unmapped:
        blocks.append('<section class="topo-host"><h3>Unmapped (SMART only)</h3>')
        for host in sorted(unmapped.keys()):
            blocks.append(f'<div class="topo-pool"><div class="topo-pool-h"><strong>{html.escape(host)}</strong></div><ul>')
            for disk, is_risk in unmapped[host]:
                chip = _disk_risk_chip(disk, is_risk)
                blocks.append(
                    f"<li>{chip} <code>{html.escape(disk.serial or disk.name)}</code>"
                    f'<div class="sub">{html.escape(disk.name)} · {html.escape(disk.model)}</div></li>'
                )
            blocks.append("</ul></div>")
        blocks.append("</section>")

    return "\n".join(blocks)


def render_html(report: dict[str, Any]) -> str:
    logo_name = LOGO_FILE
    if not (BRANDING_DIR / logo_name).is_file():
        for candidate in ("logo.svg", "logo-black.svg"):
            if (BRANDING_DIR / candidate).is_file():
                logo_name = candidate
                break
    logo_href = html.escape(BRAND_URL) if BRAND_URL else "#"
    logo_aria = html.escape(PRODUCT_NAME)
    has_logo = (BRANDING_DIR / logo_name).is_file()
    logo_css = (
        f"background: url('/branding/{html.escape(logo_name)}') center / contain no-repeat;"
        if has_logo
        else "display:none;"
    )
    brand_link = (
        f'<a class="logo" href="{logo_href}" aria-label="{logo_aria}"></a>'
        if has_logo
        else ""
    )

    risk_rows = []
    for r in report["risks"]:
        sev = {2: "critical", 1: "warn", 0: "info"}[r.severity]
        if r.any_growing:
            sev = "growing"
        if r.findings:
            attr_table = (
                '<table class="attrs"><colgroup>'
                '<col class="c-a-name"/><col class="c-a-nu"/><col class="c-a-bas"/>'
                '<col class="c-a-dtot"/><col class="c-a-dprev"/><col class="c-a-trend"/>'
                '<col class="c-a-hist"/>'
                "</colgroup><thead><tr>"
                '<th class="attr-name">Attribute</th>'
                '<th class="num">Now</th><th class="num">Baseline</th>'
                '<th class="num">Δ tot</th><th class="num">Δ last</th>'
                '<th class="trend">Trend</th><th class="hist">Hist</th>'
                "</tr></thead><tbody>"
                + "".join(_finding_attr_row(r, f) for f in r.findings)
                + "</tbody></table>"
            )
        elif (r.scrutiny_status or 0) >= 2:
            attr_table = _chip("critical", "Scrutiny flagged", STATUS_HELP["Scrutiny flagged"])
        else:
            attr_table = '<span class="muted">—</span>'
        scr = "—" if r.scrutiny_status is None else str(r.scrutiny_status)
        badge = ""
        if r.any_growing:
            badge += (
                f' <span class="badge grow" title="{html.escape(STATUS_HELP["GROWING"], quote=True)}">'
                f"GROWING</span>"
            )
        if (not r.any_growing) and any(
            f.samples >= 2 and (f.delta_total or 0) == 0 for f in r.findings
        ):
            badge += (
                f' <span class="badge stable" title="{html.escape(TREND_HELP["stable"], quote=True)}">'
                f"stable</span>"
            )
        elif (not r.any_growing) and r.findings and all(f.samples < 2 for f in r.findings):
            badge += (
                f' <span class="badge base" title="{html.escape(TREND_HELP["baseline"], quote=True)}">'
                f"baseline</span>"
            )
        topo_sub = ""
        if r.topology and r.topology.summary:
            peer = html.escape(r.topology.peer_note)
            topo_sub = (
                f"<div class='sub topo'>{html.escape(r.topology.kind)} · "
                f"{html.escape(r.topology.summary)}"
                + (f" · {peer}" if peer else "")
                + "</div>"
            )
        risk_rows.append(
            "<tr class='{sev}'>"
            "<td>{system}{badge}</td>"
            "<td><code>{serial}</code><div class='sub'>{name}</div>{topo}</td>"
            "<td>{model}</td>"
            "<td>{state}</td>"
            "<td>{scr}</td>"
            "<td class='attrs-cell'>{attrs}</td>"
            "</tr>".format(
                sev=sev,
                system=html.escape(r.system),
                badge=badge,
                serial=html.escape(r.serial or r.name),
                name=html.escape(r.name),
                topo=topo_sub,
                model=html.escape(r.model),
                state=html.escape(r.state),
                scr=html.escape(scr),
                attrs=attr_table,
            )
        )

    clean_rows = []
    for c in report.get("clean_disks") or []:
        topo_sub = ""
        if c.topology and c.topology.summary:
            peer = html.escape(c.topology.peer_note)
            topo_sub = (
                f"<div class='sub topo'>{html.escape(c.topology.kind)} · "
                f"{html.escape(c.topology.summary)}"
                + (f" · {peer}" if peer else "")
                + "</div>"
            )
        clean_rows.append(
            "<tr class='ok'>"
            "<td>{system}</td>"
            "<td><code>{serial}</code><div class='sub'>{name}</div>{topo}</td>"
            "<td>{model}</td>"
            "<td>{state}</td>"
            "<td class='muted'>—</td>"
            "<td class='muted'>no risk attributes · clean</td>"
            "</tr>".format(
                system=html.escape(c.system),
                serial=html.escape(c.serial or c.name),
                name=html.escape(c.name),
                topo=topo_sub,
                model=html.escape(c.model),
                state=html.escape(c.state),
            )
        )

    table_head = """
    <colgroup>
      <col class="c-host"/>
      <col class="c-serial"/>
      <col class="c-model"/>
      <col class="c-smart"/>
      <col class="c-scr"/>
      <col class="c-attrs"/>
    </colgroup>
    <thead>
      <tr>
        <th>Host</th>
        <th>Serial / device</th>
        <th>Model</th>
        <th>SMART</th>
        <th>Scrutiny</th>
        <th>Risk attributes &amp; trend</th>
      </tr>
    </thead>"""

    product = html.escape(PRODUCT_NAME)
    topo_html = _render_topology_view(report)
    mapped = int(report.get("topology_mapped") or 0)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{product}</title>
<link rel="icon" type="image/png" sizes="96x96" href="/branding/favicon-96x96.png"/>
<link rel="icon" href="/branding/favicon.ico"/>
<style>
:root {{
  --ink: #121718;
  --teal: #007d8a;
  --paper: #f5f2ea;
  --bg: var(--paper);
  --card: #fffcf6;
  --text: var(--ink);
  --muted: #536264;
  --crit: #c23b2e;
  --warn: #b07a00;
  --ok: #2f7a55;
  --grow: #c44a32;
  --line: rgba(18, 23, 24, 0.14);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font: 14px/1.45 ui-sans-serif, system-ui, sans-serif;
  color: var(--text);
  padding: 1.5rem;
  background:
    radial-gradient(circle at 16% 12%, rgba(0, 125, 138, 0.16), transparent 29rem),
    linear-gradient(135deg, #ece9df, var(--paper) 56%, #d9ebe9);
  min-height: 100vh;
}}
.brand {{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: .55rem;
  margin-bottom: 1rem;
}}
.brand .logo {{
  display: block;
  width: 11rem;
  height: 2.75rem;
  {logo_css}
}}
.brand .product {{
  margin: 0;
  color: var(--muted);
  font-size: .75rem;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}}
.meta {{ color: var(--muted); margin: 0 0 1.25rem; }}
.meta a {{ color: var(--teal); font-weight: 600; }}
h2 {{ font-size: 1.05rem; margin: 1.5rem 0 .6rem; }}
table.disk-table {{
  width: 100%;
  border-collapse: collapse;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
  table-layout: fixed;
}}
table.disk-table col.c-host {{ width: 12%; }}
table.disk-table col.c-serial {{ width: 18%; }}
table.disk-table col.c-model {{ width: 18%; }}
table.disk-table col.c-smart {{ width: 8%; }}
table.disk-table col.c-scr {{ width: 8%; }}
table.disk-table col.c-attrs {{ width: 36%; }}
table.disk-table th, table.disk-table td {{
  padding: .55rem .65rem;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--line);
  word-break: break-word;
}}
table.disk-table th {{
  background: rgba(0,125,138,.08);
  font-size: .78rem;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted);
}}
tr.critical td:first-child, tr.growing td:first-child {{ box-shadow: inset 3px 0 0 var(--crit); }}
tr.warn td:first-child {{ box-shadow: inset 3px 0 0 var(--warn); }}
tr.ok td:first-child {{ box-shadow: inset 3px 0 0 var(--ok); }}
.sub {{ color: var(--muted); font-size: .8rem; margin-top: .15rem; }}
code {{ font-size: .85rem; }}
.muted {{ color: var(--muted); }}
table.attrs {{
  width: 100%;
  border-collapse: collapse;
  font-size: .82rem;
  table-layout: fixed;
}}
table.attrs col.c-a-name {{ width: 22%; }}
table.attrs col.c-a-nu {{ width: 12%; }}
table.attrs col.c-a-bas {{ width: 14%; }}
table.attrs col.c-a-dtot {{ width: 12%; }}
table.attrs col.c-a-dprev {{ width: 12%; }}
table.attrs col.c-a-trend {{ width: 16%; }}
table.attrs col.c-a-hist {{ width: 12%; }}
table.attrs th, table.attrs td {{
  padding: .25rem .35rem;
  border: none;
  border-bottom: 1px solid rgba(18,23,24,.06);
  vertical-align: middle;
  overflow: hidden;
  text-overflow: ellipsis;
}}
table.attrs th.num, table.attrs td.num {{
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}
table.attrs th.trend, table.attrs td.trend,
table.attrs th.hist, table.attrs td.hist {{
  text-align: center;
  white-space: nowrap;
}}
table.attrs th.attr-name, table.attrs td.attr-name {{
  text-align: left;
  white-space: nowrap;
}}
.chip {{
  display: inline-block; padding: .1rem .4rem; border-radius: 999px;
  font-size: .72rem; font-weight: 600; cursor: help; }}
.chip.critical, .chip.growing {{ background: rgba(194,59,46,.12); color: var(--crit); }}
.chip.warn {{ background: rgba(176,122,0,.14); color: #8a5f00; }}
.chip.info {{ background: rgba(0,125,138,.1); color: #005f69; }}
.chip.ok {{ background: rgba(47,122,85,.12); color: #2f7a55; }}
.chip.kind {{ background: rgba(0,125,138,.12); color: #005f69; text-transform: uppercase; letter-spacing: .04em; }}
.badge {{ display: inline-block; margin-left: .35rem; padding: .05rem .35rem; border-radius: 6px;
  font-size: .68rem; font-weight: 700; letter-spacing: .03em; vertical-align: middle; cursor: help; }}
.badge.grow, .badge.growing {{ background: rgba(196,74,50,.18); color: #8a3222; }}
.badge.stable {{ background: rgba(47,122,85,.14); color: #2f7a55; }}
.badge.base {{ background: rgba(0,125,138,.12); color: #005f69; }}
.hist-btn {{
  margin: 0; border: 1px solid var(--line); background: rgba(0,125,138,.08);
  color: var(--teal); font: inherit; font-size: .68rem; font-weight: 700;
  padding: .12rem .4rem; border-radius: 6px; cursor: pointer;
}}
.hist-btn:hover {{ background: rgba(0,125,138,.16); }}
.hist-modal {{
  display: none; position: fixed; inset: 0; z-index: 40;
  background: rgba(18,23,24,.45); align-items: center; justify-content: center; padding: 1rem;
}}
.hist-modal.open {{ display: flex; }}
.hist-card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  max-width: 36rem; width: 100%; max-height: 90vh; overflow: auto; padding: 1rem 1.1rem;
  box-shadow: 0 12px 40px rgba(0,0,0,.18);
}}
.hist-card h3 {{ margin: 0 0 .35rem; font-size: 1.05rem; }}
.hist-card .meta {{ margin: 0 0 .75rem; }}
.hist-card table {{ width: 100%; border-collapse: collapse; font-size: .85rem; margin-top: .75rem; }}
.hist-card th, .hist-card td {{ text-align: left; padding: .3rem .35rem; border-bottom: 1px solid var(--line); }}
.hist-card th {{ color: var(--muted); font-size: .72rem; text-transform: uppercase; }}
.hist-card .grow-row {{ color: var(--grow); font-weight: 600; }}
.hist-close {{
  float: right; border: 0; background: transparent; font-size: 1.2rem; cursor: pointer; color: var(--muted);
}}
.spark {{ display: block; margin: .4rem 0; background: rgba(0,125,138,.04); border-radius: 8px; }}
.view-toggle {{
  display: inline-flex; gap: .35rem; margin: 0 0 1rem; padding: .2rem;
  background: rgba(0,125,138,.08); border-radius: 999px; border: 1px solid var(--line);
}}
.view-toggle button {{
  border: 0; background: transparent; color: var(--muted); font: inherit; font-weight: 600;
  font-size: .82rem; padding: .35rem .9rem; border-radius: 999px; cursor: pointer;
}}
.view-toggle button.active {{ background: var(--card); color: var(--teal); box-shadow: 0 1px 2px rgba(0,0,0,.06); }}
.view-panel {{ display: none; }}
.view-panel.active {{ display: block; }}
.sub.topo {{ color: #005f69; }}
.topo-host {{ margin: 1rem 0 1.5rem; }}
.topo-host h3 {{ margin: 0 0 .6rem; font-size: 1rem; color: var(--teal); }}
.topo-pool {{
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: .75rem 1rem; margin-bottom: .75rem;
}}
.topo-pool-h {{ margin-bottom: .5rem; }}
.topo-vdev {{ margin: .55rem 0 .35rem .5rem; padding-left: .75rem; border-left: 3px solid rgba(0,125,138,.35); }}
.topo-vdev-h {{ font-size: .8rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-bottom: .25rem; }}
.topo-vdev ul, .topo-pool > ul {{ list-style: none; margin: 0; padding: 0; }}
.topo-vdev li, .topo-pool > ul > li {{ padding: .35rem 0; border-bottom: 1px solid rgba(18,23,24,.06); }}
.topo-vdev li:last-child {{ border-bottom: 0; }}
.legend {{ margin-top: 1rem; color: var(--muted); font-size: .85rem; max-width: 70rem; }}
.legend strong {{ color: var(--text); }}
@media (max-width: 30rem) {{
  .brand .logo {{ width: 9rem; height: 2.25rem; }}
}}
</style>
</head>
<body>
  <header class="brand">
    {brand_link}
    <p class="product">{product}</p>
  </header>
  <p class="meta">
    {html.escape(report['generated'])} ·
    {report['total_devices']} disks ·
    {len(report['risks'])} with findings ·
    <strong>{report.get('growing_count', 0)} growing</strong> ·
    {report['clean']} clean ·
    Scrutiny status≥2: {report['scrutiny_flagged']} ·
    topology mapped: {mapped}<br/>
    Source: Beszel attributes{(' + Scrutiny' if SCRUTINY_URL else '')} ·
    <a href="/json">JSON</a> ·
    <a href="/text">text</a> ·
    <a href="/history">history</a>
  </p>

  <div class="view-toggle" role="tablist" aria-label="View mode">
    <button type="button" class="active" data-view="list" aria-selected="true">Disk list</button>
    <button type="button" data-view="topology" aria-selected="false">Topology</button>
  </div>

  <div id="view-list" class="view-panel active">
  <h2>Risk disks</h2>
  <table class="disk-table">
    {table_head}
    <tbody>
      {''.join(risk_rows) if risk_rows else '<tr><td colspan="6">No risk findings.</td></tr>'}
    </tbody>
  </table>

  <h2>Clean disks</h2>
  <table class="disk-table">
    {table_head}
    <tbody>
      {''.join(clean_rows) if clean_rows else '<tr><td colspan="6">None.</td></tr>'}
    </tbody>
  </table>

  <p class="legend">
    <strong>How to read the trend columns:</strong>
    each risk attribute has <em>Now</em>, <em>Baseline</em>, <em>Δ tot</em>, <em>Δ last</em>, and
    <em>Trend</em> (<span class="badge base">baseline</span> =
    first sample,
    <span class="badge stable">stable</span> =
    same as baseline,
    <span class="badge grow">GROWING</span> =
    acute — the value increased).<br/><br/>
    <strong>Critical:</strong> Realloc / Pending / OfflineUnc / GrownDefect etc. —
    <strong>Warn:</strong> UDMA_CRC (cable/HBA), MultiZone —
    Seagate Raw_Read / Seek are flagged only if norm ≤ thresh.<br/>
    History: <code>{html.escape(str(report.get('history_path') or ''))}</code>.
    Topology dir: <code>{html.escape(str(report.get('topology_path') or ''))}</code>.
  </p>
  </div>

  <div id="view-topology" class="view-panel">
    <h2>Pool topology</h2>
    <p class="meta">ZFS vdevs and mergerfs/btrfs/snapraid roles — same SMART risk chips, grouped how disks belong together.</p>
    {topo_html}
  </div>

  <div id="hist-modal" class="hist-modal" role="dialog" aria-modal="true" aria-labelledby="hist-title" hidden>
    <div class="hist-card">
      <button type="button" class="hist-close" id="hist-close" aria-label="Close">×</button>
      <h3 id="hist-title">History</h3>
      <p class="meta" id="hist-meta"></p>
      <div id="hist-spark"></div>
      <table>
        <thead><tr><th>When</th><th>Value</th><th>Δ</th><th></th></tr></thead>
        <tbody id="hist-body"></tbody>
      </table>
    </div>
  </div>

  <script>
  (function () {{
    var buttons = document.querySelectorAll('.view-toggle button');
    var key = 'diskrisk-view';
    function show(name) {{
      document.querySelectorAll('.view-panel').forEach(function (el) {{
        el.classList.toggle('active', el.id === 'view-' + name);
      }});
      buttons.forEach(function (btn) {{
        var on = btn.getAttribute('data-view') === name;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
      }});
      try {{ localStorage.setItem(key, name); }} catch (e) {{}}
    }}
    buttons.forEach(function (btn) {{
      btn.addEventListener('click', function () {{ show(btn.getAttribute('data-view')); }});
    }});
    var saved = null;
    try {{ saved = localStorage.getItem(key); }} catch (e) {{}}
    if (saved === 'topology' || saved === 'list') show(saved);

    var modal = document.getElementById('hist-modal');
    var title = document.getElementById('hist-title');
    var meta = document.getElementById('hist-meta');
    var spark = document.getElementById('hist-spark');
    var body = document.getElementById('hist-body');
    function closeHist() {{
      modal.classList.remove('open');
      modal.hidden = true;
    }}
    document.getElementById('hist-close').addEventListener('click', closeHist);
    modal.addEventListener('click', function (e) {{ if (e.target === modal) closeHist(); }});
    document.addEventListener('keydown', function (e) {{ if (e.key === 'Escape') closeHist(); }});

    function sparkSvg(samples) {{
      var vals = [];
      for (var i = 0; i < samples.length; i++) {{
        var n = Number(samples[i].value);
        if (!isNaN(n)) vals.push(n);
      }}
      if (!vals.length) return '';
      if (vals.length === 1) vals.push(vals[0]);
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      var span = Math.max(hi - lo, 1), w = 280, h = 56, pad = 4, pts = [];
      for (var j = 0; j < vals.length; j++) {{
        var x = pad + (w - 2 * pad) * (j / (vals.length - 1));
        var y = h - pad - (h - 2 * pad) * ((vals[j] - lo) / span);
        pts.push(x.toFixed(1) + ',' + y.toFixed(1));
      }}
      var last = pts[pts.length - 1].split(',');
      return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '">' +
        '<polyline fill="none" stroke="#007d8a" stroke-width="2" points="' + pts.join(' ') + '"/>' +
        '<circle cx="' + last[0] + '" cy="' + last[1] + '" r="3" fill="#c44a32"/></svg>';
    }}

    function openHist(data) {{
      title.textContent = (data.short || data.attr) + ' — ' + (data.serial || data.device || '');
      meta.innerHTML = (data.system || '') +
        (data.first_seen ? ' · first seen <strong>' + data.first_seen + '</strong>' : '') +
        ' · now <strong>' + data.value + '</strong>' +
        (data.delta_total != null ? ' · Δ tot <strong>' + (data.delta_total > 0 ? '+' : '') + data.delta_total + '</strong>' : '') +
        '<br/>' + (data.help || '');
      var samples = data.samples || [];
      spark.innerHTML = sparkSvg(samples);
      var rows = '';
      var prev = null;
      for (var i = 0; i < samples.length; i++) {{
        var s = samples[i];
        var v = Number(s.value);
        var dlt = (prev != null && !isNaN(v)) ? (v - prev) : null;
        var grow = dlt != null && dlt > 0;
        var label = grow ? 'grew' : (dlt === 0 ? 'same' : (i === 0 ? 'baseline' : ''));
        rows += '<tr class="' + (grow ? 'grow-row' : '') + '">' +
          '<td>' + (s.ts || '') + '</td>' +
          '<td>' + s.value + '</td>' +
          '<td>' + (dlt == null ? '—' : (dlt > 0 ? '+' : '') + dlt) + '</td>' +
          '<td>' + label + '</td></tr>';
        if (!isNaN(v)) prev = v;
      }}
      body.innerHTML = rows || '<tr><td colspan="4">No samples yet.</td></tr>';
      modal.hidden = false;
      modal.classList.add('open');
    }}

    document.querySelectorAll('.hist-btn').forEach(function (btn) {{
      btn.addEventListener('click', function () {{
        try {{ openHist(JSON.parse(btn.getAttribute('data-hist'))); }}
        catch (err) {{ console.error(err); }}
      }});
    }});
  }})();
  </script>
</body>
</html>
"""



class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, ctype: str, payload: bytes, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path.startswith("/branding/"):
            name = Path(path).name
            # Allow simple asset names only (no path traversal).
            if name != Path(name).name or ".." in name or "/" in name or "\\" in name:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
                return
            if Path(name).suffix.lower() not in {".svg", ".png", ".ico", ".webp"}:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
                return
            candidates = [
                BRANDING_DIR / name,
                _REPO_ROOT / "branding" / name,
            ]
            for fp in candidates:
                if fp.is_file():
                    data = fp.read_bytes()
                    ctype = {
                        ".svg": "image/svg+xml",
                        ".png": "image/png",
                        ".ico": "image/x-icon",
                    }.get(fp.suffix.lower(), "application/octet-stream")
                    self._send(200, ctype, data, cache="public, max-age=86400")
                    return
            self._send(404, "text/plain; charset=utf-8", b"branding missing\n")
            return

        if path in ("/history", "/history.json"):
            qs = parse_qs(urlparse(self.path).query)
            serial_q = (qs.get("serial") or [""])[0].strip()
            attr_q = (qs.get("attr") or [""])[0].strip()
            hist = load_history()
            attrs = hist.get("attrs") or {}
            items = []
            for key, entry in attrs.items():
                if not isinstance(entry, dict):
                    continue
                if serial_q and _norm_serial(str(entry.get("serial") or "")) != _norm_serial(serial_q):
                    if serial_q not in str(entry.get("serial") or "") and serial_q not in key:
                        continue
                if attr_q and str(entry.get("attr") or "") != attr_q and attr_q not in key:
                    continue
                samples = entry.get("samples") or []
                items.append(
                    {
                        "key": key,
                        "system": entry.get("system"),
                        "serial": entry.get("serial"),
                        "device": entry.get("device"),
                        "attr": entry.get("attr"),
                        "short": SHORT.get(str(entry.get("attr") or ""), entry.get("attr")),
                        "help": _attr_help(str(entry.get("attr") or "")),
                        "first_seen": entry.get("first_seen"),
                        "first_value": entry.get("first_value"),
                        "last_seen": entry.get("last_seen"),
                        "last_value": entry.get("last_value"),
                        "samples": samples,
                    }
                )
            items.sort(key=lambda x: (str(x.get("system") or ""), str(x.get("serial") or ""), str(x.get("attr") or "")))
            payload = {
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "history_path": str(HISTORY_PATH),
                "count": len(items),
                "items": items,
            }
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(payload, indent=2).encode(),
            )
            return

        try:
            report = build_report()
        except Exception as exc:
            self._send(500, "text/plain; charset=utf-8", f"error: {exc}\n".encode())
            return

        if path in ("/text", "/raw", "/report.txt"):
            self._send(200, "text/plain; charset=utf-8", render_text(report).encode())
        elif path in ("/json", "/report.json"):
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(serialize_report(report), indent=2).encode(),
            )
        else:
            self._send(200, "text/html; charset=utf-8", render_html(report).encode())


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help"):
        print(
            f"Diskrisk {__version__}\n"
            "Usage: smart_risk.py [--once|text|json]\n"
            "       smart_risk.py --version\n"
            "Env/config: see config.example.env / docs/SMART-risk-manual.md"
        )
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-V", "--version", "version"):
        print(f"diskrisk {__version__}")
        return
    if _CONFIG_PATH is not None:
        print(f"diskrisk config: {_CONFIG_PATH}", flush=True)
    if len(sys.argv) > 1 and sys.argv[1] in ("--once", "once", "text"):
        print(render_text(build_report()))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "json":
        print(json.dumps(serialize_report(build_report()), indent=2))
        return

    host, _, port_s = LISTEN.partition(":")
    port = int(port_s or "8091")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", port), Handler)
    print(f"diskrisk {__version__} listening on http://{host or '0.0.0.0'}:{port}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
