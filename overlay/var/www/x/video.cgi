#!/bin/sh
# Encoder settings (frame rate / bitrate) for the lean WebUI.
#
#   GET ?a=get                                  -> rvd stream status
#   GET ?a=set&ch=<0|1>[&fps=N][&bitrate=N]     -> apply now and persist
#
# These are ENCODER settings: they change the stream for every consumer —
# RTSP, ONVIF, recordings — not just this browser. Only the stream *selector*
# in the UI is per-session.
#
# Persistence is a targeted edit of raptor.conf rather than `raptorctl config
# save`, which writes the whole running config back and adds unrelated state
# (OSD positions and so on) to the file.

. /var/www/x/auth.sh
require_auth

CONF=/etc/raptor.conf

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

# conf_set <section> <key> <value>
# Places "key = value" immediately after the [section] header and drops any
# other uncommented copy in that section. Commented lines start with '#', so
# the documentation blocks in raptor.conf are left intact. Idempotent.
conf_set() {
	_sec="[$1]"; _key="$2"; _val="$3"
	_tmp=$(mktemp /tmp/raptorconf.XXXXXX) || return 1
	awk -v sec="$_sec" -v key="$_key" -v val="$_val" '
		BEGIN { insec = 0 }
		/^\[/ {
			insec = ($0 == sec)
			print
			if (insec) print key " = " val
			next
		}
		insec && $0 ~ "^[ \t]*" key "[ \t]*=" { next }
		{ print }
	' "$CONF" > "$_tmp" && cat "$_tmp" > "$CONF"
	rm -f "$_tmp"
}

a=""; ch=""; fps=""; bitrate=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		a=*)       a=${kv#a=} ;;
		ch=*)      ch=${kv#ch=} ;;
		fps=*)     fps=${kv#fps=} ;;
		bitrate=*) bitrate=${kv#bitrate=} ;;
	esac
done
IFS=$OLD_IFS

status() {
	json_header
	out=$(raptorctl rvd status 2>/dev/null)
	case "$out" in
		\{*) printf '%s\n' "$out" ;;
		*)   printf '{"status":"error"}\n' ;;
	esac
}

case "$a" in
	get|'')
		status
		;;
	set)
		case "$ch" in 0|1) ;; *) fail "bad channel" ;; esac

		if [ -n "$fps" ]; then
			case "$fps" in ''|*[!0-9]*) fail "bad fps" ;; esac
			[ "$fps" -ge 1 ] && [ "$fps" -le 30 ] || fail "fps out of range (1-30)"
			raptorctl rvd set-fps "$ch" "$fps" >/dev/null 2>&1 || fail "set-fps failed"
			conf_set "stream$ch" fps "$fps"
		fi

		if [ -n "$bitrate" ]; then
			case "$bitrate" in ''|*[!0-9]*) fail "bad bitrate" ;; esac
			# 200 kbps .. 8 Mbps; below that 1080p is unusable, above it the
			# radio and the SoC both struggle.
			[ "$bitrate" -ge 200000 ] && [ "$bitrate" -le 8000000 ] || fail "bitrate out of range"
			raptorctl rvd set-bitrate "$ch" "$bitrate" >/dev/null 2>&1 || fail "set-bitrate failed"
			conf_set "stream$ch" bitrate "$bitrate"
		fi

		status
		;;
	*)
		fail "unknown action"
		;;
esac
