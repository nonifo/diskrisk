#!/bin/bash
# diskinfo — ZFS vdev tree (serial → bay); optional Diskrisk SMART/RISK columns
# Manual: docs/diskinfo-manual.md
set -euo pipefail

VERSION="1.2.1"
NO_SMART=0
WANT_RISK=0          # 1 = force SMART/RISK columns
LIVE_SMART=0         # 1 = fall back to smartctl when Diskrisk is down
HAVE_RISK_CACHE=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RISK_FILE=""
# Prefer Diskrisk SMART state when running as non-root (smartctl needs privileges).
SMARTCTL_BIN="${SMARTCTL_BIN:-smartctl}"

# Fixed column widths — header and rows must use the same totals.
# STRUCTURE column: header is full width; rows are "  " + (W_STRUCT-2).
W_STRUCT=20
W_DEV=12
W_SER=28
W_SIZE=8
W_IO=4
W_SMART=7
W_RISK=40

# zpool status group names (not leaf devices): pool, mirror/raidz/draid-N, special/…
is_vdev_header() {
  local name="$1"
  [[ "$name" == "$POOL" ]] && return 0
  case "$name" in
    special|spares|logs|cache|dedup) return 0 ;;
  esac
  [[ "$name" =~ ^(mirror|raidz[123]?|draid[123]?)-[0-9]+$ ]]
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  # collapse internal whitespace (some SCSI serials are dual fields)
  echo -n "$s" | tr -s '[:space:]' ' '
}

# Optional shared config (same file as the Diskrisk service). Existing env wins.
_load_diskrisk_config() {
  local f
  local candidates=()
  [[ -n "${DISKRISK_CONFIG:-}" ]] && candidates+=("$DISKRISK_CONFIG")
  candidates+=("$PWD/config.env" "$SCRIPT_DIR/config.env" "$REPO_ROOT/config.env" /etc/diskrisk/config.env)
  for f in "${candidates[@]}"; do
    [[ -f "$f" ]] || continue
    eval "$(python3 - "$f" <<'PY'
import os, shlex, sys
path = sys.argv[1]
for raw in open(path, encoding="utf-8"):
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
        print(f"export {key}={shlex.quote(val)}")
PY
)"
    return 0
  done
  return 0
}
_load_diskrisk_config

# Empty SMART_RISK_URL = standalone ZFS mode (Diskrisk not required).
SMART_RISK_URL="${SMART_RISK_URL:-}"
DEFAULT_POOL="${DISKINFO_POOL:-tank}"
POOL="$DEFAULT_POOL"

usage() {
  cat <<EOF
diskinfo v${VERSION} — ZFS vdev tree (serial → bay). Diskrisk enrichment optional.

Works standalone: no Diskrisk/Beszel required for the core view.
Shows mirror-N, raidz1/2/3-N, draid-N, special/spares/logs/cache.

Usage:
  diskinfo [pool]              # ZFS tree; +RISK if SMART_RISK_URL is set
  diskinfo --risk [pool]       # force SMART/RISK columns (Diskrisk or --live-smart)
  diskinfo --no-smart [pool]   # ZFS + serial only (never call Diskrisk/smartctl)
  diskinfo --live-smart [pool] # if Diskrisk down, use local smartctl
  diskinfo --version
  diskinfo -h|--help

Env:
  DISKINFO_POOL     Default pool (default ${DEFAULT_POOL})
  SMART_RISK_URL    Diskrisk JSON (unset = standalone ZFS mode)
  DISKRISK_CONFIG   Optional shared config.env
EOF
}

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --version) echo "diskinfo ${VERSION}"; exit 0 ;;
    --no-smart) NO_SMART=1; shift ;;
    --risk|--with-risk) WANT_RISK=1; shift ;;
    --live-smart) LIVE_SMART=1; WANT_RISK=1; shift ;;
    -*) echo "Unknown flag: $1" >&2; usage >&2; exit 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
POOL="${ARGS[0]:-$DEFAULT_POOL}"

# Standalone by default: only enable RISK columns when URL configured or --risk.
if [[ "$NO_SMART" -eq 0 ]]; then
  if [[ "$WANT_RISK" -eq 1 && -z "$SMART_RISK_URL" ]]; then
    SMART_RISK_URL="http://127.0.0.1:8091/json"
  fi
  if [[ -z "$SMART_RISK_URL" && "$WANT_RISK" -eq 0 ]]; then
    NO_SMART=1
  fi
fi

cleanup() { [[ -n "$RISK_FILE" && -f "$RISK_FILE" ]] && rm -f "$RISK_FILE"; }
trap cleanup EXIT

