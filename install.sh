#!/usr/bin/env bash
# Install or refresh Diskrisk + diskinfo on a Linux host.
#
# Recommended (git checkout = app, easy updates):
#   sudo git clone <repo-url> /opt/diskrisk
#   cd /opt/diskrisk && sudo ./install.sh
#   sudoedit /etc/diskrisk/config.env
#   sudo systemctl start diskrisk
#
# Later:
#   cd /opt/diskrisk && sudo ./update.sh
#
# Config always lives in /etc/diskrisk/config.env and is never overwritten.
set -euo pipefail

PREFIX="${PREFIX:-/opt/diskrisk}"
SYSCONF="${SYSCONF:-/etc/diskrisk}"
STATE="${STATE:-/var/lib/diskrisk}"
BINDIR="${BINDIR:-/usr/local/bin}"
UNITDIR="${UNITDIR:-/etc/systemd/system}"
WITH_SYSTEMD=1
DISKINFO_ONLY=0
RESTART=0
MODE="" # auto | in-place | copy
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Install / refresh Diskrisk and/or diskinfo.

Recommended layout: git clone into ${PREFIX}, then run this script there.
Updates are then:  cd ${PREFIX} && sudo ./update.sh
Config is always outside the git tree: ${SYSCONF}/config.env (kept forever).

Options:
  --prefix DIR       App directory (default: ${PREFIX})
  --in-place         Use this git checkout as the app (no file copy)
  --copy             Copy files from this checkout into --prefix
  --diskinfo-only    Only install the diskinfo CLI (no Diskrisk service/config)
  --sysconf DIR      Config directory (default: ${SYSCONF})
  --statedir DIR     History/state directory (default: ${STATE})
  --bindir DIR       Symlink CLIs here (default: ${BINDIR})
  --no-systemd       Skip systemd unit install/enable
  --restart          Restart diskrisk.service if active
  -h, --help         This help

Environment:
  DESTDIR            Staging root for packaging (prepended to all paths)

diskinfo is standalone: ZFS mirror tree needs only zpool/lsblk. Diskrisk
enrichment is optional (SMART_RISK_URL / diskinfo --risk).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --sysconf) SYSCONF="$2"; shift 2 ;;
    --statedir) STATE="$2"; shift 2 ;;
    --bindir) BINDIR="$2"; shift 2 ;;
    --in-place) MODE=in-place; shift ;;
    --copy) MODE=copy; shift ;;
    --diskinfo-only) DISKINFO_ONLY=1; WITH_SYSTEMD=0; shift ;;
    --no-systemd) WITH_SYSTEMD=0; shift ;;
    --restart) RESTART=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

root() { printf '%s%s' "${DESTDIR:-}" "$1"; }

if [[ "$(id -u)" -ne 0 && -z "${DESTDIR:-}" ]]; then
  echo "Run as root (or set DESTDIR= for a staged install)." >&2
  exit 1
fi

# Resolve mode: same directory → in-place; otherwise copy into PREFIX.
REPO_ABS="$(cd "$REPO_ROOT" && pwd -P)"
PREFIX_ABS="$(mkdir -p "$(root "$PREFIX")" && cd "$(root "$PREFIX")" && pwd -P)"
if [[ -z "$MODE" ]]; then
  if [[ "$REPO_ABS" == "$PREFIX_ABS" ]]; then
    MODE=in-place
  else
    MODE=copy
  fi
fi

echo "Diskrisk install mode=${MODE}$([[ "$DISKINFO_ONLY" -eq 1 ]] && echo ' (diskinfo-only)')"
echo "  app:    $(root "$PREFIX")"
if [[ "$DISKINFO_ONLY" -eq 0 ]]; then
  echo "  config: $(root "$SYSCONF")/config.env  (never overwritten if present)"
fi

install -d "$(root "$PREFIX")"
install -d "$(root "$BINDIR")"
if [[ "$DISKINFO_ONLY" -eq 0 ]]; then
  install -d "$(root "$SYSCONF")"
  install -d "$(root "$STATE")"
fi

