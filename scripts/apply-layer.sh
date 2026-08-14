#!/bin/bash
# apply-layer.sh — copy this repo's custom layer into an upstream thingino tree.
# Idempotent: safe to run repeatedly. Overwrites tree files with our versions.
#
#   THINGINO_DIR   path to the upstream thingino-firmware checkout
#                  (default: ./build/thingino, in-repo)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
THINGINO_DIR="${THINGINO_DIR:-$REPO/build/thingino}"
CAM=tplink_tapo_c210_t23n_sc3336_rtl8188ftv

[ -d "$THINGINO_DIR" ] || { echo "!! THINGINO_DIR not found: $THINGINO_DIR (run build.sh first)"; exit 1; }

echo "== applying layer -> $THINGINO_DIR"

# 1. board layer (user/<cam> is gitignored upstream, so this is purely ours)
mkdir -p "$THINGINO_DIR/user/$CAM"
cp -v "$REPO/user/$CAM/." "$THINGINO_DIR/user/$CAM/" -r

# 2. rootfs overlay (additions + our sysctl override) — merged into upstream overlay/
cp -av "$REPO/overlay/." "$THINGINO_DIR/overlay/"

# 3. tree overrides (package edits carried as full-file overwrites)
cp -av "$REPO/tree-overrides/." "$THINGINO_DIR/"

# 3b. lean WebUI pages -> /var/www. Kept in webui/ rather than under overlay/ so
# the hand-written pages are easy to find; the CGIs they call live in
# overlay/var/www/x/. These overwrite the stock thingino-webui index/login; the
# rest of the stock UI is then deleted from the image by scripts/lean-prune.sh.
mkdir -p "$THINGINO_DIR/overlay/var/www"
cp -av "$REPO/webui/." "$THINGINO_DIR/overlay/var/www/"
rm -f "$THINGINO_DIR/overlay/var/www/.gitkeep"

# 4. board-defconfig RMEM override (idempotent).
# thingino.mk captures ISP_RMEM_MB := $(BR2_THINGINO_RMEM_MB) as an immediate
# make value from the BOARD defconfig, BEFORE local.fragment is merged — so an
# RMEM change only in local.fragment reaches .config but NOT the uboot uenv
# (osmem/rmem) generation. Set it at the source so there's no stale 26 anywhere.
DEFCFG="$THINGINO_DIR/configs/cameras/$CAM/${CAM}_defconfig"
if [ -f "$DEFCFG" ]; then
  if grep -q '^BR2_THINGINO_RMEM_MB=' "$DEFCFG"; then
    sed -i 's/^BR2_THINGINO_RMEM_MB=.*/BR2_THINGINO_RMEM_MB=20/' "$DEFCFG"
  else
    echo 'BR2_THINGINO_RMEM_MB=20' >> "$DEFCFG"
  fi
  echo "== board defconfig RMEM: $(grep '^BR2_THINGINO_RMEM_MB=' "$DEFCFG")"
fi

# ensure our init scripts + helper daemons are executable in the tree
chmod 0755 "$THINGINO_DIR/overlay/etc/init.d/S59motor" \
           "$THINGINO_DIR/overlay/etc/init.d/S97daynight" \
           "$THINGINO_DIR/overlay/etc/init.d/S61ptz-glide" \
           "$THINGINO_DIR/overlay/usr/sbin/daynight" \
           "$THINGINO_DIR/overlay/usr/sbin/ptz-glide" 2>/dev/null || true

# CGIs must be executable or uhttpd serves them as plain text
chmod 0755 "$THINGINO_DIR"/overlay/var/www/x/*.cgi 2>/dev/null || true

# post-fakeroot image prune (referenced from local.fragment)
chmod 0755 "$THINGINO_DIR/scripts/lean-prune.sh" 2>/dev/null || true

echo "== layer applied"
