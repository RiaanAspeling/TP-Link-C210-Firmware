# TP-Link Tapo C210 — custom thingino firmware

Custom [thingino](https://github.com/themactep/thingino-firmware)-based firmware for the
**TP-Link Tapo C210** pan/tilt camera — no cloud, no vendor app, works with standard
ONVIF/RTSP software.

**Hardware:** Ingenic T23N SoC · SmartSens SC3336 sensor · RTL8188FTV Wi-Fi · TMI8150 pan/tilt + IR-cut

> **Thin overlay repo.** This contains *only* the customisations. The upstream thingino
> build tree is a pinned dependency (see [`UPSTREAM_PIN`](UPSTREAM_PIN)) that the build
> script clones into `build/` (gitignored) — it is not vendored here.

## What works

| Feature | Status |
|---|---|
| Video — 1080p H.264, RTSP `:554` (`/ch0` main, `/ch1` sub) | ✅ |
| Audio — AAC 16 kHz mono, in both RTSP streams | ✅ |
| Snapshots — JPEG over HTTPS `:8443/snap.jpg` | ✅ |
| ONVIF — WS-Discovery, Profile S, works with iSpy / AgentDVR / IP Cam Viewer | ✅ |
| WebRTC preview in the stock web UI | ✅ |
| Pan/tilt — web UI, ONVIF continuous (press-and-hold), absolute moves, presets | ✅ |
| Motion detection — custom frame-difference detector (vendor IVS is broken) | ✅ |
| Lean web UI — login, WebRTC live view (1080p/360p), smooth PTZ | ✅ |
| IR-cut + automatic day/night (gain-based) | ✅ |
| Wi-Fi setup, web UI, SSH | ✅ |

## Customisations in this repo

- **Pan/tilt driver** — TMI8150 over SPI: `motor.ko` from `spi-tmi8152`, plus GPIO 38 (PB6)
  as H-bridge power enable, which the stock build leaves off.
- **IR-cut / day-night** — the packaged `ric` daemon can't drive this camera's SPI IR-cut,
  so it's replaced by [`overlay/usr/sbin/daynight`](overlay/usr/sbin/daynight): a small
  gain-based state machine reading the ISP's analog gain, switching the filter via
  `/dev/tmi8152_ir_cut` and the IR LED on GPIO 49.
- **IPv6 disabled** — the WebRTC daemon advertised an unreachable IPv6 ICE candidate,
  which made the browser preview black. Disabled in `sysctl.conf`.
- **Lean build** — dropped telegram, home-assistant, MQTT, WireGuard, cloud agent, motion
  detection, SRT and webtorrent. Frees ~272 KB of flash and several daemons' worth of RAM.
- **ISP reserved memory 26 MB → 20 MB** — gives Linux **+6 MB RAM** (33.5 → 39.6 MB), which
  matters a lot on this device. Verified stable with sustained 1080p + audio streaming.
- **Pan/tilt position-tracking fixes** — three bugs in the stock TMI8152 driver and
  motors daemon made the open-loop position tally diverge from reality until an axis
  refused to move. See [Pan/tilt driver fixes](#pantilt-driver-fixes).
- **ONVIF PTZ patch + `ptz-glide` daemon** — turns the stateless ONVIF CGI into a
  smooth press-and-hold glide; the daemon is the sole motor-command writer.
- **Motion detection** — this SoC's vendor IVS algorithms are broken, so raptor carries
  a patch adding our own frame-difference detector. See [Motion detection](#motion-detection).

## Layout

| Path | What |
|------|------|
| `user/<board>/` | Board layer: `local.fragment` (defconfig), `thingino.json` (runtime config) |
| `overlay/` | Files copied into the rootfs (gpio, day/night, init scripts, raptor.conf) |
| `tree-overrides/` | Files overwritten in the upstream tree (package tweaks, patches) |
| `scripts/build.sh` | Clone/pin upstream → apply layer → Docker build → image into `images/` |
| `scripts/apply-layer.sh` | Apply this layer to an upstream tree (idempotent) |
| `webui/` | Lean web UI pages (`login.html`, `index.html`) — copied to `/var/www` |
| `images/`, `build/`, `firmware-dumps/` | Local only — gitignored |

## Build

Requires `docker` and `git`; the toolchain image is pulled automatically.

```bash
./scripts/build.sh              # → images/thingino_tapo_c210_custom_<date>.bin
CLEAN=1 ./scripts/build.sh      # required after disabling packages (see below)
DIRCLEAN="spi-tmi8152 thingino-motors" ./scripts/build.sh   # after editing their patches
```

First run clones upstream into `build/thingino/` (~11 GB with the download cache); later
runs reuse it. Overrides: `THINGINO_DIR=<path>`, `PIN=<sha>`, `NO_PIN=1`.

> **Use `CLEAN=1` whenever you disable a package or change `BR2_THINGINO_RMEM_MB`.**
> Buildroot doesn't prune `target/` when a package is removed, so an incremental build
> silently keeps stale binaries — and the U-Boot env generator only *appends* `osmem`/`rmem`
> if absent, so an incremental build keeps the old memory split.
>
> **Use `DIRCLEAN="<pkg>"` after adding or editing a patch** under
> `tree-overrides/package/all-patches/<pkg>/`. Buildroot applies patches only at extract
> time, so an already-extracted package silently ignores a new patch — the build succeeds
> and the fix simply isn't in it.

## Flashing

**Partial (keeps Wi-Fi + password):** copy `rootfs.squashfs` to the camera and flash mtd4.
Dropbear has no SFTP, so stream it over SSH rather than using `scp`:

```bash
cat build/thingino/output/*/[board]*/images/rootfs.squashfs | ssh root@<cam> 'cat > /tmp/rootfs.bin'
ssh root@<cam> 'md5sum /tmp/rootfs.bin'          # verify against the local file
ssh root@<cam> '/etc/init.d/S31raptor stop'      # free RAM first
ssh root@<cam> 'flashcp -v /tmp/rootfs.bin /dev/mtd4 && rm -f /tmp/rootfs.bin && reboot'
```

RAM is tight (~33 MB before the rmem change): stop the streamer and clear `/tmp` before
flashing, or the OOM killer may take out SSH mid-write.

**Full / recovery:** the 8 MB image via a CH341A programmer, or `sysupgrade` (wipes the
overlay, so Wi-Fi and password need setting up again).

## Credentials

Two independent authentication systems — deliberately, so streaming clients don't get
admin access:

| Interface | User | Notes |
|---|---|---|
| Web UI, SSH | `root` | System account; password set during Wi-Fi setup |
| ONVIF, RTSP, snapshots | `thingino` | thingino default — **change it**, in `/etc/raptor.conf` + `/etc/onvif.json` |

## Pan/tilt driver fixes

Out of the box this camera's tilt axis would stick at a travel limit and refuse to
reverse, while pan never did. It looked like generic open-loop drift; it was actually
three specific bugs, patched in
`tree-overrides/package/all-patches/{spi-tmi8152,thingino-motors}/`.

**1. The position tally counted backwards on an inverted axis.** `invert_x`/`invert_y`
are applied only when encoding a move's direction for the chip, never when reading the
displacement counter back — so position accumulated in *chip* space while all targets and
limits live in *logical* space. On an inverted axis those run opposite, so the tally
saturated at the limit the axis was being driven **away from**, and the clamp then blocked
exactly the moves that would escape it. This board runs `invert_y=1` and `invert_x=0`,
which is why only tilt ever stuck.

**2. Stalling was recorded as success.** The driver's monitor thread committed the
*predicted* target when a move ended, so ramming an end-stop — or issuing a move too small
to execute — was logged as a completed move and inflated the tally. It now commits the
displacement the chip actually measured.

**3. Sub-quantum moves ran away.** Motion is encoded as `abs(steps)/16`, so a move under
16 steps encodes to target 0 — which does not mean "stand still", it commands the chip to
drive *to position 0*. The channel never reports arrival, so it ran until the 15 s
watchdog and corrupted the tally. Observed as a 13-step preset correction sending pan to
its minimum. Such requests are now dropped in the driver, covering every caller.

The daemon's edge guards were also made direction-aware: they still suppress pushes
*further* into a limit (their real purpose, anti-oscillation) but no longer discard the
move that leaves it, on the relative, in-flight and absolute paths alike.

### Keep-off margin

The chip counts steps it *commanded*, not steps achieved — there is no encoder — so
stalling against a mechanical stop loses steps invisibly and degrades preset accuracy.

Note the asymmetry: `motor_homing()` rams the **far** stop and declares that point
`max_steps`, so only that end is a real physical reference. Position `0` is never
measured — it is just "`max_steps` counts below the top". Ramming the far stop is
therefore self-correcting (you always end up at the same physical place), while ramming
the `0` end silently de-anchors the whole axis.

This is why an over-large `max_steps` shows up as slipping at the **far** end, which is
misleading. Too large, and counter `0` sits below the real near stop; every trip to the
bottom rams it and loses steps, so the counter now reads low relative to the mechanism.
Drive back up and the gears hit the far stop long before the counter reaches its limit.
The noise is at the top; the damage was done at the bottom.

Each axis therefore reserves a **keep-off margin** at both ends. Normal PTZ and presets
never reach the stops; only the once-per-boot homing sweep does, and that re-zeroes
rather than accumulating error.

**A margin only works if `steps_pan`/`steps_tilt` match real travel.** The stock
`steps_tilt` for this board was **2730, but the tilt axis physically travels only
~1930 steps** — so counters above ~1950 were a ~780-step (~35°) dead zone where the
motor turned and the gears audibly slipped. No margin can help there: 48 and 200 both
sit deep inside the dead zone, which is why raising it changed nothing. `steps_pan` was
verified accurate, which is why pan never misbehaved.

Measured with a monotonic sweep (all moves one direction, no reversals), snapshotting
every 100 steps and looking for where the image stops changing:

| Axis | Configured | Real travel | Margin | Backlash |
|---|---|---|---|---|
| Pan | 7850 | 7850 ✅ | 48 | ~16 steps (~0.7°) |
| Tilt | 2730 ❌ → **1850** | ~1930 | 160 | ~80 steps (~3.5°) |

`steps_tilt` is set slightly *below* measured travel so counter 0 lands just above the
real bottom stop. The margin then has to beat that axis's backlash — 160 > 80 for tilt,
48 > 16 for pan. Verified: at both tilt limits there is still real travel left, five
full sweeps plus relative-path pushes produce no stalls and 0 watchdog timeouts.

To re-measure on another unit: set `margin_tilt` to 0, drive to one end, then step to
the other in 100-step increments comparing snapshots. Kill `daynight` first — the IR-cut
flip reads as a huge false "movement".

Tunable at runtime, no rebuild needed:

```bash
jct /etc/thingino.json set motors.margin_tilt 160
/etc/init.d/S59motor restart      # see caveat below
```

If an axis still reaches a stop, **check `steps_*` before raising the margin** — a
margin cannot compensate for a wrong travel figure, and raising it just wastes range.
Use the re-measurement procedure above. The margin is capped at `max_steps/4` so a bad
value can't immobilise an axis. Changing `steps_*` needs a reboot (it is a module
parameter); a margin change only needs the daemon restarted.

> `S59motor restart` fails silently if anything still holds the motor module: `stop()`
> runs `rmmod motor` and `exit 1`s, so the restart never re-homes and the old margins
> stay live. It leaves the position counter untouched, which looks like the new margin
> "did nothing". Check with `motors -j` — if `speed` didn't reset, the restart aborted.
> Restarting only the daemon (`kill` its PID, then `start-stop-daemon -S -b -m -p
> /run/motors-daemon.pid -x /usr/bin/motors-daemon -- -d -p`) is enough for a margin
> change and avoids the module entirely.

## Lean web UI

Two hand-written pages in [`webui/`](webui/) — a login screen and a live view with
PTZ — plus three CGIs in `overlay/var/www/x/`. Scope is deliberately **login, live
view, pan/tilt**; everything else is done over SSH.

Authentication is the stock one (`login.cgi` → `/etc/shadow` via `mkpasswd -m sha512`,
cookie sessions in `/tmp/sessions`), so the pages inherit it by calling CGIs that
already `require_auth`. First-boot Wi-Fi provisioning is untouched: it runs from a
**separate docroot** (`/var/www-portal`, served when `S38wpa_supplicant` finds no
`ssid=` and raises the `THINGINO-xx` AP), so replacing `/var/www` cannot strand a
fresh camera.

**Video is WebRTC, not MJPEG.** MJPEG was tried first and tops out at ~3 fps on this
SoC — `[jpeg] fps = 20` measured *slower* than `fps = 10`, at 20 % CPU and load 4.4,
so it is a hardware ceiling, not configuration. `x/mjpeg.cgi` remains as a fallback
(the stock `x/ch*.mjpg` are dead here: they `exec prudyntctl`, which does not exist on
a raptor build). The live view instead uses raptor's WHIP signalling with a
1080p/360p selector.

**PTZ drives the `ptz-glide` daemon**, not repeated `motors -d g` steps. Per that
daemon's own notes, small relative moves are fork-rate limited to ~3/sec on this SoC
and look jerky; writing intent to `/tmp/ptz_glide` gets one profiled accelerate-
cruise-decelerate seek, identical to ONVIF.

| CGI | Purpose |
|---|---|
| `x/ptz.cgi` | press-and-hold PTZ via `/tmp/ptz_glide`; parses its query string **without `eval`** |
| `x/mjpeg.cgi` | same-origin MJPEG proxy for rhd (fallback preview) |
| `x/webrtc-whip.cgi` | **override** of raptor's WHIP proxy — see below |

### Why we override `webrtc-whip.cgi`

Two defects in the stock file, both fixed in our copy:

1. **It corrupted the SDP answer.** `sdp="$(cat "$body_file")"` — command substitution
   strips trailing newlines, so rwd's final CRLF lost its LF. Chrome read the dangling
   CR as line content and rejected the last `a=candidate` with *"Invalid SDP line"*.
   Signalling succeeded (rwd logged `WHIP: session created`) and then ICE never
   started, so the symptom was a black preview with no useful error.
2. **It had no authentication at all** — while supplying the `[webrtc]` credentials to
   rwd on the caller's behalf, so anyone who could reach the WebUI port could open a
   video session anonymously.

> Order matters when adding auth to a raptor CGI: `auth.sh` dereferences variables that
> are unset on non-browser requests (`HTTP_ACCEPT`, `HTTP_COOKIE`). Under raptor's
> `set -eu` the shell aborts before it can emit the 401, uhttpd sees an empty response
> and returns a confusing **502**. Source `auth.sh` *before* `set -eu`.

Still on the stock UI's shoulders for now: the remaining `/var/www` pages are installed
and unused. Stripping `thingino-webui` (and vendoring the handful of CGIs we keep) is a
separate step — worth ~139 KB compressed, measured, and deliberately not conflated with
getting the pages working.

## Motion detection

The vendor IVS algorithms in this T23 `libimp` are unusable — both fail inside the blob
with the HAL calling correctly, so there is nothing to work around at our level:

| `algorithm` | Interface created | Channel created | Result |
|---|---|---|---|
| `move` | ✅ | ✅ | **SIGSEGV** in `rvd` once frames flow (`epc=0, ra=0` — call through a NULL pointer) |
| `base_move` | ✅ | ❌ `-1` | `IMP_IVS_CreateChn` rejects the interface; fails cleanly |

So `tree-overrides/package/all-patches/thingino-raptor/` adds **`algorithm = simple`**: a
frame-difference detector that skips IVS entirely and reads NV12 frames straight from
FrameSource — the same route the JZDL standalone path uses, and an API the encoder
exercises every frame, so it is known good here. Each grid zone's mean luma is compared
against the previous processed frame; a zone is active when the mean shifts past the
sensitivity threshold. Results feed the existing `ivs_process_move_result()`, so OSD, RMD
and recording behave exactly as with the vendor algorithm.

Only the Y plane is read, subsampled 4× in both axes — a few thousand byte loads per
frame. Measured CPU cost was **within noise** of motion-disabled, and it needs **no extra
libraries**: `IVS_DETECT` stays off, so `libjzdl` + `libstdc++` (~655 KB compressed) are
never linked. Enabling it costs only `rmd` at **13.8 KB**.

```ini
[motion]
enabled = true
algorithm = simple
sensitivity = 3      # 0 (least) - 4 (most); threshold 24/16/10/6/3 luma levels
grid = 4x4           # 16 zones
skip_frames = 5      # process every Nth frame
```

Verified on hardware: static scene 12 s → **0** events; a real pan/tilt → motion; a move
clamped to zero travel → **0** events. That last pair is the signal an auto-calibration
routine needs — "did the mechanism actually move?" — without any host-side tooling.

> `rmd` is only the controller; the detector runs inside `rvd`. `raptorctl rmd status`
> reports rmd's *recording* state machine, not the motion flag — during detection it
> still reads `idle` unless `record = true`. Check `logread` for `motion detected`.

### Notes for anyone working on this

- `motors -r` performs a **physical homing sequence** — it is not a counter reset.
- `POS_L/H` and `PHASE_L/H` are the **same registers**: a signed displacement counter
  zeroed at each move start, not an absolute position.
- Motion is quantised to 16 steps — a 1000-step request moves 992.
- Verify movement from **snapshots**, never from the position counter.
- To iterate without a full rebuild, build `motor.ko` against
  `output/*/build/linux-*` and the daemon with
  `make CROSS_COMPILE=mipsel-linux- JCT_PREFIX=<output>/staging/usr` (do **not** pass
  `SYSROOT`, it breaks the header search), push both to `/tmp`, then `rmmod`/`insmod` and
  run the daemon from there. Nothing touches flash and a reboot restores the image.
- `pkill -x` silently fails on process names longer than 15 characters (`comm`
  truncation) — kill by PID.

## Credits

Built on [thingino](https://github.com/themactep/thingino-firmware) by themactep and
contributors. Firmware components remain under their upstream licences.
