#!/usr/bin/env python3
"""Diskrisk risk engines: classic (raw>0 maps) vs hybrid/policy.

smartmontools / Beszel decode attribute *names* and RAW values.
This module decides which signals are media risk vs interface vs advisory.

Engines (DISKRISK_RISK_ENGINE):
  classic — current behaviour (default on production)
  hybrid  — policy: MultiZone advisory, UDMA_CRC interface, media counters critical;
            after trend, demote scars / promote correlated GROWING advisory
  stats   — hybrid + prefer ATA Device Statistics overlays from smart_collect.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# Interface scars (UDMA_CRC, …): stable this many days → historical, not a fault.
# Re-read via _interface_historical_days() so config.env loaded after import still applies.
_DEFAULT_INTERFACE_HISTORICAL_DAYS = 14
# Media scars (GrownDefect / Realloc): longer window — intermittent growth is common.
_DEFAULT_MEDIA_SCAR_HISTORICAL_DAYS = 30


def _env_days(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def interface_historical_days() -> int:
    """Days without UDMA/interface increase before scar is historical (config)."""
    return _env_days(
        "DISKRISK_INTERFACE_HISTORICAL_DAYS", _DEFAULT_INTERFACE_HISTORICAL_DAYS
    )


def media_scar_historical_days() -> int:
    """Days without GrownDefect/Realloc increase before scar is historical (config)."""
    return _env_days(
        "DISKRISK_MEDIA_SCAR_HISTORICAL_DAYS", _DEFAULT_MEDIA_SCAR_HISTORICAL_DAYS
    )


# Back-compat aliases used inside this module
_interface_historical_days = interface_historical_days
_media_scar_historical_days = media_scar_historical_days


def policy_settings() -> dict[str, Any]:
    """Current risk-policy knobs (from env / config file)."""
    return {
        "engine": (os.environ.get("DISKRISK_RISK_ENGINE") or "classic").strip().lower(),
        "interface_historical_days": interface_historical_days(),
        "media_scar_historical_days": media_scar_historical_days(),
    }


# Media defects — actionable when raw >= threshold (usually 1).
MEDIA_CRITICAL = {
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
    # ATA Device Statistics (when surfaced as named attrs / overlay)
    "Pending_Error_Count": 1,
    "Number_of_Reallocated_Logical_Sectors": 1,
    "Number_of_Reallocation_Candidate_Logical_Sectors": 1,
    "Number_of_Reported_Uncorrectable_Errors": 1,
    "Number_of_Mechanical_Start_Failures": 1,
}

# Prefer these DevStat names over classic SMART IDs when both present (stats engine).
DEVSTAT_PREFER = {
    "Pending_Error_Count": {"Current_Pending_Sector"},
    "Number_of_Reallocation_Candidate_Logical_Sectors": {"Current_Pending_Sector"},
    "Number_of_Reallocated_Logical_Sectors": {"Reallocated_Sector_Ct", "Reallocated_Event_Count"},
    "Number_of_Reported_Uncorrectable_Errors": {
        "Offline_Uncorrectable",
        "Reported_Uncorrect",
    },
}

# Interface / path — not “dying platter”.
INTERFACE_WARN = {
    "UDMA_CRC_Error_Count": 1,
    "Command_Timeout": 1,
}

# Vendor advisory counters — not FAIL by themselves.
ADVISORY = {
    "Multi_Zone_Error_Rate": 1,
}

# Seagate: only when normalized value has breached manufacturer threshold.
NORM_THRESHOLD_ATTRS = {
    "Raw_Read_Error_Rate",
    "Seek_Error_Rate",
}

# Classic maps (legacy engine) — same as pre-1.4 Diskrisk.
CLASSIC_CRITICAL = dict(MEDIA_CRITICAL)
# Remove DevStat-only keys from classic (Beszel rarely sends them as SMART attrs).
for _k in (
    "Pending_Error_Count",
    "Number_of_Reallocated_Logical_Sectors",
    "Number_of_Reallocation_Candidate_Logical_Sectors",
    "Number_of_Reported_Uncorrectable_Errors",
    "Number_of_Mechanical_Start_Failures",
):
    CLASSIC_CRITICAL.pop(_k, None)

CLASSIC_WARN = {
    "UDMA_CRC_Error_Count": 1,
    "Multi_Zone_Error_Rate": 1,
    "Command_Timeout": 1,
}

MEDIA_PEER_NAMES = frozenset(
    {
        "Reallocated_Sector_Ct",
        "Current_Pending_Sector",
        "Offline_Uncorrectable",
        "Reported_Uncorrect",
        "GrownDefectList",
        "Pending_Error_Count",
        "Number_of_Reallocated_Logical_Sectors",
        "Number_of_Reallocation_Candidate_Logical_Sectors",
        "Number_of_Reported_Uncorrectable_Errors",
    }
)

# Stable non-zero counters that may date from early life — demote after N days.
# Pending / OfflineUnc stay critical even when "stable" (acute media risk).
MEDIA_SCAR_NAMES = frozenset(
    {
        "GrownDefectList",
        "Reallocated_Sector_Ct",
        "Reallocated_Event_Count",
        "Number_of_Reallocated_Logical_Sectors",
    }
)

# For MultiZone correlation: only acute / still-growing media counts.
MEDIA_ACUTE_NAMES = frozenset(
    {
        "Current_Pending_Sector",
        "Offline_Uncorrectable",
        "Reported_Uncorrect",
        "Pending_Error_Count",
        "Number_of_Reallocation_Candidate_Logical_Sectors",
        "Number_of_Reported_Uncorrectable_Errors",
    }
)


@dataclass
class EvalFinding:
    level: str  # critical | warn | info
    name: str
    value: Any
    note: str = ""
    kind: str = "media"  # media | interface | advisory | status
    source: str = "smart"  # smart | devstat | status | selftest | errorlog


def _raw_int(attr: dict) -> int | None:
    for key in ("rv", "raw", "raw_value", "value"):
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


def _merge_attr_lists(*lists: list[dict] | None) -> list[dict]:
    """Merge attribute dicts by name; later lists win on conflict."""
    by_name: dict[str, dict] = {}
    for lst in lists:
        for a in lst or []:
            name = a.get("n") or a.get("name") or ""
            if not name:
                continue
            by_name[name] = a
    return list(by_name.values())


def classic_evaluate(attrs: list[dict]) -> list[EvalFinding]:
    """Original Diskrisk behaviour: raw>=1 on CRITICAL/WARN maps."""
    findings: list[EvalFinding] = []
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
                            EvalFinding(
                                "critical",
                                name,
                                f"norm={v} thresh={t} raw={raw}",
                                "normalized value at/under threshold",
                                kind="media",
                                source="smart",
                            )
                        )
                except (TypeError, ValueError):
                    pass
            continue

        if name in CLASSIC_CRITICAL and raw is not None and raw >= CLASSIC_CRITICAL[name]:
            findings.append(EvalFinding("critical", name, raw, kind="media", source="smart"))
            continue

        if name in CLASSIC_WARN and raw is not None and raw >= CLASSIC_WARN[name]:
            note = "often cabling/HBA" if name == "UDMA_CRC_Error_Count" else ""
            kind = "interface" if name == "UDMA_CRC_Error_Count" else "advisory"
            findings.append(EvalFinding("warn", name, raw, note, kind=kind, source="smart"))
            continue

        wf = (a.get("wf") or a.get("when_failed") or "").strip()
        if wf:
            findings.append(
                EvalFinding(
                    "critical",
                    name,
                    raw if raw is not None else wf,
                    f"when_failed={wf}",
                    kind="status",
                    source="status",
                )
            )

    return findings


def hybrid_evaluate(
    attrs: list[dict],
    *,
    smart_extra: dict[str, Any] | None = None,
    prefer_devstat: bool = False,
) -> list[EvalFinding]:
    """Policy engine: media vs interface vs advisory; optional DevStat overlay."""
    extra_attrs: list[dict] = []
    status_findings: list[EvalFinding] = []

    if smart_extra:
        # Overall SMART health from collector / enriched payload
        st = (smart_extra.get("smart_status") or smart_extra.get("state") or "").upper()
        if st in ("FAILED", "FAILING"):
            status_findings.append(
                EvalFinding(
                    "critical",
                    "SMART_Status",
                    st,
                    "drive reports SMART FAILED",
                    kind="status",
                    source="status",
                )
            )
        for a in smart_extra.get("devstat_attrs") or []:
            extra_attrs.append(a)
        if smart_extra.get("selftest_failed"):
            status_findings.append(
                EvalFinding(
                    "critical",
                    "Self_Test",
                    smart_extra.get("selftest_status") or "FAILED",
                    "SMART self-test log reports failure",
                    kind="status",
                    source="selftest",
                )
            )
        err_n = _as_int(smart_extra.get("ata_error_count"))
        if err_n is not None and err_n > 0:
            status_findings.append(
                EvalFinding(
                    "warn",
                    "ATA_Error_Log",
                    err_n,
                    "ATA error log has entries",
                    kind="media",
                    source="errorlog",
                )
            )

    merged = _merge_attr_lists(attrs, extra_attrs)
    if prefer_devstat:
        present = {(a.get("n") or a.get("name") or "") for a in merged}
        drop: set[str] = set()
        for dev_name, classic_names in DEVSTAT_PREFER.items():
            if dev_name in present:
                drop |= classic_names
        if drop:
            merged = [
                a
                for a in merged
                if (a.get("n") or a.get("name") or "") not in drop
            ]

    findings: list[EvalFinding] = list(status_findings)

    for a in merged:
        name = a.get("n") or a.get("name") or ""
        if not name:
            continue
        raw = _raw_int(a)
        v = a.get("v")
        t = a.get("t")
        src = a.get("source") or ("devstat" if name in MEDIA_CRITICAL and name.startswith(
            ("Pending_", "Number_of_")
        ) else "smart")

        if name in NORM_THRESHOLD_ATTRS:
            if v is not None and t is not None:
                try:
                    if int(v) <= int(t):
                        findings.append(
                            EvalFinding(
                                "critical",
                                name,
                                f"norm={v} thresh={t} raw={raw}",
                                "prefail: normalized value at/under manufacturer threshold",
                                kind="media",
                                source="smart",
                            )
                        )
                except (TypeError, ValueError):
                    pass
            continue

        wf = (a.get("wf") or a.get("when_failed") or "").strip()
        if wf:
            findings.append(
                EvalFinding(
                    "critical",
                    name,
                    raw if raw is not None else wf,
                    f"when_failed={wf}",
                    kind="status",
                    source="status",
                )
            )
            continue

        if name in MEDIA_CRITICAL and raw is not None and raw >= MEDIA_CRITICAL[name]:
            findings.append(
                EvalFinding("critical", name, raw, kind="media", source=str(src))
            )
            continue

        if name in INTERFACE_WARN and raw is not None and raw >= INTERFACE_WARN[name]:
            note = "interface/path — reseat cable/HBA before condemning the disk"
            findings.append(
                EvalFinding("warn", name, raw, note, kind="interface", source="smart")
            )
            continue

        if name in ADVISORY and raw is not None and raw >= ADVISORY[name]:
            # Scar → info; GROWING + media peer promoted later in refine_after_trend
            findings.append(
                EvalFinding(
                    "info",
                    name,
                    raw,
                    "advisory counter (not FAIL by itself)",
                    kind="advisory",
                    source="smart",
                )
            )
            continue

    return findings


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _days_since_last_increase(f: Any, *, now: datetime | None = None) -> float | None:
    """Days since the counter last increased (or since first sample if never grew)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    samples = list(getattr(f, "history", None) or [])
    last_increase_at: datetime | None = None
    first_ts: datetime | None = None
    prev_val: int | None = None
    for s in samples:
        if not isinstance(s, dict):
            continue
        ts = _parse_ts(s.get("ts"))
        try:
            val = int(str(s.get("value")).split()[0])
        except (TypeError, ValueError, AttributeError):
            continue
        if first_ts is None and ts is not None:
            first_ts = ts
        if prev_val is not None and val > prev_val and ts is not None:
            last_increase_at = ts
        prev_val = val

    anchor = last_increase_at or first_ts or _parse_ts(getattr(f, "first_seen", None) or "")
    if anchor is None:
        return None
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0.0, (now - anchor).total_seconds() / 86400.0)