if [[ "$MODE" == "copy" ]]; then
  install -d "$(root "$PREFIX")/diskinfo"
  install -m 0755 "$REPO_ROOT/diskinfo/diskinfo.sh" "$(root "$PREFIX")/diskinfo/diskinfo.sh"
  install -m 0644 "$REPO_ROOT/LICENSE" "$(root "$PREFIX")/LICENSE"
  install -m 0644 "$REPO_ROOT/README.md" "$(root "$PREFIX")/README.md"
  if [[ -d "$REPO_ROOT/docs" ]]; then
    install -d "$(root "$PREFIX")/docs"
    install -m 0644 "$REPO_ROOT/docs/"*.md "$(root "$PREFIX")/docs/" 2>/dev/null || true
  fi
  if [[ "$DISKINFO_ONLY" -eq 0 ]]; then
    install -d "$(root "$PREFIX")/branding"
    install -d "$(root "$PREFIX")/deploy"
    install -m 0644 "$REPO_ROOT/smart_risk.py" "$(root "$PREFIX")/smart_risk.py"
    find "$REPO_ROOT/branding" -maxdepth 1 -type f -exec install -m 0644 {} "$(root "$PREFIX")/branding/" \;
    install -m 0644 "$REPO_ROOT/config.example.env" "$(root "$PREFIX")/config.example.env"
    install -m 0644 "$REPO_ROOT/THIRD_PARTY.md" "$(root "$PREFIX")/THIRD_PARTY.md"
    install -m 0644 "$REPO_ROOT/deploy/diskrisk.service.example" "$(root "$PREFIX")/deploy/diskrisk.service.example"
  fi
  if [[ -f "$REPO_ROOT/update.sh" ]]; then
    install -m 0755 "$REPO_ROOT/update.sh" "$(root "$PREFIX")/update.sh"
  fi
  if [[ -f "$REPO_ROOT/install.sh" ]]; then
    install -m 0755 "$REPO_ROOT/install.sh" "$(root "$PREFIX")/install.sh"
  fi
else
  chmod 0755 "$(root "$PREFIX")/diskinfo/diskinfo.sh" 2>/dev/null || true
  chmod 0755 "$(root "$PREFIX")/install.sh" "$(root "$PREFIX")/update.sh" 2>/dev/null || true
  [[ "$DISKINFO_ONLY" -eq 0 ]] && chmod 0755 "$(root "$PREFIX")/smart_risk.py" 2>/dev/null || true
fi

if [[ "$DISKINFO_ONLY" -eq 0 ]]; then
  CFG="$(root "$SYSCONF")/config.env"
  if [[ ! -f "$CFG" ]]; then
    sed \
      -e "s|^SMART_RISK_HISTORY=.*|SMART_RISK_HISTORY=${STATE}/history.json|" \
      -e "s|^SMART_RISK_BRANDING=.*|SMART_RISK_BRANDING=${PREFIX}/branding|" \
      "$REPO_ROOT/config.example.env" > "$CFG"
    # Full stack: enable diskinfo enrichment against local Diskrisk by default.
    if ! grep -q '^SMART_RISK_URL=' "$CFG"; then
      printf '\nSMART_RISK_URL=http://127.0.0.1:8091/json\n' >> "$CFG"
    fi
    chmod 0600 "$CFG"
    echo "Created ${SYSCONF}/config.env — edit BESZEL_USER / BESZEL_PASS / URLs"
  else
    echo "Keeping existing ${SYSCONF}/config.env"
  fi
fi

ln -sfn "${PREFIX}/diskinfo/diskinfo.sh" "$(root "$BINDIR")/diskinfo"
if [[ "$DISKINFO_ONLY" -eq 0 ]]; then
  ln -sfn "${PREFIX}/smart_risk.py" "$(root "$BINDIR")/diskrisk"
  chmod 0755 "$(root "$PREFIX")/smart_risk.py"
fi

if [[ "$WITH_SYSTEMD" -eq 1 && "$DISKINFO_ONLY" -eq 0 ]]; then
  install -d "$(root "$UNITDIR")"
  UNIT="$(root "$UNITDIR")/diskrisk.service"
  cat > "$UNIT" <<EOF
[Unit]
Description=Diskrisk — actionable SMART risk report
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=DISKRISK_CONFIG=${SYSCONF}/config.env
WorkingDirectory=${PREFIX}
ExecStart=/usr/bin/python3 ${PREFIX}/smart_risk.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  if [[ -z "${DESTDIR:-}" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable diskrisk.service
    if [[ "$RESTART" -eq 1 ]]; then
      systemctl try-restart diskrisk.service || systemctl restart diskrisk.service
      echo "Restarted diskrisk.service"
    else
      echo "Enabled diskrisk.service — start with: systemctl start diskrisk"
    fi
  else
    echo "Installed unit file (daemon-reload/enable skipped under DESTDIR or without systemctl)"
  fi
fi

if [[ "$DISKINFO_ONLY" -eq 1 ]]; then
  cat <<EOF

Done (${MODE}, diskinfo-only).

  App:  ${PREFIX}/diskinfo/
  CLI:  ${BINDIR}/diskinfo

  diskinfo works standalone (ZFS tree). Optional enrichment:
    SMART_RISK_URL=http://diskrisk-host:8091/json diskinfo --risk
    # or: diskinfo --risk   (tries http://127.0.0.1:8091/json)

EOF
else
  cat <<EOF

Done (${MODE}).

  Config:   ${SYSCONF}/config.env   ← yours; install/update never replace it
  App:      ${PREFIX}/              ← git checkout when using in-place mode
  History:  ${STATE}/history.json
  CLI:      ${BINDIR}/diskinfo
            ${BINDIR}/diskrisk

Update later:
  cd ${PREFIX} && sudo ./update.sh

EOF
fi
