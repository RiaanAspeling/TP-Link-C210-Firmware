#!/bin/sh
# Recording browser for the lean WebUI: list / play / download / delete the
# clips rmr writes to the SD card.
#
#   GET  /x/clips.cgi?a=dates            -> {dates:[{date,motion,segment,bytes}...]}
#   GET  /x/clips.cgi?a=list&date=D      -> {date,files:[{f,time,type,bytes}...]}
#   GET  /x/clips.cgi?a=get&f=REL        -> streams the .mp4 (Range-aware, 206)
#   POST /x/clips.cgi?a=del&f=REL        -> deletes one .mp4
#   POST /x/clips.cgi?a=delbatch         -> body "files=REL1,REL2,..." deletes many
#
# rmr lays clips out in two trees under storage_path:
#   clips/YYYY-MM-DD/HH-MM-SS.mp4   motion clips
#        YYYY-MM-DD/HH-MM-SS.mp4   continuous segments
# `f` is always a path RELATIVE to storage_path. It is validated to a strict
# charset, rejected if it contains "..", and the resolved path is confirmed to
# sit inside storage_path and be a regular .mp4 before anything touches it — so
# a crafted `f` can never read or delete outside the recording tree.
#
# Files are served through this CGI (never as static docroot files) so the
# session check in require_auth actually gates them; uhttpd serves docroot
# statics with no auth at all.

. /var/www/x/auth.sh
require_auth

SD_MNT=/mnt/mmcblk0p1
STORAGE_PATH=$SD_MNT/raptor

# --- query parse (no eval) ------------------------------------------------
q_a=""; q_date=""; q_f=""; q_d=""; q_name=""; q_to=""
OLD_IFS=$IFS; IFS='&'
for kv in ${QUERY_STRING:-}; do
	case "$kv" in
		a=*)    q_a=${kv#a=} ;;
		date=*) q_date=${kv#date=} ;;
		f=*)    q_f=${kv#f=} ;;
		d=*)    q_d=${kv#d=} ;;
		name=*) q_name=${kv#name=} ;;
		to=*)   q_to=${kv#to=} ;;
	esac
done
IFS=$OLD_IFS

urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
q_date=$(urldecode "$q_date")
q_f=$(urldecode "$q_f")
q_d=$(urldecode "$q_d")
q_name=$(urldecode "$q_name")
q_to=$(urldecode "$q_to")

json_hdr() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n\r\n'
}
fail() { json_hdr "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }

# A date must look exactly like YYYY-MM-DD.
valid_date() {
	case "$1" in
		[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]) return 0 ;;
		*) return 1 ;;
	esac
}

# Validate a client `f` and echo the absolute path on success. Rejects empty,
# any ".." component, anything outside the [0-9A-Za-z/._-] charset, and any
# resolved path that escapes STORAGE_PATH.
resolve_f() {
	local rel="$1" abs real
	[ -n "$rel" ] || return 1
	case "$rel" in
		/*|*..*) return 1 ;;                      # no absolute paths, no traversal
		*[!0-9A-Za-z/._-]*) return 1 ;;           # strict charset
		*.mp4) ;; *) return 1 ;;                  # clips only
	esac
	abs="$STORAGE_PATH/$rel"
	[ -f "$abs" ] || return 1
	# Belt-and-suspenders: confirm the real path is still under storage_path.
	real=$(readlink -f "$abs" 2>/dev/null) || return 1
	case "$real/" in "$STORAGE_PATH"/*) ;; *) return 1 ;; esac
	printf '%s' "$abs"
}

# Validate a client dir path (relative to storage_path); "" or "." = the root.
# Same confinement as resolve_f, but for directories. Echoes the absolute path.
resolve_dir() {
	local rel="$1" abs real
	case "$rel" in
		''|'.') printf '%s' "$STORAGE_PATH"; return 0 ;;
		/*|*..*) return 1 ;;
		*[!0-9A-Za-z/._-]*) return 1 ;;
	esac
	abs="$STORAGE_PATH/$rel"
	[ -d "$abs" ] || return 1
	real=$(readlink -f "$abs" 2>/dev/null) || return 1
	case "$real/" in "$STORAGE_PATH"/*) ;; *) return 1 ;; esac
	printf '%s' "$abs"
}

# Validate a client path that may be a file OR a directory (for rename/move/
# delete of either). Echoes the absolute path.
resolve_any() {
	local rel="$1" abs real
	[ -n "$rel" ] || return 1
	case "$rel" in
		/*|*..*) return 1 ;;
		*[!0-9A-Za-z/._-]*) return 1 ;;
	esac
	abs="$STORAGE_PATH/$rel"
	[ -e "$abs" ] || return 1
	real=$(readlink -f "$abs" 2>/dev/null) || return 1
	case "$real/" in "$STORAGE_PATH"/*) ;; *) return 1 ;; esac
	printf '%s' "$abs"
}

# A single path component (folder or file base name): no slash, no traversal.
valid_name() {
	case "$1" in
		''|.|..) return 1 ;;
		*/*) return 1 ;;
		*[!0-9A-Za-z._-]*) return 1 ;;
		*) [ "${#1}" -le 64 ] ;;
	esac
}

