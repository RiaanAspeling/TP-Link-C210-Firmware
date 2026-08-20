#!/bin/sh
# On-screen-display (video overlay) control for the lean WebUI.
#
#   GET  /x/osd.cgi            -> {config:{...globals}, elements:[{name,visible,
#                                 position,template,font_size,type}...]}
#   POST /x/osd.cgi?a=element  -> change one element (body: name=<el> plus any of
#                                 visible,template,position,align,color,
#                                 stroke_color,stroke_size,font_size)
#   POST /x/osd.cgi?a=global   -> change OSD globals (body: any of enabled,
#                                 font_size,font_color,stroke_color,stroke_size,
#                                 time_format)
#
# The overlays are raptor OSD regions owned by the `rod` daemon; we drive it
# through raptorctl and never hand-edit raptor.conf:
#   raptorctl rod elements                    list (JSON)
#   raptorctl rod config-show                 globals (JSON)
#   raptorctl rod set-element <n> key=val     per-element property
#   raptorctl rod show-element/hide-element   per-element visibility
#   raptorctl rod set-position <n> <pos>      move an element
#   raptorctl rod set-time-format <fmt>       global clock format
#   raptorctl rod set-font-size/-font-color/-stroke-color/-stroke-size  globals
#   raptorctl rod enable | disable            master OSD on/off
#   raptorctl config save                     persist to /etc/raptor.conf
#
# Every value is validated to a strict shape before it reaches raptorctl, and
# each is passed as a single quoted argument (never concatenated into a command
# string), so a crafted value cannot alter the command.

. /var/www/x/auth.sh
require_auth

json_header() {
	printf 'Status: %s\r\n' "${1:-200 OK}"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf '\r\n'
}
fail() { json_header "${2:-400 Bad Request}"; printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }

# --- parser (whitelisted keys, no eval) -----------------------------------
F_a=""; F_name=""; F_visible=""; F_template=""; F_position=""; F_align=""
F_color=""; F_stroke_color=""; F_stroke_size=""; F_font_size=""
F_font_color=""; F_time_format=""; F_enabled=""
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
assign() {
	case "$1" in
		a)            F_a=$2 ;;
		name)         F_name=$2 ;;
		visible)      F_visible=$2 ;;
		template)     F_template=$2 ;;
		position)     F_position=$2 ;;
		align)        F_align=$2 ;;
		color)        F_color=$2 ;;
		stroke_color) F_stroke_color=$2 ;;
		stroke_size)  F_stroke_size=$2 ;;
		font_size)    F_font_size=$2 ;;
		font_color)   F_font_color=$2 ;;
		time_format)  F_time_format=$2 ;;
		enabled)      F_enabled=$2 ;;
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
	if [ "$len" -gt 0 ] && [ "$len" -le 4096 ]; then
		body=$(head -c "$len"); parse_pairs "$body"
	fi
fi

# --- validators -----------------------------------------------------------
is_int() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
# rod element names are simple identifiers; keep the charset tight.
valid_name() { case "$1" in ''|*[!A-Za-z0-9_]*) return 1 ;; *) [ ${#1} -le 31 ] ;; esac; }
valid_pos() {
	case "$1" in
		top_left|top_center|top_right|bottom_left|bottom_center|bottom_right|center) return 0 ;;
		*[!0-9,]*) return 1 ;;                 # otherwise only an "x,y" pair
		*,*) return 0 ;;
		*) return 1 ;;
	esac
}
valid_color() { case "$1" in 0x[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) return 0 ;; *) return 1 ;; esac; }
# A template / time-format is free text but must stay printable and one line.
valid_text() { case "$1" in *[![:print:]]*) return 1 ;; *) [ ${#1} -le 63 ] ;; esac; }

rod() { raptorctl rod "$@" >/dev/null 2>&1; }

# --- GET: current globals + element list ----------------------------------
emit_get() {
	local cfg els
	cfg=$(raptorctl rod config-show 2>/dev/null); case "$cfg" in '{'*) ;; *) cfg='' ;; esac
	els=$(raptorctl rod elements 2>/dev/null);    case "$els" in '{'*) ;; *) els='' ;; esac
	[ -z "$cfg" ] && [ -z "$els" ] && fail "OSD daemon not responding" "503 Service Unavailable"
	json_header "200 OK"
	# Pass the daemon JSON through untouched under stable keys; the client reads
	# config.config and list.elements. (No sh-side JSON surgery to get wrong.)
	printf '{"ok":true,"config":%s,"list":%s}\n' "${cfg:-null}" "${els:-null}"
	exit 0
}

