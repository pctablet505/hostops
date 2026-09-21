#!/usr/bin/env bash
# One root action. Installs the daemon, the unit and the udev rule, retires the
# fixed-limit boot service, then proves the result by reading the UPS back.
set -euo pipefail
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

install -m 0755 bin/ups-gpu-power /usr/local/bin/ups-gpu-power
install -m 0644 systemd/ups-gpu-power.service /etc/systemd/system/
install -m 0644 systemd/99-cyberpower-ups.rules /etc/udev/rules.d/

udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw

# Two units writing the same limit race at boot and the loser wins silently.
systemctl disable --now nvidia-power-limit.service 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now ups-gpu-power

sleep 2
echo "--- state after install ---"
systemctl --no-pager --lines=15 status ups-gpu-power || true
nvidia-smi --query-gpu=power.limit --format=csv