def refine_after_trend(findings: list[Any], *, now: datetime | None = None) -> None:
    """Mutate findings after history: MultiZone / UDMA / GrownDefect scar policy.

    Accepts EvalFinding or smart_risk.Finding (duck-typed).
    """
    # MultiZone correlation: Pending/OfflineUnc, or GROWING realloc/grown — not old scars.
    media_present = False
    for f in findings:
        name = getattr(f, "name", "")
        growing = bool(getattr(f, "growing", False))
        level = getattr(f, "level", "")
        if level != "critical":
            continue
        if name in MEDIA_ACUTE_NAMES:
            media_present = True
            break
        if name in MEDIA_SCAR_NAMES and growing:
            media_present = True
            break

    for f in findings:
        name = getattr(f, "name", "")
        growing = bool(getattr(f, "growing", False))
        kind = getattr(f, "kind", "") or ""
        note = getattr(f, "note", "") or ""

        if name == "Multi_Zone_Error_Rate" or kind == "advisory":
            if growing and media_present:
                f.level = "warn"
                if "correlated" not in note:
                    f.note = (note + "; " if note else "") + "GROWING with media peer"
            elif growing and not media_present:
                f.level = "info"
                if "advisory growing" not in note:
                    f.note = (note + "; " if note else "") + "advisory growing (no media peer)"
            else:
                f.level = "info"

        if name in INTERFACE_WARN or kind == "interface":
            hist_days = _interface_historical_days()
            days = _days_since_last_increase(f, now=now)
            try:
                f.scar_need_days = hist_days
                f.stable_days = int(days) if days is not None else None
            except (TypeError, AttributeError):
                pass
            if growing:
                f.level = "warn"
                if "path noise" not in note:
                    f.note = (note + "; " if note else "") + "GROWING interface/path noise"
            elif days is not None and days >= hist_days:
                f.level = "info"
                hist_note = f"historical interface scar (no increase ≥{hist_days}d)"
                if "historical interface" not in note:
                    f.note = (note + "; " if note else "") + hist_note
            else:
                f.level = "warn"
                if days is not None:
                    watch = f"interface scar — {days:.0f}d stable (need {hist_days}d)"
                else:
                    watch = f"interface scar — watching until {hist_days}d stable"
                if "interface scar" not in note and "historical interface" not in note:
                    f.note = (note + "; " if note else "") + watch

        if name in MEDIA_SCAR_NAMES:
            # Same idea as UDMA, longer window: early-life / factory scars often sit forever.
            # GROWING stays critical. Pending/OfflineUnc are NOT in this set.
            hist_days = _media_scar_historical_days()
            days = _days_since_last_increase(f, now=now)
            try:
                f.scar_need_days = hist_days
                f.stable_days = int(days) if days is not None else None
            except (TypeError, AttributeError):
                pass
            if growing:
                f.level = "critical"
                if "growing media" not in note:
                    f.note = (note + "; " if note else "") + "GROWING media scar — prioritize"
            elif days is not None and days >= hist_days:
                f.level = "info"
                hist_note = f"historical media scar (no increase ≥{hist_days}d)"
                if "historical media" not in note:
                    f.note = (note + "; " if note else "") + hist_note
            else:
                f.level = "critical"
                if days is not None:
                    watch = f"media scar — {days:.0f}d stable (need {hist_days}d)"
                else:
                    watch = f"media scar — watching until {hist_days}d stable"
                if "media scar" not in note and "historical media" not in note:
                    f.note = (note + "; " if note else "") + watch


def is_actionable_finding(f: Any) -> bool:
    """Whether a finding should keep the disk on the risk list."""
    level = getattr(f, "level", "")
    growing = bool(getattr(f, "growing", False))
    if level in ("critical", "warn"):
        return True
    if level == "info" and growing:
        return True
    return False


def filter_actionable(findings: list[Any]) -> list[Any]:
    return [f for f in findings if is_actionable_finding(f)]


def evaluate(
    attrs: list[dict],
    engine: str = "classic",
    smart_extra: dict[str, Any] | None = None,
) -> tuple[list[EvalFinding], list[EvalFinding]]:
    """Return (primary_findings, classic_findings_for_diff).

    classic_findings_for_diff is only populated when engine != classic (A/B).
    """
    eng = (engine or "classic").strip().lower()
    classic = classic_evaluate(attrs)
    if eng == "classic":
        return classic, []
    prefer = eng == "stats"
    hybrid = hybrid_evaluate(attrs, smart_extra=smart_extra, prefer_devstat=prefer)
    return hybrid, classic
