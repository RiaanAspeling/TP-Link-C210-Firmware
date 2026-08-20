#!/bin/sh
# SD card + recording control for the lean WebUI.
#
#   GET  /x/sd.cgi              -> status JSON (card + live recording + config)
#   POST /x/sd.cgi?a=config     -> apply [recording] settings (body: k=v&k=v)
#   POST /x/sd.cgi?a=format     -> mkfs.exfat the card (needs confirm=ERASE)
#
# All recording state is owned by raptor's `rmr` daemon, driven through
# `raptorctl` — we never hand-parse or hand-edit raptor.conf here:
#
#   raptorctl rmr status               live JSON {recording,clip,mode,file,...}
#   raptorctl config get recording     the running [recording] section
#   raptorctl config set recording k v  change a key (takes effect live)
#   raptorctl config save              persist running config to /etc/raptor.conf
#                                      (overlay/jffs2 -> survives reboot; it MERGES,
#                                       keeping comments + every other section)
#   raptorctl rmr enable | disable     turn recording on/off now
#
# Only exfat is mountable on this build (/proc/filesystems has exfat, not vfat),
# so "format" is mkfs.exfat only. storage_path is server-fixed to the SD mount so
# the UI can never aim recording at the rootfs.

. /var/www/x/auth.sh
require_auth

SD_DEV=/dev/mmcblk0p1
SD_MNT=/mnt/mmcblk0p1
STORAGE_PATH=$SD_MNT/raptor        # server-fixed; never taken from the client
CONF=/etc/raptor.conf
RUNDIR=/var/run/rss                 # raptor daemon pid/sock dir (see S31raptor)

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}

fail() {
	json_header "${2:-400 Bad Request}"
	printf '{"ok":false,"error":"%s"}\n' "$1"
	exit 0
}

# --- form/query parser (no eval) ------------------------------------------
# Populates F_<key> for a fixed whitelist of keys from QUERY_STRING and, on a
# POST, the urlencoded body. Values are captured raw and validated per-key
# before use; unknown keys are ignored.
F_a=""; F_confirm=""
F_enabled=""; F_mode=""; F_stream=""; F_audio=""
F_clip_length_sec=""; F_prebuffer_sec=""; F_segment_minutes=""; F_max_storage_mb=""
F_sensitivity=""; F_record_post_sec=""

urldecode() {
	# %XX and + -> space, via printf. Safe: we only decode into a variable.
	local s="${1//+/ }"
	printf '%b' "${s//%/\\x}"
}

assign() {
	case "$1" in
		a)                F_a=$2 ;;
		confirm)          F_confirm=$2 ;;
		enabled)          F_enabled=$2 ;;
		mode)             F_mode=$2 ;;
		stream)           F_stream=$2 ;;
		audio)            F_audio=$2 ;;
		clip_length_sec)  F_clip_length_sec=$2 ;;
		prebuffer_sec)    F_prebuffer_sec=$2 ;;
		segment_minutes)  F_segment_minutes=$2 ;;
		max_storage_mb)   F_max_storage_mb=$2 ;;
		sensitivity)      F_sensitivity=$2 ;;
		record_post_sec)  F_record_post_sec=$2 ;;
	esac
}

parse_pairs() {
	local OLD_IFS=$IFS kv k v
	IFS='&'
	for kv in $1; do
		[ -n "$kv" ] || continue
		k=${kv%%=*}
		v=${kv#*=}
		[ "$k" = "$kv" ] && v=""      # bare key, no '='
		assign "$k" "$(urldecode "$v")"
	done
	IFS=$OLD_IFS
}

parse_pairs "${QUERY_STRING:-}"
if [ "${REQUEST_METHOD:-GET}" = "POST" ]; then
	len=${CONTENT_LENGTH:-0}
	case "$len" in ''|*[!0-9]*) len=0 ;; esac
	if [ "$len" -gt 0 ] && [ "$len" -le 4096 ]; then
		body=$(head -c "$len")
		parse_pairs "$body"
	fi
fi

