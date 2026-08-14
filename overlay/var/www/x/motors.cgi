#!/bin/sh
# Absolute / one-shot motor commands for the lean WebUI.
#
#   GET ?d=j    position + status (no movement)
#   GET ?d=s    stop
#   GET ?d=b    return to centre
#   GET ?d=r    home (physical homing sequence, ~20 s)
#
# Replaces the stock json-motor.cgi, which does
#
#     eval $(echo "$QUERY_STRING" | sed "s/&/;/g")
#
# — post-auth shell injection, and the reason json-motor.cgi is dropped by
# lean-prune.sh. Only the four commands the WebUI actually issues are accepted;
# the stock script also exposed relative/absolute seeks (d=g, d=h) that the
# WebUI drives through ptz.cgi and the glide daemon instead.
#
# Response shape matches the stock one ({"code":200,...,"message":<motors -j>})
# because the WebUI reads j.message.

. /var/www/x/auth.sh
require_auth

json_header() {
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}

json_error() {
	printf 'Status: 412 Precondition Failed\r\n'
	json_header
	printf '{"error":{"code":412,"message":"%s"}}\n' "$1"
	exit 0
}

# Parse without eval. `d` is the only parameter and it is matched whole.
d=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		d=j|d=s|d=b|d=r) d=${kv#d=} ;;
	esac
done
IFS=$OLD_IFS

case "$d" in
	j) ;;
	s) motors -d s >/dev/null 2>&1 ;;
	b) motors -d b >/dev/null 2>&1 ;;
	r) motors -r >/dev/null 2>&1 ;;
	*) json_error "motors-command-unsupported" ;;
esac

payload=$(motors -j 2>/dev/null) || json_error "motors-status-failed"

printf 'Status: 200 OK\r\n'
json_header
printf '{"code":200,"result":"success","message":%s}\n' "$payload"
