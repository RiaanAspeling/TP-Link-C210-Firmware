#!/bin/sh
# Same-origin MJPEG proxy for the lean WebUI.
#
# The stock /x/ch*.mjpg CGIs exec prudyntctl, which does not exist on a raptor
# build — they return nothing. raptor's own MJPEG lives on rhd (:8443/mjpeg)
# but behind HTTP basic auth on a different port, so a <img> on the WebUI
# origin cannot reach it: browsers refuse credentials in subresource URLs.
#
# So proxy it here, behind the WebUI's session cookie, exactly as raptor's
# own webrtc-whip.cgi does for WHIP signalling.

. /var/www/x/auth.sh
require_auth

CONF="/etc/raptor.conf"
BACKEND="https://127.0.0.1:8443/mjpeg"

# Read [http] credentials from raptor.conf rather than hardcoding them, so a
# changed password does not silently break the preview.
cred=$(awk -F= '
	/^\[/     { sect = $0 }
	sect == "[http]" && $1 ~ /^username[ \t]*$/ { gsub(/[ \t]/, "", $2); u = $2 }
	sect == "[http]" && $1 ~ /^password[ \t]*$/ { gsub(/[ \t]/, "", $2); p = $2 }
	END { if (u != "" ) printf "%s:%s", u, p }
' "$CONF" 2>/dev/null)

if [ -z "$cred" ]; then
	printf 'Status: 503 Service Unavailable\r\n'
	printf 'Content-Type: text/plain\r\n\r\n'
	printf 'no [http] credentials in %s\n' "$CONF"
	exit 0
fi

FPS="5"
case "${QUERY_STRING:-}" in
	*f=*) FPS=$(printf '%s' "$QUERY_STRING" | sed -n 's/.*f=\([0-9]\{1,2\}\).*/\1/p') ;;
esac
[ -z "$FPS" ] && FPS=5

# Emit our own CGI headers and stream only the body. Do NOT pass rhd's headers
# through (curl -D -): they start with "HTTP/1.1 200 OK", which is not a valid
# CGI header line and makes uhttpd drop the response entirely.
# The boundary must match the separators rhd writes into the body.
printf 'Content-Type: multipart/x-mixed-replace;boundary=raptorframe\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'Pragma: no-cache\r\n'
printf '\r\n'

# -N disables curl's output buffering so frames arrive as they are produced.
exec curl -sk -N -u "$cred" "${BACKEND}?f=${FPS}" 2>/dev/null
