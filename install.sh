#!/usr/bin/env bash
# One root action. Installs the daemon, the unit and the udev rule, retires the
# fixed-limit boot service, then proves the result by reading the UPS back.
# With a component argument it installs only that one: ups-gpu-power or power-ledger.
set -euo pipefail
cd "$(dirname "$0")"

what="${1:-all}"
case "$what" in all|ups-gpu-power|power-ledger) ;; *) echo "usage: install.sh [all|ups-gpu-power|power-ledger]" >&2; exit 2 ;; esac

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

if [ "$what" != power-ledger ]; then
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
fi

if [ "$what" != ups-gpu-power ]; then
    install -m 0755 bin/power-ledger /usr/local/bin/power-ledger
    install -m 0644 systemd/power-ledger.service /etc/systemd/system/power-ledger.service
    systemctl daemon-reload
    systemctl enable --now power-ledger
    sleep 3
    echo "--- power-ledger after install ---"
    systemctl --no-pager --lines=10 status power-ledger || true
    runuser -u pctablet505 -- /usr/local/bin/power-ledger status || true
fi