require_post() { [ "${REQUEST_METHOD:-GET}" = POST ] || fail "POST required" "405 Method Not Allowed"; }

# --- dates: one row per calendar day, with per-tree counts ----------------
list_dates() {
	json_hdr
	printf '{"ok":true,"storage":"%s","dates":[' "$STORAGE_PATH"
	local first=1 d name mc sc by
	# Union of day-dirs from both trees. clips/ is the motion tree; the top
	# level holds segment day-dirs (skip the literal "clips" entry).
	for d in $(
		{ ls -1 "$STORAGE_PATH" 2>/dev/null; ls -1 "$STORAGE_PATH/clips" 2>/dev/null; } \
			| grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -u
	); do
		mc=$(ls -1 "$STORAGE_PATH/clips/$d"/*.mp4 2>/dev/null | wc -l)
		sc=$(ls -1 "$STORAGE_PATH/$d"/*.mp4 2>/dev/null | wc -l)
		[ "$mc" -eq 0 ] && [ "$sc" -eq 0 ] && continue
		by=$(du -sk "$STORAGE_PATH/clips/$d" "$STORAGE_PATH/$d" 2>/dev/null | awk '{s+=$1} END{print s*1024}')
		[ -n "$by" ] || by=0
		[ "$first" = 1 ] || printf ','
		first=0
		printf '{"date":"%s","motion":%s,"segment":%s,"bytes":%s}' "$d" "$mc" "$sc" "$by"
	done
	printf ']}\n'
}

# --- list: files for one day, both trees, newest first --------------------
list_files() {
	valid_date "$q_date" || fail "bad date"
	json_hdr
	printf '{"ok":true,"date":"%s","files":[' "$q_date"
	local first=1 typ dir base t sz
	for typ in motion segment; do
		[ "$typ" = motion ] && dir="$STORAGE_PATH/clips/$q_date" || dir="$STORAGE_PATH/$q_date"
		[ -d "$dir" ] || continue
		for path in $(ls -1 "$dir"/*.mp4 2>/dev/null | sort -r); do
			base=${path##*/}
			# HH-MM-SS.mp4 -> HH:MM:SS
			t=${base%.mp4}; t=$(printf '%s' "$t" | tr '-' ':')
			sz=$(stat -c%s "$path" 2>/dev/null || echo 0)
			[ "$typ" = motion ] && rel="clips/$q_date/$base" || rel="$q_date/$base"
			[ "$first" = 1 ] || printf ','
			first=0
			printf '{"f":"%s","time":"%s","type":"%s","bytes":%s}' "$rel" "$t" "$typ" "$sz"
		done
	done
	printf ']}\n'
}

