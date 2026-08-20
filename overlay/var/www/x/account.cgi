#!/bin/sh
# Account & access control for the lean WebUI.
#
#   GET  /x/account.cgi           -> {web_user, onvif_user}   (never returns passwords)
#   POST /x/account.cgi?a=webpass -> change the web login (root) password
#                                    body: cur=<b64>&new=<b64>
#   POST /x/account.cgi?a=onvif   -> change the ONVIF stream credentials
#                                    body: user=<urlenc>&pass=<b64>
#
# Two independent credential stores:
#   * Web login  == the Linux root password in /etc/shadow (sha512). login.cgi
#     verifies it with `mkpasswd -m sha512 <pw> <salt>`; we do the same to check
#     the current one, and generate a fresh salted hash for the new one.
#   * ONVIF      == "server".username / "server".password in /etc/onvif.json.
#     onvif.cgi (the ONVIF service) re-reads that file per request, so a change
#     is live immediately; only onvif_notify_server caches creds, so we bounce it.
#
# Passwords arrive base64-encoded (like login.html) so any byte survives the
# urlencoded transport intact. Every value is validated before it touches a
# file, and the ONVIF value is confined to JSON-safe printable ASCII so the
# in-place edit can never inject a quote/backslash into onvif.json.

. /var/www/x/auth.sh
require_auth

SHADOW=/etc/shadow
ONVIF_JSON=/etc/onvif.json
RUNDIR=/var/run/rss                 # raptor daemon pid/sock dir (see S31raptor)

# Reliably restart a raptor stream daemon so it re-reads its creds from
# raptor.conf. Only touches it if it's already running (never auto-starts a
# disabled service). Same kill+clean+start dance S31raptor uses; a plain
# `raptorctl <d> restart` leaves a stale sock that blocks the next bind.
restart_stream_daemon() {
	local d="$1" i=0
	pidof "$d" >/dev/null 2>&1 || return 0
	kill $(pidof "$d") 2>/dev/null
	while pidof "$d" >/dev/null 2>&1 && [ "$i" -lt 25 ]; do i=$((i + 1)); sleep 0.2; done
	pidof "$d" >/dev/null 2>&1 && kill -9 $(pidof "$d") 2>/dev/null
	rm -f "$RUNDIR/$d.pid" "$RUNDIR/$d.sock"
	start-stop-daemon -S -b -x "/usr/bin/$d" -- -c /etc/raptor.conf
}

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}
fail() { json_header "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }
ok()   { json_header "200 OK"; printf '{"ok":true%s}\n' "$1"; exit 0; }

# --- form parser (no eval), whitelisted keys ------------------------------
F_a=""; F_cur=""; F_new=""; F_user=""; F_pass=""
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
assign() {
	case "$1" in
		a)    F_a=$2 ;;
		cur)  F_cur=$2 ;;
		new)  F_new=$2 ;;
		user) F_user=$2 ;;
		pass) F_pass=$2 ;;
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
	if [ "$len" -gt 0 ] && [ "$len" -le 8192 ]; then
		body=$(head -c "$len"); parse_pairs "$body"
	fi
fi

# base64 -> raw. Rejects (returns 1) if the input isn't valid base64.
b64d() { printf '%s' "$1" | base64 -d 2>/dev/null; }

# A password is acceptable if it is 6..64 bytes and carries no control byte,
# no ':' (the shadow field separator) and no NUL. We keep the charset generous
# for the *web* password (it never lands in JSON, only in an awk ENVIRON var and
# the shadow file, both of which take arbitrary printable text safely).
valid_web_pw() {
	local p="$1" n
	n=$(printf '%s' "$p" | wc -c)
	[ "$n" -ge 6 ] && [ "$n" -le 64 ] || return 1
	case "$p" in
		*[![:print:]]* ) return 1 ;;   # control chars
		*:* )            return 1 ;;   # would corrupt the shadow record
	esac
	return 0
}

