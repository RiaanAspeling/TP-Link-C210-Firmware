#!/bin/sh
# PTZ presets for the lean WebUI.
#
# Deliberately a thin wrapper around /sbin/ptz_presets, which is the SAME store
# ONVIF uses (/etc/ptz_presets.conf, wired up in onvif.json as set/get/move/
# remove_preset). So a preset saved here appears in iSpy and IP Cam Viewer, and
# theirs appear here. Do not keep a WebUI-private list.
#
#   GET ?a=list                     -> {"presets":[{"n":0,"name":"Door","x":..,"y":..}]}
#   GET ?a=goto&n=<num>             -> recall
#   GET ?a=save&n=<num|-1>&name=..  -> store CURRENT position (-1 = first free slot)
#   GET ?a=del&n=<num>              -> clear slot
#
# ptz_presets interpolates the name straight into `sed s/^N=.*/N=NAME,x,y/`, so
# a name containing / or & would corrupt the config or inject a sed command.
# It is sanitised here, hard, before it ever reaches that script.

. /var/www/x/auth.sh
require_auth

PRESETS=/sbin/ptz_presets

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

url_decode() {
	printf '%b' "$(printf '%s' "$1" | sed 's/+/ /g; s/%\([0-9a-fA-F][0-9a-fA-F]\)/\\x\1/g')"
}

# Parse the query string without eval (the stock json-motor.cgi eval()s it).
a=""; n=""; name=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		a=*)    a=${kv#a=} ;;
		n=*)    n=${kv#n=} ;;
		name=*) name=${kv#name=} ;;
	esac
done
IFS=$OLD_IFS

# Slot number: digits only, or the literal -1 for "first free".
case "$n" in
	''|*[!0-9-]*) n_ok=0 ;;
	-1)           n_ok=1 ;;
	*[!0-9]*)     n_ok=0 ;;
	*)            n_ok=1 ;;
esac

list() {
	json_header
	printf '{"ok":true,"presets":['
	first=1
	# ptz_presets -g emits "NUM=NAME,X,Y"; empty slots are "NUM=,,"
	$PRESETS -g 2>/dev/null | while IFS='=,' read -r pnum pname px py; do
		[ -n "$pnum" ] || continue
		[ -n "$px" ] && [ -n "$py" ] || continue
		[ "$first" = 1 ] || printf ','
		first=0
		printf '{"n":%s,"name":"%s","x":%s,"y":%s}' "$pnum" "$pname" "$px" "$py"
	done
	printf ']}\n'
}

case "$a" in
	list|'')
		list
		;;
	goto)
		[ "$n_ok" = 1 ] && [ "$n" != "-1" ] || fail "bad preset number"
		$PRESETS "$n" >/dev/null 2>&1
		json_header
		printf '{"ok":true,"pos":%s}\n' "$(motors -j 2>/dev/null || echo null)"
		;;
	save)
		[ "$n_ok" = 1 ] || fail "bad preset number"
		name=$(url_decode "$name")
		# Keep only characters that are safe inside a sed replacement and a
		# NUM=NAME,X,Y config line: no / & , = newline. Cap the length.
		name=$(printf '%s' "$name" | tr -cd 'A-Za-z0-9 _-' | cut -c1-20)
		# Trim leading/trailing spaces; fall back to a generated name.
		name=$(printf '%s' "$name" | sed 's/^ *//; s/ *$//')
		[ -n "$name" ] || name="Preset"
		$PRESETS -a "$n" "$name" >/dev/null 2>&1 || fail "save failed"
		list
		;;
	del)
		[ "$n_ok" = 1 ] && [ "$n" != "-1" ] || fail "bad preset number"
		$PRESETS -r "$n" >/dev/null 2>&1
		list
		;;
	*)
		fail "unknown action"
		;;
esac