print_header() {
  local color="$1" tail="${2:-PARTUUID}"
  if [[ "$NO_SMART" -eq 1 ]]; then
    printf "${color}%-${W_STRUCT}s %-${W_DEV}s %-${W_SER}s %-${W_SIZE}s %${W_IO}s %${W_IO}s %${W_IO}s %s\033[0m\n" \
      "ZFS STRUCTURE" "DEVICE" "SERIAL" "SIZE" "RD" "WR" "CK" "$tail"
  else
    printf "${color}%-${W_STRUCT}s %-${W_DEV}s %-${W_SER}s %-${W_SIZE}s %${W_IO}s %${W_IO}s %${W_IO}s %-${W_SMART}s %-${W_RISK}s %s\033[0m\n" \
      "ZFS STRUCTURE" "DEVICE" "SERIAL" "SIZE" "RD" "WR" "CK" "SMART" "RISK" "$tail"
  fi
}

print_row() {
  local c_struct="$1" struct="$2" dev="$3" ser="$4" size="$5" rd="$6" wr="$7" ck="$8"
  local smart="${9-}" risk="${10-}" uuid="${11-}"
  # Indent 2 + (W_STRUCT-2) matches header W_STRUCT
  printf "  ${c_struct}%-$((W_STRUCT - 2))s\033[0m " "$struct"
  if [[ "$NO_SMART" -eq 1 ]]; then
    printf "%-${W_DEV}s %-${W_SER}s %-${W_SIZE}s %${W_IO}s %${W_IO}s %${W_IO}s %s\n" \
      "$dev" "$ser" "$size" "$rd" "$wr" "$ck" "$uuid"
  else
    printf "%-${W_DEV}s %-${W_SER}s %-${W_SIZE}s %${W_IO}s %${W_IO}s %${W_IO}s %-${W_SMART}s %-${W_RISK}s %s\n" \
      "$dev" "$ser" "$size" "$rd" "$wr" "$ck" "$smart" "$risk" "$uuid"
  fi
}

load_risk_cache() {
  RISK_FILE=$(mktemp /tmp/diskinfo-risk.XXXXXX)
  if ! curl -fsS --connect-timeout 2 --max-time 8 "$SMART_RISK_URL" -o "$RISK_FILE" 2>/dev/null; then
    rm -f "$RISK_FILE"
    RISK_FILE=""
    return 1
  fi
  return 0
}

# Diskrisk lookup: prints "SMART|RISK" (e.g. PASSED|GrownDefect=815↑). Exit 1 = miss.
risk_lookup() {
  local serial="$1"
  [[ -z "$RISK_FILE" || ! -s "$RISK_FILE" || -z "$serial" ]] && return 1
  python3 - "$RISK_FILE" "$serial" <<'PY'
import json, re, sys
SHORT = {
  "Reallocated_Sector_Ct": "Realloc",
  "Current_Pending_Sector": "Pending",
  "Offline_Uncorrectable": "OfflineUnc",
  "Reported_Uncorrect": "ReportedUnc",
  "GrownDefectList": "GrownDefect",
  "UDMA_CRC_Error_Count": "UDMA_CRC",
  "Multi_Zone_Error_Rate": "MultiZone",
  "Command_Timeout": "CmdTimeout",
  "Spin_Retry_Count": "SpinRetry",
  "End-to-End_Error": "E2E",
  "ReadTotalUncorrectedErrors": "ReadUnc",
  "WriteTotalUncorrectedErrors": "WriteUnc",
  "VerifyTotalUncorrectedErrors": "VerifyUnc",
  "Reallocated_Event_Count": "ReallocEvt",
  "Runtime_Bad_Block": "BadBlock",
}

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

path, want = sys.argv[1], norm(sys.argv[2])
data = json.load(open(path))

def find(rows):
    for r in rows or []:
        if norm(r.get("serial") or "") == want:
            return r
    return None

match = find(data.get("risks"))
clean = None if match else find(data.get("clean_disks"))
row = match or clean
if row is None:
    raise SystemExit(1)

state = (row.get("state") or "").strip().upper() or "?"
if state not in ("PASSED", "FAILED", "?"):
    # tolerate OK / FAIL-ish variants from upstream
    if state in ("OK", "GOOD"):
        state = "PASSED"
    elif "FAIL" in state:
        state = "FAILED"

if clean is not None and match is None:
    print(f"{state}|.")
    raise SystemExit(0)

parts = []
for f in (match or {}).get("findings") or []:
    short = SHORT.get(f.get("name") or "", f.get("name") or "?")
    val = f.get("value")
    arrow = "↑" if f.get("growing") else ""
    parts.append(f"{short}={val}{arrow}")
scr = (match or {}).get("scrutiny_status")
if scr is not None and int(scr) >= 2:
    parts.append(f"Scr≥{scr}")
risk = " ".join(parts) if parts else "."
print(f"{state}|{risk[:40]}")
PY
}

