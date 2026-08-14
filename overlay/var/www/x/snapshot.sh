#!/bin/sh
# Shared snapshot proxy for the ONVIF snapshot URIs (x/ch0.jpg, x/ch1.jpg).
#
# The stock ch*.jpg shell out to `prudyntctl snapshot`, which does not exist on
# a raptor build — so ONVIF snapshots have been silently broken on this camera
# since the switch to raptor. Every client that offers a still image (iSpy's
# thumbnail, IP Cam Viewer's preview, the ONVIF Media GetSnapshotUri response)
# got an empty body.
#
# raptor's rhd serves JPEG on :8443/snapshot, but behind HTTP basic auth on a
# different port, so it cannot be exposed directly. Proxy it here, same as
# mjpeg.cgi does for the MJPEG stream.
#
# NOTE: rhd ignores a channel parameter — ?ch=0 and ?ch=1 both return the main
# 1920x1080 encoder. Verified by decoding the JPEG dimensions of both. So ch1.jpg
# deliberately returns the same image as ch0.jpg rather than pretending to offer
# a substream still.

RAPTOR_CONF="/etc/raptor.conf"
RAPTOR_SNAPSHOT="https://127.0.0.1:8443/snapshot"

# Read the [http] credentials from raptor.conf rather than hardcoding them, so a
# changed password does not silently break snapshots.
raptor_http_cred() {
	awk -F= '
		/^\[/     { sect = $0 }
		sect == "[http]" && $1 ~ /^username[ \t]*$/ { gsub(/[ \t]/, "", $2); u = $2 }
		sect == "[http]" && $1 ~ /^password[ \t]*$/ { gsub(/[ \t]/, "", $2); p = $2 }
		END { if (u != "") printf "%s:%s", u, p }
	' "$RAPTOR_CONF" 2>/dev/null
}

# serve_snapshot — emit CGI headers then stream the JPEG body.
#
# Emit our own headers and only the body: passing rhd's headers through would
# start the response with "HTTP/1.1 200 OK", which is not a valid CGI header
# line and makes uhttpd drop the whole response.
serve_snapshot() {
	_cred=$(raptor_http_cred)
	if [ -z "$_cred" ]; then
		printf 'Status: 503 Service Unavailable\r\n'
		printf 'Content-Type: text/plain\r\n\r\n'
		printf 'no [http] credentials in %s\n' "$RAPTOR_CONF"
		exit 0
	fi

	printf 'Content-Type: image/jpeg\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf 'Pragma: no-cache\r\n'
	printf '\r\n'

	exec curl -sk --max-time 10 -u "$_cred" "$RAPTOR_SNAPSHOT" 2>/dev/null
}
