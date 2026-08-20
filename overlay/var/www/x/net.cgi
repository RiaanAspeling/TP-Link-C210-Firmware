#!/bin/sh
# Network (IP) configuration for the wifi interface — DHCP or static.
#
#   GET  /x/net.cgi          -> {mode, address, netmask, gateway, dns1, dns2,
#                                mac, current_ip}
#   POST /x/net.cgi?a=set     -> body mode=dhcp | mode=static&address=&netmask=&
#                                gateway=&dns1=&dns2=  ; writes the interface
#                                config and bounces the interface to apply it.
#
# The interface is configured Debian-style in /etc/network/interfaces.d/<iface>
# (`iface <if> inet dhcp` or `... inet static` + address/netmask/gateway); the
# S40network init script applies it at boot, and `ifdown`/`ifup` apply it live.
# We bounce the interface from a BACKGROUND subshell after a short delay so the
# HTTP response reaches the browser before the connection drops. Static mode
# also writes DNS to resolv.conf (no DHCP lease to provide it).
#
# Every value is validated as a dotted-quad before it touches a file. A wrong
# static IP can make the camera unreachable — the UI warns about that.

. /var/www/x/auth.sh
require_auth

IFACE=wlan0
IFDIR=/etc/network/interfaces.d
IFFILE="$IFDIR/$IFACE"
RESOLV=/etc/resolv.conf

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n\r\n'
}
fail() { json_header "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }

# --- parser (whitelisted keys, no eval) -----------------------------------
F_a=""; F_mode=""; F_address=""; F_netmask=""; F_gateway=""; F_dns1=""; F_dns2=""
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
assign() {
	case "$1" in
		a)       F_a=$2 ;;
		mode)    F_mode=$2 ;;
		address) F_address=$2 ;;
		netmask) F_netmask=$2 ;;
		gateway) F_gateway=$2 ;;
		dns1)    F_dns1=$2 ;;
		dns2)    F_dns2=$2 ;;
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
	if [ "$len" -gt 0 ] && [ "$len" -le 2048 ]; then
		body=$(head -c "$len"); parse_pairs "$body"
	fi
fi

# --- dotted-quad validator ------------------------------------------------
valid_ip() {
	local ip="$1" o OLD_IFS=$IFS
	IFS=.; set -- $ip; IFS=$OLD_IFS
	[ $# -eq 4 ] || return 1
	for o in "$@"; do
		case "$o" in ''|*[!0-9]*) return 1 ;; esac
		[ "${#o}" -le 3 ] && [ "$o" -le 255 ] || return 1
	done
	return 0
}

# --- GET: current config + live state -------------------------------------
emit_get() {
	local mode=dhcp addr="" mask="" gw="" d1="" d2="" mac="" live=""
	if grep -q 'inet static' "$IFFILE" 2>/dev/null; then
		mode=static
		addr=$(sed -nE 's/^[[:space:]]*address[[:space:]]+([^[:space:]]+).*/\1/p' "$IFFILE")
		mask=$(sed -nE 's/^[[:space:]]*netmask[[:space:]]+([^[:space:]]+).*/\1/p' "$IFFILE")
		gw=$(sed -nE 's/^[[:space:]]*gateway[[:space:]]+([^[:space:]]+).*/\1/p' "$IFFILE")
	fi
	live=$(ifconfig "$IFACE" 2>/dev/null | sed -nE 's/.*inet addr:([0-9.]+).*/\1/p')
	[ -z "$mask" ] && mask=$(ifconfig "$IFACE" 2>/dev/null | sed -nE 's/.*Mask:([0-9.]+).*/\1/p')
	[ -z "$gw" ]   && gw=$(route -n 2>/dev/null | awk '$1=="0.0.0.0"{print $2; exit}')
	[ -z "$addr" ] && addr="$live"
	mac=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null)
	set -- $(sed -nE 's/^[[:space:]]*nameserver[[:space:]]+([0-9.]+).*/\1/p' "$RESOLV" 2>/dev/null)
	d1="$1"; d2="$2"
	json_header
	printf '{"ok":true,"iface":"%s","mode":"%s","address":"%s","netmask":"%s","gateway":"%s","dns1":"%s","dns2":"%s","mac":"%s","current_ip":"%s"}\n' \
		"$IFACE" "$mode" "$addr" "$mask" "$gw" "$d1" "$d2" "$mac" "$live"
	exit 0
}

# Bounce the interface from the background so the response flushes first.
apply_net() {
	( sleep 1; ifdown "$IFACE"; ifup "$IFACE" ) >/dev/null 2>&1 &
}

# --- POST a=set -----------------------------------------------------------
set_net() {
	[ "${REQUEST_METHOD:-GET}" = POST ] || fail "POST required" "405 Method Not Allowed"
	[ -d "$IFDIR" ] || mkdir -p "$IFDIR"
	case "$F_mode" in
		dhcp)
			{ printf 'auto %s\n' "$IFACE"
			  printf 'iface %s inet dhcp\n' "$IFACE"
			  printf '\tdhcp-v6-enabled true\n'; } > "$IFFILE" \
				|| fail "could not write config" "500 Internal Server Error"
			json_header; printf '{"ok":true,"mode":"dhcp"}\n'
			apply_net; exit 0 ;;
		static)
			valid_ip "$F_address" || fail "invalid IP address"
			valid_ip "$F_netmask" || fail "invalid subnet mask"
			[ -n "$F_gateway" ] && { valid_ip "$F_gateway" || fail "invalid gateway"; }
			[ -n "$F_dns1" ] && { valid_ip "$F_dns1" || fail "invalid DNS 1"; }
			[ -n "$F_dns2" ] && { valid_ip "$F_dns2" || fail "invalid DNS 2"; }
			{ printf 'auto %s\n' "$IFACE"
			  printf 'iface %s inet static\n' "$IFACE"
			  printf '\taddress %s\n' "$F_address"
			  printf '\tnetmask %s\n' "$F_netmask"
			  [ -n "$F_gateway" ] && printf '\tgateway %s\n' "$F_gateway"; } > "$IFFILE" \
				|| fail "could not write config" "500 Internal Server Error"
			# Static has no DHCP lease to supply DNS, so set it if provided.
			if [ -n "$F_dns1" ] || [ -n "$F_dns2" ]; then
				{ [ -n "$F_dns1" ] && printf 'nameserver %s\n' "$F_dns1"
				  [ -n "$F_dns2" ] && printf 'nameserver %s\n' "$F_dns2"; } > "$RESOLV" 2>/dev/null
			fi
			json_header; printf '{"ok":true,"mode":"static","address":"%s"}\n' "$F_address"
			apply_net; exit 0 ;;
		*) fail "mode must be dhcp or static" ;;
	esac
}

case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:status) emit_get ;;
	POST:set)        set_net ;;
	*)               fail "unsupported action" "405 Method Not Allowed" ;;
esac
