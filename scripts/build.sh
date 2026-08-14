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
#   DIRCLEAN="a b" re-extract these packages before building. REQUIRED after
#                  adding or editing a patch in
#                  tree-overrides/package/all-patches/<pkg>/ — Buildroot applies
#                  patches only at extract time, so an already-extracted package
#                  silently ignores a new patch. Much faster than CLEAN=1.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
THINGINO_DIR="${THINGINO_DIR:-$REPO/build/thingino}"
CAM=tplink_tapo_c210_t23n_sc3336_rtl8188ftv
IMG=ghcr.io/themactep/thingino-builder-image:latest

UPSTREAM_URL="$(sed -n '1p' "$REPO/UPSTREAM_PIN")"
PIN="${PIN:-$(sed -n '2p' "$REPO/UPSTREAM_PIN")}"

# Fixed rootfs partition size (bytes). Upstream auto-sizes the rootfs partition
# to hug the squashfs (thingino Makefile: ROOTFS_PARTITION_SIZE =
# ROOTFS_BIN_SIZE_ALIGNED) and gives the remainder to the data/overlay
# partition — which leaves ZERO rootfs headroom, so any feature that grows the
# rootfs would force a full (bootloader-rewriting) flash. We pin it instead so
# routine growth stays on the safe rootfs-only OTA path.
#
# 5376 KB = 84 erase blocks (64 KB each) = 5,505,024 bytes. Layout becomes:
#   boot 320 + env 64 + backup 64 + kernel 1600 + rootfs 5376 + data 768 = 8192 KB
# data/overlay 768 KB is well above the jffs2 floor (~320 KB) and the ~148 KB in
# use. Current rootfs is ~4.92 MB, so this reserves ~450 KB of growth room.
#
# Passed as a make COMMAND-LINE variable (not env): line 346 uses `=`, which
# overrides an env var but not a command-line assignment. It propagates to the
# offset/data/mtdparts/uenv math that all derives from it. The build's own
# overflow guard errors if the squashfs ever exceeds this. Override per-build
# with ROOTFS_PARTITION_SIZE=<bytes> ./scripts/build.sh; set empty to restore
# upstream auto-sizing.
ROOTFS_PARTITION_SIZE="${ROOTFS_PARTITION_SIZE-5505024}"

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

# force rootfs regen so overlay/target-finalize re-applies our files.
# NOTE the glob depth: images/ lives at output/<ref>/<board>/images/, so a
# single */ silently matched nothing and the pack step reused a stale rootfs.
rm -f output/*/*/images/rootfs.squashfs 2>/dev/null || true

in_builder() {
  docker run --rm --user "$(id -u):$(id -g)" --network=host \
    -v "$PWD":/workspace $OVR -v "$PWD/dl":/dl -w /workspace \
    -e TERM=xterm-256color -e BR2_DL_DIR=/dl -e HOME=/workspace \
    "$IMG" bash -lc "sudo update-alternatives --install /usr/bin/install install /usr/bin/gnuinstall 100 2>/dev/null;$1"
}

# --- config first, in its own make invocation -------------------------------
# `make fast` is `user-dirs defconfig build_fast pack`. Regenerating .config in
# the same run that builds is what produced the long-standing "first build after
# a config change is stale, and still exits 0" trap: a package whose *sub-options*
# changed keeps its .stamp_built, so Buildroot never rebuilds it and packs the
# previous binary. That is how RMD went missing from an exit-0 build.
#
# So: run defconfig alone, diff the .config it produces against the one the last
# build used, and dirclean whatever package owns each changed symbol before
# building. The workaround until now was "just build twice", which does not
# actually fix this — it only helped when the stale artefact happened to be
# rebuilt for another reason.
CFG_PATH="$(ls -d output/*/"${CAM}"-*/.config 2>/dev/null | head -1 || true)"
CFG_BEFORE="$(mktemp)"; trap 'rm -f "$CFG_BEFORE"' EXIT
[ -n "$CFG_PATH" ] && [ -f "$CFG_PATH" ] && cp "$CFG_PATH" "$CFG_BEFORE"

echo "== $(date +%T) generating .config"
in_builder "BOARD=$CAM make defconfig"

CFG_PATH="$(ls -d output/*/"${CAM}"-*/.config 2>/dev/null | head -1 || true)"
AUTO_DIRCLEAN=""
if [ -n "$CFG_PATH" ] && [ -s "$CFG_BEFORE" ]; then
  # Symbols that appear on exactly one side, reduced to the bare symbol name.
  CHANGED="$(diff "$CFG_BEFORE" "$CFG_PATH" 2>/dev/null \
             | sed -n 's/^[<>] *\(# *\)\?\(BR2_[A-Za-z0-9_]*\).*/\2/p' | sort -u || true)"
  for sym in $CHANGED; do
    # The package that declares the symbol owns the stale artefact. Buildroot's
    # target name is the package directory name.
    owner="$(grep -rl "^[[:space:]]*config[[:space:]]\+${sym}\$" package/*/Config.in 2>/dev/null \
             | head -1 | cut -d/ -f2 || true)"
    [ -n "$owner" ] || continue
    case " $AUTO_DIRCLEAN ${DIRCLEAN:-} " in
      *" $owner "*) ;;
      *) AUTO_DIRCLEAN="$AUTO_DIRCLEAN $owner"; echo "== config changed: $sym -> rebuild $owner" ;;
    esac
  done
fi

# Per-package re-extract. `make fast` only builds what the pack step needs, so a
# dirclean alone leaves the package MISSING and silently packs a stale rootfs —
# each package must be rebuilt explicitly before packing.
MAKE_CMDS=""
for p in ${DIRCLEAN:-} $AUTO_DIRCLEAN; do
  echo "== dirclean + rebuild $p (forces re-extract so its patches re-apply)"
  MAKE_CMDS="$MAKE_CMDS BOARD=$CAM make $p-dirclean; BOARD=$CAM make $p;"
done
# Forward the fixed rootfs partition size as a make command-line variable (empty
# = upstream auto-sizing). Command-line assignment overrides the makefile's
# `ROOTFS_PARTITION_SIZE = ...` and propagates to every derived partition value.
RPS_ARG=""
[ -n "$ROOTFS_PARTITION_SIZE" ] && RPS_ARG="ROOTFS_PARTITION_SIZE=$ROOTFS_PARTITION_SIZE"
MAKE_CMDS="$MAKE_CMDS BOARD=$CAM make fast WORKFLOW=1 $RPS_ARG"

echo "== $(date +%T) starting build (make fast)${RPS_ARG:+  [$RPS_ARG]}"
in_builder "$MAKE_CMDS"

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
