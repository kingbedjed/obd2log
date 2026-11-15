# obd2log: notes for Claude

The tool repo. No personal data belongs here: no VINs, logs, photos,
manual images or user paths. Those live in the user's records repo (path in
`~/.config/obd2log/config.toml`), which has its own CLAUDE.md for working cases.

## Hard rules

- **Read-only.** `ReadOnlyCar.query` (src/obd2log/cli.py) allows only modes
  {01, 02, 03, 06, 07, 09, 0A, 22}. Never send 04 (clear codes) or any write /
  actuate / routine service (2E, 2F, 31, 3B, ...), including in raw-serial
  debugging: AT commands and read requests only. `tests/test_readonly.py`
  pins this.
- **Don't guess manufacturer PIDs.** A mode-22 PID needs a cited source for
  its address *and* scaling; otherwise log raw and label it unverified.
- VINs: display masked (first 11 + ******); store only sha256 for matching.

## Layout

- `src/obd2log/cli.py`: car link + read-only guard, check/codes/monitors/log,
  summarize/analyze, vehicle/case scaffolding, argparse
- `src/obd2log/records.py`: config, vehicle-by-VIN-hash, case date windows
- `src/obd2log/report.py`: figures, HTML→PDF (Chrome headless), PDF
  attachments (pypdf), rclone upload
- `autostart/`: udev + systemd user unit generator
- `examples/sample-records/`: synthetic records for trying `report`

## Debugging a link

`obd2log check` diagnoses no-adapter / no-car-power (ATRV `--.-V`) /
ECU-silent. For raw work, use pyserial at 115200: `ATZ`, `ATE0`, `ATH1`,
`ATSP6`, `ATSH7E0`, then the request; a `7F <mode> <nrc>` reply is a
negative response (0x31 = requestOutOfRange = unsupported).

To watch a live log, run `log` in the background and tail the CSV (it
flushes every 10 rows) with a small script that reports milestones and
code changes.

## Interpreting

- Freeze frame (mode 02) = conditions when a code set; often the most
  informative single reading.
- Permanent codes (0A) clear only when the car's own monitor passes.
- Mode 06 misfire TIDs 0B (10-cycle average) and 0C (this cycle) are
  SAE-standard; TIDs 0x80+ are manufacturer-defined but carry their own
  units and limits.
- Single-sample fuel-trim spikes on tip-in / lift-off are normal.