# --- POST a=element: change one overlay -----------------------------------
apply_element() {
	valid_name "$F_name" || fail "bad element name"
	local did=0

	if [ -n "$F_visible" ]; then
		case "$F_visible" in
			1|true)  rod show-element "$F_name"; did=1 ;;
			0|false) rod hide-element "$F_name"; did=1 ;;
			*) fail "bad visible" ;;
		esac
	fi
	if [ -n "$F_template" ]; then
		valid_text "$F_template" || fail "template: printable, <=63 chars"
		rod set-element "$F_name" template="$F_template"; did=1
	fi
	if [ -n "$F_position" ]; then
		valid_pos "$F_position" || fail "bad position"
		# set-element (not set-position) so the element state + config both update.
		rod set-element "$F_name" position="$F_position"; did=1
	fi
	if [ -n "$F_align" ]; then
		case "$F_align" in
			left)   rod set-element "$F_name" align=0 ;;
			center) rod set-element "$F_name" align=1 ;;
			right)  rod set-element "$F_name" align=2 ;;
			0|1|2)  rod set-element "$F_name" align="$F_align" ;;
			*) fail "align: left|center|right" ;;
		esac
		did=1
	fi
	if [ -n "$F_color" ]; then
		valid_color "$F_color" || fail "color: 0xAARRGGBB"
		rod set-element "$F_name" color="$F_color"; did=1
	fi
	if [ -n "$F_stroke_color" ]; then
		valid_color "$F_stroke_color" || fail "stroke_color: 0xAARRGGBB"
		rod set-element "$F_name" stroke_color="$F_stroke_color"; did=1
	fi
	if [ -n "$F_stroke_size" ]; then
		is_int "$F_stroke_size" && [ "$F_stroke_size" -le 5 ] || fail "stroke_size 0..5"
		rod set-element "$F_name" stroke_size="$F_stroke_size"; did=1
	fi
	if [ -n "$F_font_size" ]; then
		is_int "$F_font_size" && [ "$F_font_size" -ge 10 ] && [ "$F_font_size" -le 72 ] || fail "font_size 10..72"
		rod set-element "$F_name" font_size="$F_font_size"; did=1
	fi

	[ "$did" = 1 ] || fail "nothing to change"
	raptorctl config save >/dev/null 2>&1
	# Moving/resizing an OSD region corrupts the live H.264 stream until the next
	# keyframe (long GOP => seconds of artifacts). Force one so every viewer
	# recovers within a frame. Harmless for a colour-only change.
	raptorctl rvd request-idr >/dev/null 2>&1
	emit_get
}

# --- POST a=global: OSD-wide settings -------------------------------------
apply_global() {
	local did=0
	if [ -n "$F_enabled" ]; then
		case "$F_enabled" in
			1|true)  rod enable;  did=1 ;;
			0|false) rod disable; did=1 ;;
			*) fail "bad enabled" ;;
		esac
	fi
	if [ -n "$F_font_size" ]; then
		is_int "$F_font_size" && [ "$F_font_size" -ge 10 ] && [ "$F_font_size" -le 72 ] || fail "font_size 10..72"
		rod set-font-size "$F_font_size"; did=1
	fi
	if [ -n "$F_font_color" ]; then
		valid_color "$F_font_color" || fail "font_color: 0xAARRGGBB"
		rod set-font-color "$F_font_color"; did=1
	fi
	if [ -n "$F_stroke_color" ]; then
		valid_color "$F_stroke_color" || fail "stroke_color: 0xAARRGGBB"
		rod set-stroke-color "$F_stroke_color"; did=1
	fi
	if [ -n "$F_stroke_size" ]; then
		is_int "$F_stroke_size" && [ "$F_stroke_size" -le 5 ] || fail "stroke_size 0..5"
		rod set-stroke-size "$F_stroke_size"; did=1
	fi
	if [ -n "$F_time_format" ]; then
		valid_text "$F_time_format" || fail "time_format: printable, <=63 chars"
		rod set-time-format "$F_time_format"; did=1
	fi

	[ "$did" = 1 ] || fail "nothing to change"
	raptorctl config save >/dev/null 2>&1
	raptorctl rvd request-idr >/dev/null 2>&1
	emit_get
}

# --- dispatch -------------------------------------------------------------
case "${REQUEST_METHOD:-GET}:$F_a" in
	GET:|GET:status) emit_get ;;
	POST:element)    apply_element ;;
	POST:global)     apply_global ;;
	*)               fail "unsupported action" "405 Method Not Allowed" ;;
esac
