#!/bin/bash
# build.sh — reproducible build of the custom Tapo C210 firmware.
#
# Clones (or reuses) the pinned upstream thingino tree, applies this repo's
# layer, builds in the official Docker builder, and copies the packed image
# into ./images/.
#
# Env overrides:
#   THINGINO_DIR   upstream checkout path      (default: ./build/thingino, in-repo)
#   PIN            override the pinned commit  (default: from ./UPSTREAM_PIN)
#   NO_PIN=1       skip checkout of the pin (use the tree as-is)
#   CLEAN=1        wipe this board's output dir first (needed after disabling
#                  packages: Buildroot leaves stale files in target/ otherwise).
#                  ccache still accelerates the recompile.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
THINGINO_DIR="${THINGINO_DIR:-$REPO/build/thingino}"
CAM=tplink_tapo_c210_t23n_sc3336_rtl8188ftv
IMG=ghcr.io/themactep/thingino-builder-image:latest

UPSTREAM_URL="$(sed -n '1p' "$REPO/UPSTREAM_PIN")"
PIN="${PIN:-$(sed -n '2p' "$REPO/UPSTREAM_PIN")}"

# 1. upstream tree ----------------------------------------------------------
if [ ! -d "$THINGINO_DIR/.git" ]; then
  echo "== cloning upstream -> $THINGINO_DIR"
  git clone "$UPSTREAM_URL" "$THINGINO_DIR"
fi
if [ "${NO_PIN:-0}" != "1" ]; then
  echo "== checking out pin $PIN"
  git -C "$THINGINO_DIR" fetch --all --tags --quiet || true
  git -C "$THINGINO_DIR" checkout --quiet "$PIN"
fi

# 2. apply our layer --------------------------------------------------------
THINGINO_DIR="$THINGINO_DIR" "$REPO/scripts/apply-layer.sh"

# optional clean: wipe this board's output dir(s) so disabled packages leave
# no stale files behind (Buildroot does not prune target/ on package removal)
if [ "${CLEAN:-0}" = "1" ]; then
  echo "== CLEAN: wiping output for $CAM"
  rm -rf "$THINGINO_DIR"/output/*/"${CAM}"-* 2>/dev/null || true
fi

# 3. build ------------------------------------------------------------------
cd "$THINGINO_DIR"
echo "== $(date +%T) pulling builder image (if needed)"
docker image inspect "$IMG" >/dev/null 2>&1 || docker pull "$IMG"
mkdir -p dl output
OVR=""; [ -d overrides ] && OVR="-v $(readlink -f overrides):/overrides"

# force rootfs regen so overlay/target-finalize re-applies our files
rm -f output/*/images/rootfs.squashfs 2>/dev/null || true

echo "== $(date +%T) starting build (make fast)"
docker run --rm --user "$(id -u):$(id -g)" --network=host \
  -v "$PWD":/workspace $OVR -v "$PWD/dl":/dl -w /workspace \
  -e TERM=xterm-256color -e BR2_DL_DIR=/dl -e HOME=/workspace \
  "$IMG" bash -lc "sudo update-alternatives --install /usr/bin/install install /usr/bin/gnuinstall 100 2>/dev/null; BOARD=$CAM make fast WORKFLOW=1"

# 4. collect image ----------------------------------------------------------
# newest by mtime (NOT alphabetical: detached-HEAD builds land in output/HEAD,
# which would sort before a stale output/master)
OUT="$(find output -path "*images/thingino-*${CAM}.bin" -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)"
if [ -n "$OUT" ]; then
  STAMP="$(date +%Y-%m-%d)"
  DEST="$REPO/images/thingino_tapo_c210_custom_${STAMP}.bin"
  cp -v "$OUT" "$DEST"
  ( cd "$REPO/images" && md5sum "$(basename "$DEST")" )
  echo "== image -> $DEST"
else
  echo "!! no output image found under output/*/images/"
  exit 1
fi