# --- get: stream one clip, Range-aware ------------------------------------
serve_file() {
	local abs; abs=$(resolve_f "$q_f") || fail "not found" "404 Not Found"
	local size; size=$(stat -c%s "$abs" 2>/dev/null || echo 0)

	# Parse a single "bytes=START-END" range if present.
	local start="" end="" rng="${HTTP_RANGE:-}"
	case "$rng" in
		bytes=*)
			rng=${rng#bytes=}
			start=${rng%-*}; end=${rng#*-}
			;;
	esac

	if [ -n "$start" ] || [ -n "$end" ]; then
		[ -n "$start" ] || start=0
		case "$start" in *[!0-9]*) start=0 ;; esac
		if [ -z "$end" ] || [ "$end" -ge "$size" ] 2>/dev/null; then end=$((size-1)); fi
		case "$end" in *[!0-9]*) end=$((size-1)) ;; esac
		if [ "$start" -gt "$end" ] 2>/dev/null; then
			printf 'Status: 416 Range Not Satisfiable\r\n'
			printf 'Content-Range: bytes */%s\r\n\r\n' "$size"
			exit 0
		fi
		local len=$((end-start+1))
		printf 'Status: 206 Partial Content\r\n'
		printf 'Content-Type: video/mp4\r\n'
		printf 'Accept-Ranges: bytes\r\n'
		printf 'Content-Range: bytes %s-%s/%s\r\n' "$start" "$end" "$size"
		printf 'Content-Length: %s\r\n' "$len"
		printf 'Cache-Control: private, max-age=30\r\n\r\n'
		[ "${REQUEST_METHOD:-GET}" = HEAD ] && exit 0
		# Byte-precise slice without loading the file into memory.
		tail -c +$((start+1)) "$abs" | head -c "$len"
		exit 0
	fi

	printf 'Status: 200 OK\r\n'
	printf 'Content-Type: video/mp4\r\n'
	printf 'Accept-Ranges: bytes\r\n'
	printf 'Content-Length: %s\r\n' "$size"
	printf 'Cache-Control: private, max-age=30\r\n\r\n'
	[ "${REQUEST_METHOD:-GET}" = HEAD ] && exit 0
	cat "$abs"
}

# --- del: remove one clip -------------------------------------------------
del_file() {
	[ "${REQUEST_METHOD:-GET}" = POST ] || fail "POST required" "405 Method Not Allowed"
	local abs; abs=$(resolve_f "$q_f") || fail "not found" "404 Not Found"
	rm -f "$abs" || fail "delete failed" "500 Internal Server Error"
	json_hdr; printf '{"ok":true,"deleted":"%s"}\n' "$q_f"
}

# --- delbatch: remove many clips in one request ---------------------------
# Body is "files=REL1,REL2,...". Each REL is url-encoded (so it carries no
# literal comma) and validated through resolve_f exactly like a single del,
# so a crafted entry can't escape the recording tree. One bad entry is
# counted and skipped, never fatal.
del_batch() {
	[ "${REQUEST_METHOD:-GET}" = POST ] || fail "POST required" "405 Method Not Allowed"
	local len="${CONTENT_LENGTH:-0}" body="" list ok=0 bad=0 tok rel abs
	case "$len" in ''|*[!0-9]*) len=0 ;; esac
	[ "$len" -gt 0 ] && [ "$len" -le 65536 ] && body=$(head -c "$len")
	list="${body#files=}"
	[ "$list" = "$body" ] && fail "missing files"
	local OIFS=$IFS; IFS=','
	for tok in $list; do
		IFS=$OIFS
		rel=$(urldecode "$tok")
		if abs=$(resolve_f "$rel") && rm -f "$abs"; then
			ok=$((ok + 1))
		else
			bad=$((bad + 1))
		fi
		IFS=','
	done
	IFS=$OIFS
	json_hdr; printf '{"ok":true,"deleted":%s,"failed":%s}\n' "$ok" "$bad"
}

