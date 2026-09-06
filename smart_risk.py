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

__version__ = "1.2.2"

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


@dataclass
class DiskRisk:
    system: str
    name: str
    serial: str
    model: str
    state: str
    findings: list[Finding] = field(default_factory=list)
    scrutiny_status: int | None = None

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
            scrutiny_status=scr.get(serial),
        )
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


def _finding_attr_row(f: Finding) -> str:
    """HTML row for one risk attribute with visible trend columns."""
    short = html.escape(SHORT.get(f.name, f.name))
    title = html.escape(
        f.name
        + (f" — {f.note}" if f.note else "")
        + (f" · first_seen={f.first_seen}" if f.first_seen else "")
    )
    trend = _trend_label(f)
    trend_cls = {"GROWING": "growing", "stable": "stable", "baseline": "base"}[trend]
    lvl = "growing" if f.growing else f.level
    return (
        f'<tr class="attr {lvl}" title="{title}">'
        f'<td class="attr-name"><span class="chip {lvl}">{short}</span></td>'
        f'<td class="num">{html.escape(str(f.value))}</td>'
        f'<td class="num">{html.escape(str(f.first_value if f.first_value is not None else "—"))}</td>'
        f'<td class="num">{html.escape(_fmt_delta(f.delta_total))}</td>'
        f'<td class="num">{html.escape(_fmt_delta(f.delta_prev))}</td>'
        f'<td><span class="badge {trend_cls}">{trend}</span></td>'
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
    }


def _serialize_clean(c: CleanDisk) -> dict[str, Any]:
    return {
        "system": c.system,
        "name": c.name,
        "serial": c.serial,
        "model": c.model,
        "state": c.state,
    }


def serialize_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated": report["generated"],
        "total_devices": report["total_devices"],
        "clean": report["clean"],
        "growing_count": report.get("growing_count", 0),
        "scrutiny_flagged": report["scrutiny_flagged"],
        "history_path": report.get("history_path"),
        "risks": [_serialize_risk(r) for r in report["risks"]],
        "clean_disks": [_serialize_clean(c) for c in report.get("clean_disks") or []],
    }


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
                "</colgroup><thead><tr>"
                "<th>Attribute</th><th>Now</th><th>Baseline</th>"
                "<th>Δ tot</th><th>Δ last</th><th>Trend</th>"
                "</tr></thead><tbody>"
                + "".join(_finding_attr_row(f) for f in r.findings)
                + "</tbody></table>"
            )
        elif (r.scrutiny_status or 0) >= 2:
            attr_table = '<span class="chip critical">Scrutiny flagged</span>'
        else:
            attr_table = '<span class="muted">—</span>'
        scr = "—" if r.scrutiny_status is None else str(r.scrutiny_status)
        badge = ' <span class="badge grow">GROWING</span>' if r.any_growing else ""
        if (not r.any_growing) and any(
            f.samples >= 2 and (f.delta_total or 0) == 0 for f in r.findings
        ):
            badge += ' <span class="badge stable">stable</span>'
        elif (not r.any_growing) and r.findings and all(f.samples < 2 for f in r.findings):
            badge += ' <span class="badge base">baseline</span>'
        risk_rows.append(
            "<tr class='{sev}'>"
            "<td>{system}{badge}</td>"
            "<td><code>{serial}</code><div class='sub'>{name}</div></td>"
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
                model=html.escape(r.model),
                state=html.escape(r.state),
                scr=html.escape(scr),
                attrs=attr_table,
            )
        )

    clean_rows = []
    for c in report.get("clean_disks") or []:
        clean_rows.append(
            "<tr class='ok'>"
            "<td>{system}</td>"
            "<td><code>{serial}</code><div class='sub'>{name}</div></td>"
            "<td>{model}</td>"
            "<td>{state}</td>"
            "<td class='muted'>—</td>"
            "<td class='muted'>no risk attributes · clean</td>"
            "</tr>".format(
                system=html.escape(c.system),
                serial=html.escape(c.serial or c.name),
                name=html.escape(c.name),
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
}}
table.attrs th, table.attrs td {{
  padding: .2rem .3rem;
  border: none;
  border-bottom: 1px solid rgba(18,23,24,.06);
}}
table.attrs .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.chip {{
  display: inline-block; padding: .1rem .4rem; border-radius: 999px;
  font-size: .72rem; font-weight: 600; }}
.chip.critical, .chip.growing {{ background: rgba(194,59,46,.12); color: var(--crit); }}
.chip.warn {{ background: rgba(176,122,0,.14); color: #8a5f00; }}
.chip.info {{ background: rgba(0,125,138,.1); color: #005f69; }}
.badge {{ display: inline-block; margin-left: .35rem; padding: .05rem .35rem; border-radius: 6px;
  font-size: .68rem; font-weight: 700; letter-spacing: .03em; vertical-align: middle; }}
.badge.grow, .badge.growing {{ background: rgba(196,74,50,.18); color: #8a3222; }}
.badge.stable {{ background: rgba(47,122,85,.14); color: #2f7a55; }}
.badge.base {{ background: rgba(0,125,138,.12); color: #005f69; }}
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
    Scrutiny status≥2: {report['scrutiny_flagged']}<br/>
    Source: Beszel attributes{(' + Scrutiny' if SCRUTINY_URL else '')} ·
    <a href="/json">JSON</a> ·
    <a href="/text">text</a>
  </p>

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
  </p>
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
