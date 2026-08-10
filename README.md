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
| Pan/tilt — via web UI and ONVIF absolute moves / presets | ✅ |
| IR-cut + automatic day/night (gain-based) | ✅ |
| Wi-Fi setup, web UI, SSH | ✅ |
| ONVIF **continuous** (press-and-hold) PTZ | ⚠️ see [Known issues](#known-issues) |

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
- **ONVIF PTZ patch + `ptz-glide` daemon** — work in progress, see Known issues.

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
```

First run clones upstream into `build/thingino/` (~11 GB with the download cache); later
runs reuse it. Overrides: `THINGINO_DIR=<path>`, `PIN=<sha>`, `NO_PIN=1`.

> **Use `CLEAN=1` whenever you disable a package or change `BR2_THINGINO_RMEM_MB`.**
> Buildroot doesn't prune `target/` when a package is removed, so an incremental build
> silently keeps stale binaries — and the U-Boot env generator only *appends* `osmem`/`rmem`
> if absent, so an incremental build keeps the old memory split.

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

## Known issues

**ONVIF continuous (press-and-hold) PTZ is unreliable.** Absolute moves, presets, and the
web UI controls all work; holding a direction button in an ONVIF client does not work
consistently. Root cause: the motor is **open-loop with no position feedback** — firmware
only counts commanded steps. The counter drifts from reality (driving into the end-stops
makes it worse), and once it is pinned at an end, moves in that direction are clamped away
and silently do nothing, so one axis direction stops responding.

Notes for anyone picking this up:

- `motors -r` performs a **physical homing sequence** — it is not a counter reset.
- Absolute moves (`motors -d h`) silently do nothing once the counter desyncs; relative
  moves (`motors -d g -x <x> -y <y>`, **always pass both axes**) keep working. That is what
  the web UI does — see `/var/www/x/json-motor.cgi` on the camera.
- Verify movement from **snapshots**, never from the position counter: it happily reports
  motion that didn't physically happen.

## Credits

Built on [thingino](https://github.com/themactep/thingino-firmware) by themactep and
contributors. Firmware components remain under their upstream licences.
