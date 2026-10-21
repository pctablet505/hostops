<a href="docs/UPS-GPU-POWER.md">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/hero-dark.svg">
  <img src="assets/hero-light.svg" width="100%" alt="hostops. The GPU's power limit follows the UPS, not a fixed number. Root-level services for one GPU workstation: a power cap and a power ledger.">
</picture>
</a>

**The GPU's power limit follows the UPS, not a fixed number.** hostops holds the root-level services of one GPU
workstation: a daemon that sets the GPU power limit from the UPS state, and a ledger of outages and power draw.

Why a daemon, and not a cron job? The answer is in how the UPS reports a power cut.
**[Read the policy and the reason →](docs/UPS-GPU-POWER.md)**

## What you get

- **`ups-gpu-power`**: 550 W on mains, 200 W on battery, and 200 W when the state is unknown. It never fails open.
- **`power-ledger`**: records UPS outages and power draw to SQLite, with a command to query it.

## Is it for you?

For one Linux workstation with an NVIDIA GPU behind a USB UPS that speaks the HID power-device protocol. It was
built for a CyberPower OLS2000EC.

What it is not:

- **Not general-purpose.** The limits and the device rule match one machine; read the doc before using it on yours.
- **Not an overload guard.** It protects battery runtime, not the UPS rating.
- **Not free of root.** Everything here needs it, which is why it lives apart from
  [Servedeck](https://github.com/pctablet505/servedeck), where nothing does.

## Quick start

```bash
git clone https://github.com/pctablet505/hostops
cd hostops
sudo ./install.sh                  # or: sudo ./install.sh ups-gpu-power, or power-ledger
```

The installer adds the services and the udev rule, retires a fixed-limit boot service if one exists, and prints the
GPU power limit it set.

---

<picture><source media="(prefers-color-scheme: dark)" srcset="assets/mark-dark.svg"><img src="assets/mark-light.svg" width="16" height="16" alt=""></picture>
<sub>**Real numbers, failures left in.** One of the [open-source tools of RL Alpha Labs](https://rlalphalabs.com/tools/), built by [pctablet505](https://github.com/pctablet505).</sub>