# Live smartctl -H. Never treat "open device: … failed: Permission denied" as FAILED.
smart_health() {
  local pk="$1"
  [[ -z "$pk" || ! -b "/dev/$pk" ]] && { echo "-"; return 0; }
  local out
  out=$("$SMARTCTL_BIN" -H "/dev/$pk" 2>&1 || true)
  if echo "$out" | grep -qiE 'Permission denied|you must be root|Operation not permitted'; then
    echo "?"
    return 0
  fi
  # ATA
  if echo "$out" | grep -qiE 'overall-health self-assessment test result:\s*PASSED'; then
    echo "PASSED"; return 0
  fi
  if echo "$out" | grep -qiE 'overall-health self-assessment test result:\s*FAILED'; then
    echo "FAILED"; return 0
  fi
  # SCSI/SAS
  if echo "$out" | grep -qiE 'SMART Health Status:\s*OK'; then
    echo "PASSED"; return 0
  fi
  if echo "$out" | grep -qiE 'SMART Health Status:\s*FAILED'; then
    echo "FAILED"; return 0
  fi
  echo "?"
  return 0
}

smart_risk_live() {
  local pk="$1"
  [[ -z "$pk" || ! -b "/dev/$pk" ]] && { echo "-"; return 0; }
  local atr grown pending unc realloc mz crc
  # smartctl may fail for non-root; ignore pipeline status under pipefail
  atr=$("$SMARTCTL_BIN" -A "/dev/$pk" 2>/dev/null || true)
  grown=$("$SMARTCTL_BIN" -x "/dev/$pk" 2>/dev/null | awk -F: '/[Ee]lements in grown defect list/{gsub(/[^0-9]/,"",$2); print $2+0; exit}' || true)
  pending=$(echo "$atr" | awk '/Current_Pending_Sector/{print $10+0; exit}')
  unc=$(echo "$atr" | awk '/Offline_Uncorrectable/{print $10+0; exit}')
  realloc=$(echo "$atr" | awk '/Reallocated_Sector_Ct/{print $10+0; exit}')
  mz=$(echo "$atr" | awk '/Multi_Zone_Error_Rate/{print $10+0; exit}')
  crc=$(echo "$atr" | awk '/UDMA_CRC_Error_Count/{print $10+0; exit}')
  local parts=()
  [[ -n "${grown:-}" && "$grown" -gt 0 ]] && parts+=("GrownDefect=$grown")
  [[ -n "${pending:-}" && "$pending" -gt 0 ]] && parts+=("Pending=$pending")
  [[ -n "${unc:-}" && "$unc" -gt 0 ]] && parts+=("OfflineUnc=$unc")
  [[ -n "${realloc:-}" && "$realloc" -gt 0 ]] && parts+=("Realloc=$realloc")
  [[ -n "${mz:-}" && "$mz" -gt 0 ]] && parts+=("MultiZone=$mz")
  [[ -n "${crc:-}" && "$crc" -gt 0 ]] && parts+=("UDMA_CRC=$crc")
  if [[ ${#parts[@]} -eq 0 ]]; then
    echo "."
  else
    local s
    s=$(IFS=' '; echo "${parts[*]}")
    echo "${s:0:40}"
  fi
  return 0
}

fill_smart() {
  # sets globals _smart _risk for pk+serial
  local pk="$1" serial="$2" combo smart_live
  _smart="-"
  _risk="-"
  [[ "$NO_SMART" -eq 1 ]] && return 0

  # Prefer Diskrisk JSON (works as non-root; includes SMART state + trend).
  if [[ "$HAVE_RISK_CACHE" -eq 1 ]] && combo=$(risk_lookup "$serial"); then
    _smart="${combo%%|*}"
    _risk="${combo#*|}"
    [[ -z "$_smart" ]] && _smart="?"
    [[ -z "$_risk" ]] && _risk="."
    return 0
  fi

  # Optional local smartctl fallback (slow; often needs root).
  if [[ "$LIVE_SMART" -eq 1 ]]; then
    smart_live=$(smart_health "$pk")
    _smart="$smart_live"
    _risk=$(smart_risk_live "$pk" || true)
    return 0
  fi

  _smart="-"
  _risk="-"
  return 0
}

USED_DISKS=$(zpool status -P "$POOL" 2>/dev/null | awk '/\/dev\/disk\/by-partuuid\// {print $1}' | xargs -r -I {} lsblk -no PKNAME "{}" 2>/dev/null | sort -u | tr '\n' '|' | sed 's/|$//')

echo -e "\033[1;34mdiskinfo v${VERSION}\033[0m  pool=\033[1m${POOL}\033[0m  $(date '+%Y-%m-%d %H:%M:%S %Z')"
if [[ "$NO_SMART" -eq 0 ]]; then
  if load_risk_cache; then
    HAVE_RISK_CACHE=1
    echo -e "Diskrisk: \033[0;32menriched\033[0m from ${SMART_RISK_URL}  (↑ = growing since baseline)"
  elif [[ "$LIVE_SMART" -eq 1 ]]; then
    echo -e "Diskrisk: \033[0;33munreachable\033[0m — \033[0;33mlive smartctl\033[0m (no trend arrow)"
  else
    # Keep RISK columns but stay fast/standalone when the service is down.
    echo -e "Diskrisk: \033[0;33munreachable\033[0m — ZFS view only (set SMART_RISK_URL / use --live-smart)"
    NO_SMART=1
  fi
else
  echo -e "Mode: \033[0;32mZFS standalone\033[0m (Diskrisk not required)"
fi
echo ""

print_header "\033[1;34m" "PARTUUID"

while IFS= read -r line; do
  if [[ "$line" =~ (/dev/disk/by-partuuid/([a-z0-9-]+)) ]]; then
    set -- $line
    devpath="$1"
    state="$2"
    rd="${3:-0}"; wr="${4:-0}"; ck="${5:-0}"
    uuid="${BASH_REMATCH[2]}"
    [[ "$state" == "AVAIL" ]] && rd="-" wr="-" ck="-"

    pk=$(lsblk -dno PKNAME "$devpath" 2>/dev/null)
    serial=$(trim "$(lsblk -dno SERIAL "/dev/$pk" 2>/dev/null)")
    size=$(trim "$(lsblk -dno SIZE "/dev/$pk" 2>/dev/null)")

    col="\033[0;32m"
    [[ "$state" != "ONLINE" && "$state" != "AVAIL" ]] && col="\033[0;31m"

    _smart="" _risk=""
    if [[ "$NO_SMART" -eq 0 ]]; then
      fill_smart "$pk" "$serial"
      if [[ "$_smart" == "FAILED" ]]; then
        col="\033[0;31m"
      elif [[ "$_risk" == *↑* || "$_risk" == GrownDefect=* || "$_risk" == *Pending=* || "$_risk" == *OfflineUnc=* || "$_risk" == *Realloc=* ]]; then
        [[ "$state" == "ONLINE" || "$state" == "AVAIL" ]] && col="\033[0;33m"
      fi
    fi

    print_row "$col" "$state" "/dev/$pk" "$serial" "$size" "$rd" "$wr" "$ck" "${_smart:-}" "${_risk:-}" "$uuid"

  else
    # Structure line: pool root or vdev group (mirror / raidz / draid / …).
    clean=$(echo "$line" | awk '{print $1}')
    if is_vdev_header "$clean"; then
      printf "\033[1;32m%s\033[0m\n" "$clean"
    fi
  fi
done < <(zpool status -P "$POOL" 2>/dev/null)

echo ""
# Unused header — same columns, last = INFO
print_header "\033[1;33m" "INFO"

while read -r name; do
  [[ -z "$name" ]] && continue
  serial=$(trim "$(lsblk -dno SERIAL "/dev/$name" 2>/dev/null)")
  size=$(trim "$(lsblk -dno SIZE "/dev/$name" 2>/dev/null)")
  _smart="" _risk=""
  if [[ "$NO_SMART" -eq 0 ]]; then
    fill_smart "$name" "$serial"
  fi
  print_row "\033[0m" "UNUSED" "/dev/$name" "$serial" "$size" "-" "-" "-" "${_smart:-}" "${_risk:-}" "Not in ZFS pool"
done < <(lsblk -dno NAME | grep -E '^sd' | { [[ -z "$USED_DISKS" ]] && cat || grep -vE "$USED_DISKS" || true; })

echo ""
if [[ "$NO_SMART" -eq 0 ]]; then
  echo -e "Legend: \033[0;32mONLINE/AVAIL\033[0m  \033[0;33mrisk/growing\033[0m  \033[0;31mFAILED/FAULTED\033[0m  RISK \".\" = clean  ↑ = growing  ? = SMART unknown"
else
  echo -e "Legend: \033[0;32mONLINE/AVAIL\033[0m  \033[0;31mFAULTED\033[0m  — Diskrisk optional: diskinfo --risk"
fi
echo "Docs: docs/diskinfo-manual.md"
