# hostops

Root-level services for this workstation. Deliberately separate from
`servedeck`, which holds the line that it uses **no sudo anywhere**; anything
needing root lives here instead.

- `bin/ups-gpu-power` — holds the GPU power limit inside the UPS budget
  (550 W on mains, 200 W on battery). See `docs/UPS-GPU-POWER.md`.
- `bin/power-ledger` — records UPS outages and power draw to SQLite, with a CLI
  to query it. See `docs/UPS-GPU-POWER.md`.
