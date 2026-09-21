# UPS-aware GPU power cap

**Policy:** mains present → **550 W**. On battery → **200 W**. State unknown →
200 W. The daemon never fails open.

## Why a daemon and not a cron job

The UPS is a **CyberPower OLS2000EC** (2000 VA / 1800 W, online
double-conversion) on USB `0764:0601`. It exposes a standard HID Power Device
interface, and its status report is an **input** report — the device *pushes*
it the instant it transfers to battery. A polling loop would leave the GPU at
550 W for the whole poll interval after a cut; a blocking read reacts in
milliseconds. That is the only reason this runs as a service.

Report layout, decoded from the device's own `report_descriptor` (world
readable, so this is not guessed):

| report | field | offset |
|---|---|---|
| 11 (input + feature) | `ACPresent` / `Charging` / `Discharging` | bits 0 / 1 / 2 |
| 8 (feature) | `RemainingCapacity` %, then `RunTimeToEmpty` (u16 LE seconds) | bytes 0, 1-2 |
| 19 (feature) | `PercentLoad` | byte 0 |

A 30 s resync still re-reads the feature report, so a missed push event cannot
strand the cap at 550 W while on battery.

## This is a runtime guard, not an overload guard

At 1800 W the UPS can *carry* the GPU at full tilt — the box draws ~310 W idle
(measured from the PSU, below) and would reach perhaps 950 W with the GPU
saturated. Nothing trips. What changes is **battery runtime**, which falls
roughly as the inverse of load.

Read off the UPS itself on 2026-09-21, idle: **battery 100%, runtime 1080 s
(18 min) at 20% load (~310 W)**. A saturated GPU roughly triples that load, so
the same battery gives well under 10 minutes — and the 550 → 200 W drop is
most of the difference between riding out a flicker and dying mid-write.

## 550 W is a ceiling, not a preference

`HARD_MAX = 550` in the daemon clamps every path. Recorded incident
**2026-09-11**: the card threw **Xid 79 — "GPU has fallen off the bus"**
3 min 52 s after the limit went 550 → 600 W under full load; only a reboot
recovered it. 550 W itself ran ~1.5 h clean under saturation stress, so the
mains figure sits inside measured-good territory and 600 W stays out of reach.

## Free instrumentation: the PSU already reports wall draw

The **Corsair HX1200i** binds the in-kernel `corsair-psu` driver and exports
hwmon, readable with no privileges at all:

```bash
cat /sys/class/hwmon/hwmon3/power1_input   # microwatts, total system draw
```

The daemon logs this on every transition. It is also the continuous power
record the box has never had — the 2026-09-11 Xid was diagnosed without one.

## Install

```bash
sudo /home/pctablet505/Projects/hostops/install.sh
```

It installs the binary, the unit and the udev rule, disables the old
fixed-375 W `nvidia-power-limit.service` (two units writing one limit race at
boot), starts the daemon and prints the resulting limit.

## Verify, any time, without sudo

```bash
ups-gpu-power --check
```

Prints the resolved device, mains state, battery %, runtime, UPS load, PSU
draw, and the cap it would apply. Changes nothing.

## Tuning

`MAINS_WATTS` / `BATTERY_WATTS` at the top of `/usr/local/bin/ups-gpu-power`,
then `sudo systemctl restart ups-gpu-power`. `HARD_MAX` should not move
without a measured power path.
