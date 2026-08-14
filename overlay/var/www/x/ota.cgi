#!/bin/sh
# OTA firmware update for the lean WebUI.
#
#   POST /x/ota.cgi?sha256=<hex>&size=<bytes>[&mode=full]
#       body = the raw firmware image (application/octet-stream)
#
# The body is streamed to /tmp/ota.bin, its sha256 is compared against the one
# the BROWSER computed over the same bytes (crypto.subtle in index.html), the
# leading magic is checked, and then /usr/sbin/sysupgrade is handed the file.
#
# Two image types, distinguished by the first four bytes:
#
#   squashfs (68 73 71 73)  -> rootfs-only. Flashes mtd4 and leaves the jffs2
#                              overlay (wifi + password) untouched. Common case.
#   u-boot   (06 05 04 03)  -> full image. Rewrites kernel + rootfs + BOOTLOADER.
#                              Can brick the camera, so the caller must pass
#                              mode=full to confirm it was deliberate.
#
# We do NOT flash here. `sysupgrade` owns that. Both types go through the SAME
# invocation — `sysupgrade -x <file>` in "local" mode, which auto-detects by
# magic — and we deliberately DO NOT pass `-r`.
#
#   WHY NOT `-r`: the rootfs-only branch of sysupgrade `exit 0`s before it sets
#   up the tmpfs busybox applet symlinks and PATH=/tmp/sysupgrade, so its flashcp
#   runs from /usr/sbin ON THE ROOTFS IT IS ERASING. Once mtd4 is blank the
#   flasher faults from erased flash and dies mid-write, leaving mtd4 fully
#   erased -> unbootable. This bricked a camera on 2026-08-14 (CH341A dump: mtd4
#   100% 0xFF). Local mode instead hands a squashfs to sysupgrade-stage2, which
#   runs flashcp from tmpfs (PATH=/tmp/sysupgrade) with the streamer stopped and
#   the watchdog taken over from RAM. That is the intended, safe path.
#
# busybox is dynamically linked, so even stage2 needs libc to stay resident
# through the erase. It does when memory pressure is low, so before handing off
# we stop the streamer and drop caches ourselves — the same thing that made a
# manual `flashcp /dev/mtd4` reliable. `-x` keeps sysupgrade fully offline (no
# GitHub self-update). Touching /tmp/webupgrade tells it to leave httpd running
# so this response reaches the browser before the reboot.

. /var/www/x/auth.sh
require_auth

OTA_BIN=/tmp/ota.bin
OTA_LOG=/tmp/ota.log
SQUASHFS_MAGIC=68737173
UBOOT_MAGIC=06050403
MTD4_SIZE=5373952   # 0x520000, the rootfs partition
FULL_SIZE=8388608   # 0x800000, the whole flash

json() {
	printf 'Status: %s\r\n' "$1"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n\r\n'
	shift
	printf '%s\n' "$*"
}

fail() {
	rm -f "$OTA_BIN"
	json '400 Bad Request' "{\"ok\":false,\"error\":\"$1\"}"
	exit 0
}

# --- request must be a POST with a body -----------------------------------
[ "${REQUEST_METHOD:-}" = "POST" ] || fail "POST required"

len="${CONTENT_LENGTH:-0}"
case "$len" in
	''|*[!0-9]*) fail "missing Content-Length" ;;
esac
[ "$len" -gt 0 ] || fail "empty body"
# A rootfs is <=5 MB and the full image is 8 MB; refuse anything larger before
# it can fill tmpfs.
[ "$len" -le "$FULL_SIZE" ] || fail "image too large ($len > $FULL_SIZE)"

# --- query string (no eval) -----------------------------------------------
q_sha=""; q_size=""; q_mode=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		sha256=*) q_sha=${kv#sha256=} ;;
		size=*)   q_size=${kv#size=} ;;
		mode=*)   q_mode=${kv#mode=} ;;
	esac
done
IFS=$OLD_IFS

# Normalise and sanity-check the client's hash: 64 lowercase hex chars.
q_sha=$(printf '%s' "$q_sha" | tr 'A-F' 'a-f')
case "$q_sha" in
	*[!0-9a-f]*) fail "bad sha256" ;;
esac
[ "${#q_sha}" -eq 64 ] || fail "bad sha256 length"

# --- receive the body ------------------------------------------------------
# head -c bounds the read to exactly Content-Length, so a chunked or padded
# request cannot overrun. Fail closed if the write is short.
if ! head -c "$len" > "$OTA_BIN"; then
	fail "upload write failed"
fi
got=$(stat -c%s "$OTA_BIN" 2>/dev/null || echo 0)
[ "$got" = "$len" ] || fail "short upload ($got of $len)"
if [ -n "$q_size" ]; then
	[ "$q_size" = "$len" ] || fail "size mismatch (declared $q_size, got $len)"
fi

# --- integrity: our sha256 must equal the browser's -----------------------
have=$(sha256sum "$OTA_BIN" 2>/dev/null | cut -d' ' -f1)
[ "$have" = "$q_sha" ] || fail "sha256 mismatch (transfer corrupted)"

# --- image type by leading magic ------------------------------------------
magic=$(xxd -l 4 -p "$OTA_BIN" 2>/dev/null)
case "$magic" in
	"$SQUASHFS_MAGIC")
		imgtype="rootfs"
		[ "$got" -le "$MTD4_SIZE" ] || fail "rootfs larger than partition ($got > $MTD4_SIZE)"
		;;
	"$UBOOT_MAGIC")
		imgtype="full"
		# The bootloader-rewriting path is gated: the UI only sends mode=full
		# after an explicit second confirmation.
		[ "$q_mode" = "full" ] || fail "full image needs explicit recovery confirmation"
		;;
	*)
		fail "unrecognised image (magic $magic; expected squashfs or u-boot)"
		;;
esac

[ -x /usr/sbin/sysupgrade ] || fail "sysupgrade not installed"

# --- hand off to sysupgrade, detached -------------------------------------
# Keep httpd alive through the flash so this response is delivered; sysupgrade
# checks for this flag in stop_services.
: > /tmp/webupgrade

# Free RAM before the flash so uClibc stays resident while mtd4 is erased (see
# the header): stop the streamer, wait for it to release, and drop caches. Then
# hand off with local mode (`-x`, no `-r`). setsid + </dev/null fully detaches
# the flasher from this CGI and from uhttpd — it must outlive the request and
# must not inherit a controlling tty (the full-image countdown waits for a
# keypress that will never come, then proceeds, which is what we want).
setsid sh -c "
	[ -x /etc/init.d/S31raptor ] && /etc/init.d/S31raptor stop
	i=0; while pidof rvd >/dev/null 2>&1 && [ \$i -lt 10 ]; do i=\$((i+1)); sleep 1; done
	sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
	sysupgrade -x $OTA_BIN > $OTA_LOG 2>&1
" </dev/null >/dev/null 2>&1 &

json '200 OK' "{\"ok\":true,\"type\":\"$imgtype\",\"bytes\":$got}"
exit 0