# --- card probe -----------------------------------------------------------
sd_present() { [ -b "$SD_DEV" ]; }
sd_fstype()  { awk -v m="$SD_MNT" '$2==m{print $3; exit}' /proc/mounts; }
sd_mounted() { [ -n "$(sd_fstype)" ]; }

# Read an uncommented numeric key from the [motion] section of the config file —
# the persisted truth, readable whatever state the daemons are in. Empty if the
# key is absent/commented (i.e. still at its code default).
motion_file_key() {
	awk -v k="$1" '
		/^\[/ { insec = ($0=="[motion]") }
		insec && $1==k && $2=="=" { print $3; exit }
	' "$CONF" 2>/dev/null
}

# --- status ---------------------------------------------------------------
emit_status() {
	local present=false mounted=false fstype="" size=0 used=0 avail=0
	sd_present && present=true
	local ft; ft=$(sd_fstype)
	if [ -n "$ft" ]; then
		mounted=true; fstype=$ft
		# df -k: pull the data row's total/used/avail (KB).
		set -- $(df -k "$SD_MNT" 2>/dev/null | awk 'NR==2{print $2,$3,$4}')
		size=${1:-0}; used=${2:-0}; avail=${3:-0}
	fi

	# Live recording state (raw rmr JSON, or null when the daemon is down / no card).
	local rec; rec=$(raptorctl rmr status 2>/dev/null)
	case "$rec" in '{'*) ;; *) rec=null ;; esac

	# Running [recording] config -> JSON object. `config get recording` prints
	# "key = value" lines under a [recording] header; turn them into JSON,
	# quoting non-numeric/non-bool values.
	local cfg; cfg=$(raptorctl config get recording 2>/dev/null | awk '
		/^\[/ { next }
		/=/ {
			k=$1; sub(/^[ \t]+/,"",k)
			v=$0; sub(/^[^=]*=[ \t]*/,"",v); sub(/[ \t]+$/,"",v)
			if (n++) printf ",";
			if (v ~ /^-?[0-9]+$/ || v=="true" || v=="false")
				printf "\"%s\":%s", k, v
			else {
				gsub(/\\/,"\\\\",v); gsub(/"/,"\\\"",v)
				printf "\"%s\":\"%s\"", k, v
			}
		}
	')
	[ -n "$cfg" ] || cfg=""

	# Motion detector knobs. sensitivity comes from `rmd status` (the LIVE value):
	# `config get motion sensitivity` reports a constant default (3) no matter what
	# was set, which made the dropdown snap back to High after every apply. If rmd
	# is momentarily unavailable, fall back to the persisted file value (never a
	# hard-coded default), so the UI still shows what the user chose.
	local sens post
	sens=$(raptorctl rmd status 2>/dev/null | sed -n 's/.*"sensitivity":\([0-9]\).*/\1/p')
	[ -n "$sens" ] || sens=$(motion_file_key sensitivity)
	[ -n "$sens" ] || sens=3
	post=$(motion_file_key record_post_sec)
	[ -n "$post" ] || post=30

	json_header "200 OK"
	printf '{"ok":true,"card":{"present":%s,"mounted":%s,"dev":"%s","mount":"%s","fstype":"%s","size_kb":%s,"used_kb":%s,"avail_kb":%s},"rec":%s,"cfg":{%s},"motion":{"sensitivity":%s,"record_post_sec":%s}}\n' \
		"$present" "$mounted" "$SD_DEV" "$SD_MNT" "$fstype" "$size" "$used" "$avail" "$rec" "$cfg" "$sens" "$post"
	exit 0
}

# --- recstate: cheap poll of just the live recording indicator ------------
# The main status runs df + several raptorctl calls; this is the lean version the
# UI hits every couple of seconds to drive the "REC" badge. `on` = recording is
# enabled and the daemon is up; `active` = a clip/segment is being written right
# now (motion clip in progress, or continuous always-on).
emit_recstate() {
	local on=false active=false card=false rec
	sd_mounted && card=true
	if pidof rmr >/dev/null 2>&1; then
		on=true
		rec=$(raptorctl rmr status 2>/dev/null)
		case "$rec" in
			*'"recording":true'*|*'"clip":true'*) active=true ;;
		esac
	fi
	json_header "200 OK"
	printf '{"ok":true,"on":%s,"active":%s,"card":%s}\n' "$on" "$active" "$card"
	exit 0
}

