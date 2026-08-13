#!/bin/sh
# Smooth press-and-hold PTZ for the lean WebUI.
#
# Do NOT drive this with repeated `motors -d g` steps. Per ptz-glide(8): small
# relative moves are fork-rate limited on this SoC (~3/sec) and look jerky.
# Smooth motion comes from the motor's own profiled seek, which the ptz-glide
# daemon already drives for ONVIF. Write the same intent file and the WebUI
# gets identical motion to iSpy.
#
#   /tmp/ptz_glide = "SX SY EVENT TS"
#     SX,SY  direction in {-1,0,1}    EVENT  1 = move, 0 = stop
#     TS     /proc/uptime seconds when the request arrived
#
# Usage: ptz.cgi?sx=<-1|0|1>&sy=<-1|0|1>&e=<0|1>

. /var/www/x/auth.sh
require_auth

STATE=/tmp/ptz_glide

sx=0; sy=0; e=0
# Parse without eval - the stock json-motor.cgi eval()s QUERY_STRING, which is
# post-auth shell injection. Accept only the three values we expect.
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		sx=-1|sx=0|sx=1) sx=${kv#sx=} ;;
		sy=-1|sy=0|sy=1) sy=${kv#sy=} ;;
		e=0|e=1)         e=${kv#e=} ;;
	esac
done
IFS=$OLD_IFS

ts=$(cut -d. -f1 /proc/uptime 2>/dev/null)
[ -z "$ts" ] && ts=0

printf 'Status: 200 OK\r\n'
printf 'Content-Type: application/json\r\n'
printf 'Cache-Control: no-store\r\n'
printf '\r\n'

if [ ! -e "$STATE" ] && ! pgrep -f ptz-glide >/dev/null 2>&1; then
	printf '{"ok":false,"error":"ptz-glide not running"}\n'
	exit 0
fi

printf '%s %s %s %s\n' "$sx" "$sy" "$e" "$ts" > "$STATE"
printf '{"ok":true,"sx":%s,"sy":%s,"e":%s}\n' "$sx" "$sy" "$e"
