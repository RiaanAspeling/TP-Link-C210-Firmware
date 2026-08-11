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

## Layout

| Path | What |
|------|------|
| `user/<board>/` | Board layer: `local.fragment` (defconfig), `thingino.json` (runtime config) |
| `overlay/` | Files copied into the rootfs (gpio, day/night, init scripts, raptor.conf) |
| `tree-overrides/` | Files overwritten in the upstream tree (package tweaks, patches) |
| `scripts/build.sh` | Clone/pin upstream → apply layer → Docker build → image into `images/` |
| `scripts/apply-layer.sh` | Apply this layer to an upstream tree (idempotent) |
| `webui/` | Custom web UI (planned) |
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
Homing anchors the far stop to `max_steps`, so an unmargined range would end *on* the
stops and every trip to a limit would stall.

Each axis therefore reserves a **keep-off margin** at both ends, default 48 steps (~2°,
about 3.5% of tilt travel and 1.2% of pan). Normal PTZ and presets never reach the
stops; only the once-per-boot homing sweep does, and that re-zeroes rather than
accumulating error.

Tunable at runtime, no rebuild needed:

```bash
jct /etc/thingino.json set motors.margin_tilt 80
/etc/init.d/S59motor restart
```

Raise it if an axis still reaches a stop — the margin also has to absorb however much
`steps_pan`/`steps_tilt` over-estimates true travel, and that gap can't be measured
without feedback the hardware doesn't provide. It must exceed both the 16-step hardware
quantum and the 24-step edge deadband to have any effect (10 steps, say, would do
nothing), and is capped at `max_steps/4` so a bad value can't immobilise an axis.

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
