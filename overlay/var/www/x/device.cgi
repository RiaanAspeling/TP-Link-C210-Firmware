#!/bin/sh
# Device identity + system control for the lean WebUI.
#
#   GET  /x/device.cgi         -> {name, kernel, soc, version, uptime_sec,
#                                  mem_total_kb, mem_avail_kb}
#   POST /x/device.cgi?a=name  -> set the device name (body: name=<n>)
#                                 writes /etc/hostname + /etc/hosts, applies the
#                                 hostname live, and sets the centre video
#                                 overlay text to match. ONVIF discovery uses the
#                                 hostname, so its device name follows too.
#   POST /x/device.cgi?a=reboot-> reboot the camera (body: confirm=REBOOT)
#
# The device name is constrained to a valid hostname (letters, digits, hyphen)
# because it becomes the system hostname; that same string is written as the
# literal text of the [osd.camera] overlay element via raptorctl.

. /var/www/x/auth.sh
require_auth

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}
fail() { json_header "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }

# --- parser ---------------------------------------------------------------
F_a=""; F_name=""; F_confirm=""
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
assign() {
	case "$1" in
		a)       F_a=$2 ;;
		name)    F_name=$2 ;;
		confirm) F_confirm=$2 ;;
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
	if [ "$len" -gt 0 ] && [ "$len" -le 1024 ]; then
		body=$(head -c "$len"); parse_pairs "$body"
	fi
fi

# A valid hostname label: 1..32 chars, letters/digits/hyphen, not starting or
# ending with a hyphen.
valid_hostname() {
	local h="$1"
	[ ${#h} -ge 1 ] && [ ${#h} -le 32 ] || return 1
	case "$h" in
		-*|*-) return 1 ;;
		*[!A-Za-z0-9-]*) return 1 ;;
		*) return 0 ;;
	esac
}

# --- GET: device info -----------------------------------------------------
emit_info() {
	local name kernel soc version up memtotal memavail
	name=$(hostname 2>/dev/null)
	kernel=$(uname -r 2>/dev/null)
	# SoC / version are best-effort: read only if the source exists, never fail.
	soc=$(fw_printenv -n soc 2>/dev/null)
	[ -n "$soc" ] || soc=$(uname -m 2>/dev/null)
	if [ -f /etc/os-release ]; then
		version=$(sed -n 's/^GITVERSION=//p; s/^VERSION=//p' /etc/os-release 2>/dev/null | head -1 | tr -d '"')
	fi
	up=$(cut -d. -f1 /proc/uptime 2>/dev/null); case "$up" in ''|*[!0-9]*) up=0 ;; esac
	memtotal=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null); : "${memtotal:=0}"
	memavail=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
	[ -n "$memavail" ] || memavail=$(awk '/^MemFree:/{print $2; exit}' /proc/meminfo 2>/dev/null)
	: "${memavail:=0}"

	# JSON-escape the free-text fields (backslash + quote) so an odd hostname or
	# version string can't break the response.
	esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
	json_header "200 OK"
	printf '{"ok":true,"name":"%s","kernel":"%s","soc":"%s","version":"%s","uptime_sec":%s,"mem_total_kb":%s,"mem_avail_kb":%s}\n' \
		"$(esc "$name")" "$(esc "$kernel")" "$(esc "$soc")" "$(esc "$version")" "$up" "$memtotal" "$memavail"
	exit 0
}

# --- POST a=name ----------------------------------------------------------
set_name() {
	valid_hostname "$F_name" || fail "name: 1-32 chars, letters/digits/hyphen, no leading/trailing hyphen"

	# Persist + apply the hostname.
	echo "$F_name" > /etc/hostname 2>/dev/null || fail "could not write /etc/hostname" "500 Internal Server Error"
	hostname "$F_name" 2>/dev/null
	if grep -q '^127.0.1.1' /etc/hosts 2>/dev/null; then
		sed -i "s/^127.0.1.1.*/127.0.1.1\t$F_name/" /etc/hosts 2>/dev/null
	else
		printf '127.0.1.1\t%s\n' "$F_name" >> /etc/hosts 2>/dev/null
	fi

	# Mirror the name into the centre video overlay ([osd.camera]) and persist.
	# Best-effort: the overlay daemon may be disabled; the hostname still stands.
	raptorctl rod set-element camera template="$F_name" >/dev/null 2>&1
	raptorctl config save >/dev/null 2>&1

	json_header "200 OK"
	printf '{"ok":true,"name":"%s"}\n' "$F_name"
	exit 0
}

# --- POST a=reboot --------------------------------------------------------
do_reboot() {
	[ "$F_confirm" = "REBOOT" ] || fail "reboot needs confirm=REBOOT" "428 Precondition Required"
	json_header "200 OK"
	printf '{"ok":true,"rebooting":true}\n'
	# Let the response flush before the box goes down.
	(sleep 1; reboot) >/dev/null 2>&1 &
	exit 0
}

# --- dispatch -------------------------------------------------------------
case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:info) emit_info ;;
	POST:name)     set_name ;;
	POST:reboot)   do_reboot ;;
	*)             fail "unsupported action" "405 Method Not Allowed" ;;
esac
