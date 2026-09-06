#!/usr/bin/env bash
# Build a source tarball + SHA-256 checksums for a GitHub Release.
#
# Prefer SHA-256 over MD5 (MD5 is not suitable for integrity checks).
# Install path is normally `git clone` / `./update.sh`; these assets are for
# people who download a release archive and want to verify it.
#
# Usage:
#   ./scripts/release-assets.sh           # version from VERSION file / tag
#   ./scripts/release-assets.sh 1.3.5
#
# Then upload:
#   gh release upload "v${VER}" dist/diskrisk-${VER}.tar.gz dist/SHA256SUMS dist/SHA256SUMS.asc  # asc optional
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VER="${1:-}"
if [[ -z "$VER" ]]; then
  VER="$(tr -d '[:space:]' < VERSION)"
fi
VER="${VER#v}"

if [[ ! "$VER" =~ ^[0-9]+\.[0-9]+ ]]; then
  echo "Bad version: $VER" >&2
  exit 1
fi

OUTDIR="${OUTDIR:-$REPO_ROOT/dist}"
mkdir -p "$OUTDIR"
NAME="diskrisk-${VER}"
TAR="${OUTDIR}/${NAME}.tar.gz"
SUMS="${OUTDIR}/SHA256SUMS"

# Prefer an annotated/lightweight tag tree when present
REF="v${VER}"
if ! git rev-parse -q --verify "$REF" >/dev/null; then
  REF="HEAD"
  echo "Note: tag v${VER} not found — archiving HEAD ($(git rev-parse --short HEAD))" >&2
fi

rm -f "$TAR" "$SUMS"
git archive --format=tar.gz --prefix="${NAME}/" -o "$TAR" "$REF"

(
  cd "$OUTDIR"
  # GNU coreutils: portable enough on Linux release hosts
  sha256sum "$(basename "$TAR")" > SHA256SUMS
)

echo "Wrote:"
echo "  $TAR"
echo "  $SUMS"
echo
echo "Verify later with:"
echo "  cd dist && sha256sum -c SHA256SUMS"
echo
echo "Upload to GitHub (example):"
echo "  gh release upload \"v${VER}\" \"$TAR\" \"$SUMS\" --clobber"