# --- browse: one directory (sub-folders + clips) for the file manager ------
list_browse() {
	local rel="$q_d" dir; dir=$(resolve_dir "$rel") || fail "not found" "404 Not Found"
	json_hdr
	local up=""; case "$rel" in */*) up="${rel%/*}" ;; *) up="" ;; esac
	printf '{"ok":true,"path":"%s","up":"%s","dirs":[' "$rel" "$up"
	local first=1 name cnt by pre=""
	[ -n "$rel" ] && pre="$rel/"
	for name in $(ls -1 "$dir" 2>/dev/null | sort); do
		[ -d "$dir/$name" ] || continue
		case "$name" in *[!0-9A-Za-z._-]*) continue ;; esac
		cnt=$(ls -1 "$dir/$name" 2>/dev/null | wc -l)
		by=$(du -sk "$dir/$name" 2>/dev/null | awk '{print $1*1024}'); [ -n "$by" ] || by=0
		[ "$first" = 1 ] || printf ','; first=0
		printf '{"name":"%s","rel":"%s","count":%s,"bytes":%s}' "$name" "$pre$name" "$cnt" "$by"
	done
	printf '],"files":['
	first=1
	local base sz
	for path in $(ls -1 "$dir"/*.mp4 2>/dev/null | sort -r); do
		[ -f "$path" ] || continue
		base=${path##*/}
		sz=$(stat -c%s "$path" 2>/dev/null || echo 0)
		[ "$first" = 1 ] || printf ','; first=0
		printf '{"name":"%s","rel":"%s","bytes":%s}' "$base" "$pre$base" "$sz"
	done
	printf ']}\n'
}

# --- mkdir -----------------------------------------------------------------
do_mkdir() {
	require_post
	local parent; parent=$(resolve_dir "$q_d") || fail "bad folder"
	valid_name "$q_name" || fail "bad name"
	[ -e "$parent/$q_name" ] && fail "already exists" "409 Conflict"
	mkdir "$parent/$q_name" 2>/dev/null || fail "mkdir failed" "500 Internal Server Error"
	json_hdr; printf '{"ok":true}\n'
}

# --- rename (within the same parent) ---------------------------------------
do_rename() {
	require_post
	local abs; abs=$(resolve_any "$q_f") || fail "not found" "404 Not Found"
	valid_name "$q_name" || fail "bad name"
	# A clip must keep its .mp4 extension so it stays servable.
	if [ -f "$abs" ]; then case "$q_name" in *.mp4) ;; *) fail "clip name must end in .mp4" ;; esac; fi
	local parent="${abs%/*}"
	[ -e "$parent/$q_name" ] && fail "target exists" "409 Conflict"
	mv "$abs" "$parent/$q_name" 2>/dev/null || fail "rename failed" "500 Internal Server Error"
	json_hdr; printf '{"ok":true}\n'
}

# --- move items into a folder (body "files=REL1,REL2,..."; ?to=<destdir>) ---
do_move() {
	require_post
	local dest; dest=$(resolve_dir "$q_to") || fail "bad destination"
	local len="${CONTENT_LENGTH:-0}" body="" list tok rel abs ok=0 bad=0
	case "$len" in ''|*[!0-9]*) len=0 ;; esac
	[ "$len" -gt 0 ] && [ "$len" -le 65536 ] && body=$(head -c "$len")
	list="${body#files=}"; [ "$list" = "$body" ] && fail "missing files"
	local OIFS=$IFS; IFS=','
	for tok in $list; do
		IFS=$OIFS
		rel=$(urldecode "$tok")
		if abs=$(resolve_any "$rel") && [ "${abs%/*}" != "$dest" ] && [ ! -e "$dest/${abs##*/}" ] \
			&& mv "$abs" "$dest/" 2>/dev/null; then ok=$((ok + 1)); else bad=$((bad + 1)); fi
		IFS=','
	done
	IFS=$OIFS
	json_hdr; printf '{"ok":true,"moved":%s,"failed":%s}\n' "$ok" "$bad"
}

# --- delete a folder and everything under it -------------------------------
do_deltree() {
	require_post
	local dir; dir=$(resolve_dir "$q_d") || fail "not found" "404 Not Found"
	[ "$dir" = "$STORAGE_PATH" ] && fail "refusing to delete the root" "409 Conflict"
	rm -rf "$dir" || fail "delete failed" "500 Internal Server Error"
	json_hdr; printf '{"ok":true}\n'
}

case "$q_a" in
	dates) list_dates ;;
	list)  list_files ;;
	browse) list_browse ;;
	get)   serve_file ;;
	del)   del_file ;;
	delbatch) del_batch ;;
	mkdir) do_mkdir ;;
	rename) do_rename ;;
	move)  do_move ;;
	deltree) do_deltree ;;
	*)     fail "unsupported action" ;;
esac
