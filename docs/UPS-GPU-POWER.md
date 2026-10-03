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

## Free instrumentation: the PSU already reports its own power

The **Corsair HX1200i** binds the in-kernel `corsair-psu` driver and exports
hwmon, readable with no privileges at all:

```bash
cat /sys/class/hwmon/hwmon3/power1_input   # microwatts, PSU DC output (sum of the rails), not wall draw
```

Measured 2026-10-03: `power1` read 476 W while the rails summed to 471.5 W, so
wall draw is higher by the PSU's losses. The hwmon index is not stable; find
the node by `name` = `corsairpsu`.

The daemon logs this on every transition, and `power-ledger` (below) records
it continuously — the 2026-09-11 Xid was diagnosed without such a record.

## Install

```bash
sudo /home/pctablet505/Projects/hostops/install.sh
```

It installs the binary, the unit and the udev rule, disables the old
fixed-375 W `nvidia-power-limit.service` (two units writing one limit race at
boot), starts the daemon and prints the resulting limit. It also installs
`power-ledger`. `install.sh ups-gpu-power` or `install.sh power-ledger`
installs just one.

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

## Power ledger

`power-ledger` is a service that records outages and power draw in
`/var/lib/power-ledger/ledger.db`: a row every 60 s on mains, every 5 s on
battery, and one the moment the UPS status byte changes (status, charge,
runtime, load, input and battery volts, PSU DC output, GPU watts and limit). It
only reads the UPS (opened read-only, GET_FEATURE only; a test enforces it) and
never touches `ups-gpu-power`.

```bash
power-ledger status | tail | outages [--days N] | energy [--days N]
```

Install: `sudo /home/pctablet505/Projects/hostops/install.sh power-ledger`.
Roll back: `sudo systemctl disable --now power-ledger`, delete
`/etc/systemd/system/power-ledger.service` and `/usr/local/bin/power-ledger`,
then `sudo systemctl daemon-reload`. The data stays until you delete it.