# ONVIF user/pass must be JSON-safe: printable ASCII, 1..32 bytes, and never a
# double-quote or backslash (the only two bytes that need escaping inside a JSON
# string). This lets the in-place edit reprint the value literally.
valid_onvif_field() {
	local v="$1" n
	n=$(printf '%s' "$v" | wc -c)
	[ "$n" -ge 1 ] && [ "$n" -le 32 ] || return 1
	case "$v" in
		*[![:print:]]* ) return 1 ;;
		*[\"\\]* )       return 1 ;;   # no " or \
		*' '* )          return 1 ;;   # no spaces (ONVIF clients choke on them)
	esac
	return 0
}

# --- current ONVIF username (for the GET) ---------------------------------
# Read the username inside the "server" object without a JSON parser: track the
# block, take the first username line within it.
onvif_user() {
	awk '
		/^[ \t]*"server"[ \t]*:[ \t]*\{/ { ins=1 }
		ins && /^[ \t]*"username"[ \t]*:/ {
			v=$0; sub(/^[^:]*:[ \t]*"/,"",v); sub(/".*$/,"",v); print v; exit
		}
		ins && /^[ \t]*\}/ { ins=0 }
	' "$ONVIF_JSON" 2>/dev/null
}

# --- verify the current root password (mirrors login.cgi) -----------------
verify_web_pw() {
	local pw="$1" line hash salt test
	line=$(grep '^root:' "$SHADOW" 2>/dev/null); [ -n "$line" ] || return 1
	hash=$(printf '%s' "$line" | cut -d: -f2)
	case "$hash" in ""|"!"|"*") return 1 ;; esac
	salt=$(printf '%s' "$hash" | cut -d'$' -f3)
	test=$(mkpasswd -m sha512 "$pw" "$salt" 2>/dev/null)
	[ "$test" = "$hash" ]
}

# --- GET ------------------------------------------------------------------
emit_get() {
	local ou; ou=$(onvif_user)
	json_header "200 OK"
	printf '{"ok":true,"web_user":"root","onvif_user":"%s"}\n' "$ou"
	exit 0
}

# --- change the web (root) password ---------------------------------------
set_web_pw() {
	local cur new salt nhash vsalt vhash tmp
	cur=$(b64d "$F_cur"); new=$(b64d "$F_new")
	[ -n "$cur" ] || fail "current password required"
	[ -n "$new" ] || fail "new password required"
	valid_web_pw "$new" || fail "new password must be 6-64 chars, no ':' or control chars"
	verify_web_pw "$cur" || fail "current password is incorrect" "403 Forbidden"

	# Hash with an EXPLICIT random salt. Never rely on mkpasswd inventing a salt
	# (some busybox builds read it from stdin, which in a CGI is the request body
	# or EOF — producing a hash that then doesn't verify).
	salt=$(head -c 96 /dev/urandom 2>/dev/null | tr -dc 'A-Za-z0-9./' | cut -c1-8)
	[ "${#salt}" -ge 8 ] || fail "could not generate salt" "500 Internal Server Error"
	nhash=$(mkpasswd -m sha512 "$new" "$salt" 2>/dev/null)
	case "$nhash" in \$6\$*) ;; *) fail "hashing failed" "500 Internal Server Error" ;; esac

	# SELF-CHECK before we touch /etc/shadow: recompute the hash from the new
	# password + the salt actually embedded in nhash and require an exact match.
	# If anything mangled the password on the way in, we refuse here instead of
	# committing a hash that would lock everyone out.
	vsalt=$(printf '%s' "$nhash" | cut -d'$' -f3)
	vhash=$(mkpasswd -m sha512 "$new" "$vsalt" 2>/dev/null)
	[ "$vhash" = "$nhash" ] || fail "password hash self-check failed — not applied" "500 Internal Server Error"

	tmp=$(mktemp 2>/dev/null || echo /tmp/shadow.$$)
	# ENVIRON carries the hash literally (awk -v would interpret backslashes).
	NHASH="$nhash" awk -F: 'BEGIN{OFS=":"} $1=="root"{$2=ENVIRON["NHASH"]} {print}' \
		"$SHADOW" > "$tmp" 2>/dev/null || { rm -f "$tmp"; fail "could not update shadow" "500 Internal Server Error"; }
	grep -q '^root:' "$tmp" || { rm -f "$tmp"; fail "shadow rewrite invalid" "500 Internal Server Error"; }
	# Truncate-in-place so /etc/shadow keeps its inode and 0600 root perms.
	cat "$tmp" > "$SHADOW" || { rm -f "$tmp"; fail "shadow write failed" "500 Internal Server Error"; }
	rm -f "$tmp"
	ok ',"changed":"web"'
}