# --- apply config ---------------------------------------------------------
# Each setter validates its value against a strict pattern before it reaches
# raptorctl, so a value can never carry shell metacharacters into the command.
set_key() { raptorctl config set recording "$1" "$2" >/dev/null 2>&1; }
set_key_section() { raptorctl config set "$1" "$2" "$3" >/dev/null 2>&1; }
is_int()  { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

# Daemon lifecycle, the reliable way. `raptorctl <d> start/stop/restart` leaves a
# stale $RUNDIR/<d>.sock that blocks the next bind, so a control-socket read then
# times out (this cost hours of head-scratching). The init script instead kills
# the pid, removes the stale pid/sock, and relaunches with start-stop-daemon — so
# we do the same. rmr binds its control socket in ~0s; rmd is slow (~15-20s), so
# callers must NOT block waiting on rmd. Restarting rmr/rmd never touches rvd/rwd,
# so the live stream survives.
daemon_kill() {
	local p; p=$(pidof "$1" 2>/dev/null)
	if [ -n "$p" ]; then
		kill "$p" 2>/dev/null
		local i=0
		while kill -0 "$p" 2>/dev/null && [ "$i" -lt 30 ]; do i=$((i+1)); sleep 0.1; done
		kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
	fi
	rm -f "$RUNDIR/$1.pid" "$RUNDIR/$1.sock"
}
daemon_start() { start-stop-daemon -S -b -x "/usr/bin/$1" -- -c "$CONF"; }

# Stop recording cleanly and release the SD mount: `disable` closes any open
# segment/clip first, then kill + clean sock. No-op if rmr isn't running.
rmr_stop_clean() {
	pidof rmr >/dev/null 2>&1 && raptorctl rmr disable >/dev/null 2>&1
	daemon_kill rmr
	sync
}

apply_config() {
	sd_mounted || fail "no SD card mounted" "409 Conflict"
	local changed=0

	if [ -n "$F_mode" ]; then
		case "$F_mode" in continuous|motion|both) set_key mode "$F_mode"; changed=1 ;;
			*) fail "bad mode" ;; esac
	fi
	if [ -n "$F_stream" ]; then
		case "$F_stream" in 0|1) set_key stream "$F_stream"; changed=1 ;;
			*) fail "bad stream" ;; esac
	fi
	if [ -n "$F_audio" ]; then
		case "$F_audio" in 1|true)  set_key audio true;  changed=1 ;;
			0|false) set_key audio false; changed=1 ;;
			*) fail "bad audio" ;; esac
	fi
	if [ -n "$F_clip_length_sec" ]; then
		is_int "$F_clip_length_sec" && [ "$F_clip_length_sec" -ge 5 ] && [ "$F_clip_length_sec" -le 600 ] \
			|| fail "clip_length_sec 5..600"
		set_key clip_length_sec "$F_clip_length_sec"; changed=1
	fi
	if [ -n "$F_prebuffer_sec" ]; then
		is_int "$F_prebuffer_sec" && [ "$F_prebuffer_sec" -ge 0 ] && [ "$F_prebuffer_sec" -le 5 ] \
			|| fail "prebuffer_sec 0..5"
		set_key prebuffer_sec "$F_prebuffer_sec"; changed=1
	fi
	if [ -n "$F_segment_minutes" ]; then
		is_int "$F_segment_minutes" && [ "$F_segment_minutes" -ge 1 ] && [ "$F_segment_minutes" -le 60 ] \
			|| fail "segment_minutes 1..60"
		set_key segment_minutes "$F_segment_minutes"; changed=1
	fi
	if [ -n "$F_max_storage_mb" ]; then
		is_int "$F_max_storage_mb" && [ "$F_max_storage_mb" -le 1000000 ] \
			|| fail "max_storage_mb 0..1000000"
		set_key max_storage_mb "$F_max_storage_mb"; changed=1
	fi

	# storage_path is server-owned, never client-set. Pin it every apply so a
	# stale value can't leave recording pointed at the rootfs.
	set_key storage_path "$STORAGE_PATH"; changed=1

	# --- motion detector knobs (the [motion] section) ------------------------
	# sensitivity applies LIVE via `rmd sensitivity` and is persisted to the file.
	# record_post_sec is persisted too and takes effect when rmd next starts. We
	# deliberately do NOT restart rmd here: it is slow to rebind its control
	# socket (~20-30s) and a spurious reload was knocking motion detection offline
	# and making the status read fall back to defaults right after an apply.
	if [ -n "$F_sensitivity" ]; then
		case "$F_sensitivity" in 0|1|2|3|4) ;; *) fail "sensitivity 0..4" ;; esac
		set_key_section motion sensitivity "$F_sensitivity"
		raptorctl rmd sensitivity "$F_sensitivity" >/dev/null 2>&1     # live
	fi
	if [ -n "$F_record_post_sec" ]; then
		is_int "$F_record_post_sec" && [ "$F_record_post_sec" -ge 0 ] && [ "$F_record_post_sec" -le 300 ] \
			|| fail "record_post_sec 0..300"
		set_key_section motion record_post_sec "$F_record_post_sec"
	fi

	# Desired on/off. If the client doesn't send `enabled`, keep whatever the
	# daemon is currently doing (running == on) so a knob-only change re-applies
	# without silently toggling recording.
	local want
	case "$F_enabled" in
		1|true)  set_key enabled true;  want=on ;;
		0|false) set_key enabled false; want=off ;;
		'')      pidof rmr >/dev/null 2>&1 && want=on || want=off ;;
		*)       fail "bad enabled" ;;
	esac

	# Persist to /etc/raptor.conf BEFORE (re)starting: a fresh start adopts config
	# from the saved file. `config set` alone never reaches a running daemon.
	raptorctl config save >/dev/null 2>&1

	# rmr owns the [recording] knobs; a clean kill+start (never `restart`, which
	# comes up un-armed) re-reads them all. Stream-safe: rvd/rwd are untouched.
	rmr_stop_clean
	if [ "$want" = on ]; then daemon_start rmr; fi

	sleep 1
	emit_status
}

