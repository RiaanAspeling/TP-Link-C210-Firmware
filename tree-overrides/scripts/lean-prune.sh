#!/bin/sh
# lean-prune.sh — drop files no longer reachable on this build.
#
# Wired up as BR2_ROOTFS_POST_FAKEROOT_SCRIPT (see local.fragment). That hook
# is deliberate: fs/common.mk rsyncs target/ into a throwaway directory and
# rebinds TARGET_DIR to the copy before running us, then deletes the copy. So
# everything here affects the packed image ONLY — the real output/*/target/ is
# never touched, nothing goes stale, and no CLEAN=1 is needed to undo a change.
#
#   $1 = TARGET_DIR (the throwaway copy). Do NOT use $TARGET_DIR: Buildroot
#        exports that globally and it still points at the real tree.
#
# Two jobs:
#   1. Remove the stock thingino-webui pages our lean UI replaced (~1 MB).
#   2. Remove Ingenic accelerator blobs no longer selected by the config.
#      Buildroot never prunes target/ on package removal, so a lib installed by
#      an earlier config lingers forever. Upstream's rootfs_script.sh already
#      does exactly this for libstdc++; libjzdl was simply missed.
#
# Deny-by-default: /var/www is rebuilt from a keep list, so a page added by a
# future upstream package is dropped unless it is named here. That is the point
# — flash headroom on mtd4 is ~100 KB and a silent addition would break the
# build at pack time with a much less obvious error.

set -eu

TGT="$1"
[ -n "$TGT" ] && [ -d "$TGT" ] || { echo "lean-prune: bad TARGET_DIR '$TGT'"; exit 1; }

# Buildroot exports BR2_CONFIG globally, but EXTRA_ENV only passes O= explicitly,
# so fall back to $O/.config rather than trust inheritance through fakeroot. An
# empty CFG would make every guard below vacuous and delete libraries that a
# future config actually selects.
CFG="${BR2_CONFIG:-}"
[ -n "$CFG" ] && [ -f "$CFG" ] || CFG="${O:-}/.config"
[ -f "$CFG" ] || { echo "lean-prune: FATAL - cannot locate .config (BR2_CONFIG='${BR2_CONFIG:-}' O='${O:-}')"; exit 1; }

# --------------------------------------------------------------------------
# 1. Ingenic accelerator blobs
# --------------------------------------------------------------------------
# libjzdl is only needed by the JZDL/YOLO inference paths (IVS_DETECT). We run
# our own frame-difference detector inside rvd instead, so nothing links it.
prune_unselected_lib() {
	symbol="$1"; shift
	if grep -q "^${symbol}=y" "$CFG" 2>/dev/null; then
		return 0
	fi
	for f in "$@"; do
		rm -vf "$TGT/usr/lib/$f"
	done
}

prune_unselected_lib BR2_PACKAGE_INGENIC_LIB_JZDL libjzdl.m.so
prune_unselected_lib BR2_PACKAGE_INGENIC_LIB_PERSONDET \
	libpersonDet_inf.so libjzdl.so \
	libmxu_core.so libmxu_imgproc.so libmxu_merge.so libmxu_video.so

# --------------------------------------------------------------------------
# 2. Stock web UI
# --------------------------------------------------------------------------
WWW="$TGT/var/www"
[ -d "$WWW" ] || exit 0

# Top level: our two pages, and nothing else. index.cgi is dead on this build —
# uhttpd's CGI prefix is /x, so /var/www/index.cgi is served as plain text
# rather than executed, and "/" is served straight from index.html.
KEEP_ROOT="index.html login.html"

# /var/www/x: session plumbing, the ONVIF snapshot endpoints, and ours.
#
#   auth.sh session.sh login.cgi logout.cgi session-status.cgi
#       the session layer our pages and every CGI below depend on;
#       onvif.cgi sources auth.sh too.
#   ch0.jpg ch1.jpg snapshot.sh
#       named as the snapshot URIs in /etc/onvif.json, and ch0.jpg is also the
#       target of the /var/www/onvif/image.cgi symlink. Ours, proxying rhd —
#       the stock pair shelled out to prudyntctl, absent on a raptor build.
#   legacy-url-recovery.cgi
#       uhttpd's -E handler, set in /etc/default/uhttpd.
#   reboot.cgi
#       not used by our UI, but 283 bytes and the one recovery action worth
#       having without SSH.
#
# Everything else goes, including run.cgi and texteditor.cgi (arbitrary command
# execution and arbitrary file writes) and the json-*.cgi family, most of which
# eval() QUERY_STRING. Our own motors.cgi replaces json-motor.cgi.
KEEP_X="auth.sh session.sh login.cgi logout.cgi session-status.cgi
        ch0.jpg ch1.jpg snapshot.sh legacy-url-recovery.cgi reboot.cgi
        mjpeg.cgi motion.cgi motors.cgi ota.cgi presets.cgi ptz.cgi video.cgi
        webrtc-whip.cgi"

keeps_root=" $(echo $KEEP_ROOT) "
keeps_x=" $(echo $KEEP_X) "

removed=0
for f in "$WWW"/*; do
	[ -e "$f" ] || continue
	b="${f##*/}"
	# a/ is the stock asset bundle — our pages reference no /a/ asset at all.
	# onvif/ is the live ONVIF service and must stay.
	case "$b" in
		x|onvif) continue ;;
		a) rm -rf "$f"; removed=$((removed + 1)); continue ;;
	esac
	case "$keeps_root" in
		*" $b "*) continue ;;
	esac
	rm -rf "$f"
	removed=$((removed + 1))
done

for f in "$WWW"/x/*; do
	[ -e "$f" ] || continue
	b="${f##*/}"
	case "$keeps_x" in
		*" $b "*) continue ;;
	esac
	rm -rf "$f"
	removed=$((removed + 1))
done

echo "lean-prune: removed $removed entries under /var/www"

# Fail loudly if the prune ate something our UI needs — a typo in KEEP_X would
# otherwise ship a firmware whose login page 404s, and the camera is a flash
# cycle away from being testable.
for need in index.html login.html \
            x/auth.sh x/session.sh x/login.cgi x/logout.cgi \
            x/ptz.cgi x/motors.cgi x/presets.cgi x/motion.cgi x/video.cgi \
            x/webrtc-whip.cgi x/mjpeg.cgi x/snapshot.sh x/ch0.jpg x/ch1.jpg \
            x/ota.cgi; do
	[ -e "$WWW/$need" ] || { echo "lean-prune: FATAL - $need missing after prune"; exit 1; }
done
[ -x "$WWW/onvif/onvif.cgi" ] || { echo "lean-prune: FATAL - onvif.cgi missing"; exit 1; }