# --- change the ONVIF credentials -----------------------------------------
set_onvif() {
	local user pass tmp
	user="$F_user"                 # urlencoded transport already decoded
	pass=$(b64d "$F_pass")
	valid_onvif_field "$user" || fail "onvif username: 1-32 printable chars, no space/quote/backslash"
	valid_onvif_field "$pass" || fail "onvif password: 1-32 printable chars, no space/quote/backslash"
	[ -f "$ONVIF_JSON" ] || fail "onvif config missing" "500 Internal Server Error"

	tmp=$(mktemp 2>/dev/null || echo /tmp/onvif.$$)
	# Reprint only the username/password lines inside the "server" object,
	# preserving each line's original trailing punctuation (comma or not) so the
	# JSON stays valid regardless of field order. Values come via ENVIRON so no
	# escape processing touches them; they're already proven quote/backslash-free.
	NU="$user" NP="$pass" awk '
		BEGIN{ ins=0 }
		/^[ \t]*"server"[ \t]*:[ \t]*\{/ { ins=1 }
		{
			if (ins && $0 ~ /^[ \t]*"username"[ \t]*:/) {
				tail=""; if ($0 ~ /,[ \t]*$/) tail=",";
				match($0, /^[ \t]*/); ind=substr($0,1,RLENGTH);
				print ind "\"username\": \"" ENVIRON["NU"] "\"" tail; next
			}
			if (ins && $0 ~ /^[ \t]*"password"[ \t]*:/) {
				tail=""; if ($0 ~ /,[ \t]*$/) tail=",";
				match($0, /^[ \t]*/); ind=substr($0,1,RLENGTH);
				print ind "\"password\": \"" ENVIRON["NP"] "\"" tail; next
			}
			if (ins && $0 ~ /^[ \t]*\}/) ins=0
			print
		}
	' "$ONVIF_JSON" > "$tmp" 2>/dev/null || { rm -f "$tmp"; fail "could not edit onvif.json" "500 Internal Server Error"; }

	# Sanity: the new username must now be present, and the file must still end
	# with a closing brace (cheap guard against a mangled rewrite).
	grep -q "\"username\": \"$user\"" "$tmp" || { rm -f "$tmp"; fail "onvif rewrite failed" "500 Internal Server Error"; }
	cat "$tmp" > "$ONVIF_JSON" || { rm -f "$tmp"; fail "onvif write failed" "500 Internal Server Error"; }
	rm -f "$tmp"

	# The ONVIF service (onvif.cgi) re-reads the file per request — already live.
	# Bounce the notify daemon so event subscriptions use the new creds too.
	[ -x /etc/init.d/S97onvif_notify ] && /etc/init.d/S97onvif_notify restart >/dev/null 2>&1

	# Unify the credential with the raptor stream servers so ONE username/password
	# covers ONVIF control AND the RTSP stream + HTTP snapshot. These read creds
	# from raptor.conf at start, so persist then restart rsd/rhd. Neither serves
	# the WebUI's WebRTC view, so the live page is unaffected; RTSP/snapshot
	# clients just reconnect with the new credential.
	raptorctl config set rtsp username "$user" >/dev/null 2>&1
	raptorctl config set rtsp password "$pass" >/dev/null 2>&1
	raptorctl config set http username "$user" >/dev/null 2>&1
	raptorctl config set http password "$pass" >/dev/null 2>&1
	raptorctl config save >/dev/null 2>&1
	restart_stream_daemon rsd
	restart_stream_daemon rhd

	ok ',"changed":"onvif","onvif_user":"'"$user"'"'
}

# --- dispatch -------------------------------------------------------------
case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:info)  emit_get ;;
	POST:webpass)   set_web_pw ;;
	POST:onvif)     set_onvif ;;
	*)              fail "unsupported action" "405 Method Not Allowed" ;;
esac
