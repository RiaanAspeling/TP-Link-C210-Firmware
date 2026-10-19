#!/bin/sh
# Day/night (IR-cut) control for the lean WebUI.
#
#   GET  /x/daynight.cgi              -> {ok,mode,calibrate,threshold,state,
#                                         dgain,analog,analog_min,analog_max}
#   POST /x/daynight.cgi?a=mode       -> body v=auto|day|night   set IR mode
#   POST /x/daynight.cgi?a=cal        -> body v=auto|manual[&thr=N]
#                                        auto = self-calibrating return threshold,
#                                        manual = pin the threshold to N and stop
#                                        learning.
#
# The daynight daemon (/usr/sbin/daynight) reads these control files every poll
# (~5s), so changes take effect without a restart:
#   /etc/daynight.force  auto|day|night   (day = colour/IR off, night = IR on)
#   /etc/daynight.cal    1|0              (1 = auto-calibrate, 0 = manual)
#   /etc/daynight.thr    <int>            learned (auto) or user-set (manual)
# and publishes its live state to /run/daynight.state for the readout below.

. /var/www/x/auth.sh
require_auth

FORCEFILE=/etc/daynight.force
CALFILE=/etc/daynight.cal
THRFILE=/etc/daynight.thr
STATUS=/run/daynight.state
AMIN=40; AMAX=150

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}
fail() { json_header "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }

# --- parser (whitelisted keys, no eval) -----------------------------------
F_a=""; F_v=""; F_thr=""
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
assign() {
	case "$1" in
		a)   F_a=$2 ;;
		v)   F_v=$2 ;;
		thr) F_thr=$2 ;;
	esac
}
parse_pairs() {
	local OLD_IFS=$IFS kv k v; IFS='&'
	for kv in $1; do
		[ -n "$kv" ] || continue
		k=${kv%%=*}; v=${kv#*=}; [ "$k" = "$kv" ] && v=""
		assign "$k" "$(urldecode "$v")"
	done
	IFS=$OLD_IFS
}
parse_pairs "${QUERY_STRING:-}"
if [ "${REQUEST_METHOD:-GET}" = "POST" ]; then
	len=${CONTENT_LENGTH:-0}; case "$len" in ''|*[!0-9]*) len=0 ;; esac
	if [ "$len" -gt 0 ] && [ "$len" -le 256 ]; then
		body=$(head -c "$len"); parse_pairs "$body"
	fi
fi

# --- GET / emit current state --------------------------------------------
emit() {
	local mode cal thr st dg an tok
	mode=$(cat "$FORCEFILE" 2>/dev/null); case "$mode" in auto|day|night) ;; *) mode=auto ;; esac
	cal=$(cat "$CALFILE" 2>/dev/null);    case "$cal" in 0) cal=manual ;; *) cal=auto ;; esac
	thr=$(cat "$THRFILE" 2>/dev/null);    case "$thr" in ''|*[!0-9]*) thr=0 ;; esac
	st=unknown; dg=null; an=null
	if [ -r "$STATUS" ]; then
		for tok in $(cat "$STATUS" 2>/dev/null); do
			case "$tok" in
				state=*)  st=${tok#state=} ;;
				dgain=*)  dg=${tok#dgain=} ;;
				analog=*) an=${tok#analog=} ;;
			esac
		done
		case "$dg" in ''|*[!0-9]*) dg=null ;; esac
		case "$an" in ''|*[!0-9]*) an=null ;; esac
		case "$st" in day|night) ;; *) st=unknown ;; esac
	fi
	json_header "200 OK"
	printf '{"ok":true,"mode":"%s","calibrate":"%s","threshold":%s,"state":"%s","dgain":%s,"analog":%s,"analog_min":%s,"analog_max":%s}\n' \
		"$mode" "$cal" "$thr" "$st" "$dg" "$an" "$AMIN" "$AMAX"
	exit 0
}

# --- POST a=mode ----------------------------------------------------------
set_mode() {
	case "$F_v" in auto|day|night) ;; *) fail "mode must be auto|day|night" ;; esac
	echo "$F_v" > "$FORCEFILE" 2>/dev/null || fail "could not write force file" "500 Internal Server Error"
	emit
}

# --- POST a=cal -----------------------------------------------------------
set_cal() {
	case "$F_v" in
		auto)
			echo 1 > "$CALFILE" 2>/dev/null || fail "could not write cal file" "500 Internal Server Error"
			;;
		manual)
			if [ -n "$F_thr" ]; then
				case "$F_thr" in ''|*[!0-9]*) fail "thr must be a number" ;; esac
				[ "$F_thr" -lt "$AMIN" ] && F_thr=$AMIN
				[ "$F_thr" -gt "$AMAX" ] && F_thr=$AMAX
				echo "$F_thr" > "$THRFILE" 2>/dev/null || fail "could not write threshold" "500 Internal Server Error"
			fi
			echo 0 > "$CALFILE" 2>/dev/null || fail "could not write cal file" "500 Internal Server Error"
			;;
		*) fail "cal must be auto|manual" ;;
	esac
	emit
}

# --- dispatch -------------------------------------------------------------
case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:status) emit ;;
	POST:mode)       set_mode ;;
	POST:cal)        set_cal ;;
	*)               fail "unsupported action" "405 Method Not Allowed" ;;
esac
