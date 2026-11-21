# obd2log

Read-only OBD-II logging for an ELM327 adapter, organised per vehicle and per
problem ("case"), with PDF reports, built so an AI assistant (Claude Code) can
drive a diagnostic session: run the commands, read the data, write the case up.

- **Read-only by construction.** Every request goes through one guard that
  allows only service modes 01, 02, 03, 06, 07, 09, 0A and 22. Clearing codes (04) and all
  write / actuate / routine services are refused before anything is sent.
- **Logs everything the car exposes:** every supported mode 01 reading (core
  set at 1 Hz, the rest every 10 s), stored / pending / permanent codes, freeze
  frame, readiness monitors, mode 06 self-tests (per-cylinder misfire counts),
  calibration ID.
- **Records live in a separate repo**, one folder per vehicle, recognised by a
  hash of the VIN. The tool holds no personal data.
- **Case reports:** one PDF per problem with the write-up, plan, photos,
  figures, and every raw log and snapshot embedded as attachments.

## Install

```sh
git clone <this repo> ~/git/obd2log && cd ~/git/obd2log
python3 -m venv .venv && .venv/bin/pip install -e '.[report]'
ln -s "$PWD/.venv/bin/obd2log" ~/.local/bin/obd2log      # or: pipx install '.[report]'
```

Linux: your user must be in the `dialout` group to open the adapter. Reports
need Chrome or Chromium for HTML→PDF. Uploads need rclone.

## Configure

`~/.config/obd2log/config.toml`:

```toml
records = "~/git/car-records"           # your records repo (see examples/sample-records)
rclone_remote = "gdrive:Car Records"    # optional, for `obd2log upload`
port = "/dev/serial/by-id/usb-..."      # optional; auto-detected otherwise
```

Without `records`, data goes to `~/obd2log-data/unassigned/`.

## Records layout

```
<records>/vehicles/<vehicle>/
  vehicle.md          frontmatter: title, vin_masked, vin_sha256, protocol
  maintenance.md
  reference/          manuals, diagrams
  logs/<YYYY>/<YYYYmmdd_HHMMSS>.csv.gz
  snapshots/<YYYY>/<YYYYmmdd_HHMMSS>_<check|codes|monitors>.json
  inspections/<YYYY>/<YYYYmmdd_HHMMSS>_inspect/report.{md,json,pdf}
  cases/<YYYY-MM-DD>_<slug>/
    case.md           frontmatter: title, opened, closed, status, include, exclude
    photos/
    report/report.pdf
<records>/unassigned/ data from a car with no vehicle folder yet
```

A case's report uses the vehicle's logs and snapshots dated `opened`..`closed`
(or today), plus `include`, minus `exclude` (comma-separated file names). Image
paths in case.md are relative to case.md. See `examples/sample-records/`.

## Use

Plug the adapter into the OBD port, ignition ON (the engine can stay off for
everything except driving logs).

```sh
obd2log check                      # link, protocol, battery, masked VIN, calibration, supported fields
obd2log vehicle add <slug>         # first time with a car: create its profile (VIN hash)
obd2log codes                      # stored/pending/permanent codes, freeze frame, readiness
obd2log monitors                   # mode 06 self-tests incl. per-cylinder misfire counts
obd2log log                        # one .csv.gz per ignition cycle; Ctrl-C or ignition off ends it
obd2log log --auto                 # wait for the car and log every drive (used by autostart)
obd2log case new <vehicle> <slug>  # start a case folder with a template case.md
obd2log report <case-folder>       # PDF report
obd2log summarize [log]            # markdown summary of a log (newest by default)
obd2log analyze [log ...]          # summary + vehicle.md -> `claude -p`
obd2log upload [path]              # rclone copy of the records repo to Drive
obd2log inspect [--make M --model X]   # used-car check: everything readable -> one report
obd2log pids fetch <make> [model]  # cache an OBDb manufacturer signalset (do it while online)
obd2log pids search <text>         # find OBDb repos, e.g. `pids search escape`
```

### Used-car inspection

`obd2log inspect` makes one read-only pass over everything the car reports and
writes `report.md`, `report.json` (with raw replies) and `report.pdf` to the
vehicle's `inspections/<YYYY>/<stamp>_inspect/` (or `unassigned/` for a car
with no profile, e.g. one you're looking at).

- Standard OBD-II: check-engine status, readiness monitors (since codes were
  cleared and this drive cycle), stored / pending / permanent codes, freeze
  frame, distance / warm-ups / engine time since codes were cleared, distance
  with the light on, odometer (01 A6, mostly 2019+), live readings, mode 06
  self-tests, and every other mode 01 PID the car supports.
- `--make ford --model escape`: every request in the matching
  [OBDb](https://github.com/OBDb) signalset (community data, CC-BY-SA-4.0),
  for the model year from the VIN (or `--year`). `--make` alone uses the
  make-wide set; `--make-wide` adds it to a model set. Signalsets are cached
  in `~/.cache/obd2log/obdb/`, so fetch them before going somewhere with no
  network.
- **Every request is listed with its outcome:** ok, no response, negative
  response (with the module's reason, e.g. `7F 22 31` = PID not supported),
  not decodable, error, or not sent (with the reason: outside the read-only
  allowlist, 29-bit or extended addressing, needs a diagnostic session, or a
  different model year).
- A findings section flags the usual buyer gotchas: recently cleared codes,
  incomplete monitors, permanent codes, distance driven with the light on, an
  engine warmed up before you arrived, charging voltage, fuel trims, failed
  self-tests and misfires, and odometer readings from each module that
  reports one. These are rules of thumb, not diagnoses.

Run it twice if you can: key on, engine off, cold (before the seller starts
it), then again after the test drive with the engine running.

`check`, `codes` and `monitors` save a JSON snapshot to the vehicle by default
(`--save -` to skip). If a link fails, `check` says whether the adapter is
missing, has no power from the car, or the ECU isn't answering.

### Autostart logging (Linux, systemd)

With the adapter plugged in: `autostart/install.sh` (needs sudo for the udev
rule). Plugging the adapter in starts `obd2log log --auto`, and unplugging it stops the
service. The laptop must stay awake. `autostart/uninstall.sh` removes it.

## Manufacturer-specific readings

Only standard OBD-II is decoded by default. `log --ford-cht` requests Ford PID
`22 1624` (cylinder head temperature); its address is widely cited, but its scaling
is not verified, so the raw value is logged next to an UNVERIFIED conversion.
The 2014 Escape 1.6L rejects it (`7F 22 31`). New mode-22 PIDs need a cited
source for both address and scaling. OBDb counts as a source for `inspect`, which
labels its values community-sourced and keeps the raw reply.

## Development

`pytest` (install with `.[dev]`). Built on
[python-OBD](https://github.com/brendan-w/python-OBD).
