#!/bin/sh
# Motion state and zone mask for the lean WebUI.
#
#   GET ?a=status            -> rvd's ivs-status verbatim (grid, zone_hits, zone_enable)
#   GET ?a=mask&v=<int>      -> set the zone mask (0 = every zone enabled)
#
# zone_hits is the raw per-zone bitmap of the last processed frame; zone_enable
# is the mask of zones allowed to raise motion. A masked zone still appears in
# zone_hits so the UI can draw it as "seen but ignored".
#
# raptorctl's positional form does not marshal an argument into the command's
# "value" field (even the stock `raptorctl rvd ivs-set-sensitivity 3` fails that
# way), so use the documented raw-JSON form instead.

. /var/www/x/auth.sh
require_auth

json_header() {
	printf 'Status: 200 OK\r\n'
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}

fail() {
	printf 'Status: 400 Bad Request\r\n'
	printf 'Content-Type: application/json\r\n\r\n'
	printf '{"ok":false,"error":"%s"}\n' "$1"
	exit 0
}

a=""; v=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		a=*) a=${kv#a=} ;;
		v=*) v=${kv#v=} ;;
	esac
done
IFS=$OLD_IFS

case "$a" in
	mask)
		# digits only, and inside the 52-zone ceiling's 64-bit bitmap
		case "$v" in
			''|*[!0-9]*) fail "bad mask" ;;
		esac
		[ "${#v}" -le 19 ] || fail "bad mask"
		raptorctl -j "{\"daemon\":\"rvd\",\"cmd\":\"ivs-set-zones\",\"value\":$v}" >/dev/null 2>&1
		;;
	status|'') ;;
	*) fail "unknown action" ;;
esac

json_header
out=$(raptorctl rvd ivs-status 2>/dev/null)
case "$out" in
	\{*) printf '%s\n' "$out" ;;
	*)   printf '{"status":"error","active":false}\n' ;;
esac