# --- format ---------------------------------------------------------------
# Destroys everything on the card. Guarded by an explicit confirm token so a
# stray request can never wipe it. Stops recording, unmounts, mkfs.exfat,
# remounts, recreates storage_path.
do_format() {
	[ "$F_confirm" = "ERASE" ] || fail "format needs confirm=ERASE" "428 Precondition Required"
	sd_present || fail "no SD card present" "409 Conflict"

	# Stop recording and fully release the daemon so nothing holds the mount.
	set_key enabled false
	raptorctl config save >/dev/null 2>&1
	rmr_stop_clean

	# Unmount (best effort, then force) if mounted.
	if sd_mounted; then
		umount "$SD_MNT" 2>/dev/null || umount -l "$SD_MNT" 2>/dev/null
	fi
	if sd_mounted; then
		fail "could not unmount (device busy)" "409 Conflict"
	fi

	local out
	out=$(mkfs.exfat -n THINGINO "$SD_DEV" 2>&1) || {
		# Try to remount whatever is there so the card isn't left offline.
		mount "$SD_DEV" "$SD_MNT" 2>/dev/null
		fail "mkfs failed: $(printf '%s' "$out" | tr '\n"' '  ' | tail -c 160)" "500 Internal Server Error"
	}

	mount -t exfat "$SD_DEV" "$SD_MNT" 2>/dev/null || fail "remount failed" "500 Internal Server Error"
	mkdir -p "$STORAGE_PATH" 2>/dev/null

	emit_status
}

# --- dispatch -------------------------------------------------------------
case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:status)  emit_status ;;
	GET:recstate)     emit_recstate ;;
	POST:config)      apply_config ;;
	POST:format)      do_format ;;
	*)                fail "unsupported action" "405 Method Not Allowed" ;;
esac
