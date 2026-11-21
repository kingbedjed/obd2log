#!/usr/bin/env python3
"""Read-only OBD-II logger: link check, trouble codes, CSV logging, summaries for Claude.

Subcommands:
  check      verify the adapter link and list what the car supports
  codes      read stored (03), pending (07) and permanent (0A) codes plus freeze frame
  log        log readings to CSV until Ctrl-C
  summarize  condense a CSV into a short markdown report
  analyze    summarize a CSV and ask Claude (claude -p) to interpret it

Nothing here clears codes or writes to a module; see READ_ONLY_MODES.
"""

import argparse
import csv
import json
import datetime as dt
import gzip
import math
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import records

FALLBACK_DIR = Path.home() / "obd2log-data"   # used when no records repo is configured

# Service modes this tool may send (06 = on-board self-test results). Mode 04 (clear codes), 2E/2F/31/3B/3D
# (write / actuate / routines) and everything else are refused.
READ_ONLY_MODES = {"01", "02", "03", "06", "07", "09", "0A", "22"}

STANDARD_FIELDS = [
    # (csv column, python-OBD command name, unit to convert to)
    ("coolant_temp_c", "COOLANT_TEMP", "degC"),
    ("rpm", "RPM", None),
    ("speed_kph", "SPEED", "kph"),
    ("engine_load_pct", "ENGINE_LOAD", None),
    ("intake_air_temp_c", "INTAKE_TEMP", "degC"),
    ("ambient_air_temp_c", "AMBIANT_AIR_TEMP", "degC"),
    ("stft_b1_pct", "SHORT_FUEL_TRIM_1", None),
    ("ltft_b1_pct", "LONG_FUEL_TRIM_1", None),
    ("stft_b2_pct", "SHORT_FUEL_TRIM_2", None),
    ("ltft_b2_pct", "LONG_FUEL_TRIM_2", None),
]


def _require_obd():
    try:
        import obd
    except ImportError:
        sys.exit("python-OBD missing: run with ./.venv/bin/python or `pip install obd`")
    return obd


# --------------------------------------------------------------------------
# Ford cylinder head temperature (mode 22, PID 0x1624, PCM at 7E0)
#
# The PID address is consistent across ScanGauge's Ford CANSF list
# (07E0 221624), Torque's Ford PID set and forum posts. The SCALING is not:
# the only formula found, ((A*256)+B)*1.999 degF, was curve-fitted by a forum
# user on a 2004-08 F-150 5.4L. No Ford or SAE source confirms it for the
# 1.6L EcoBoost. Raw bytes are always logged; the decoded value is labelled
# UNVERIFIED until checked against a cold soak (see README).
# --------------------------------------------------------------------------
CHT_REQUEST = b"221624"


def cht_candidate_f(raw):
    return raw * 1.999


def _make_custom_commands(obd):
    from obd import OBDCommand
    from obd.decoders import dtc
    from obd.protocols import ECU

    def decode_cht(messages):
        d = bytes(messages[0].data)
        if len(d) < 5 or d[0] != 0x62 or d[1:3] != b"\x16\x24":
            return None  # negative response (7F 22 xx) or wrong PID
        return int.from_bytes(d[3:5], "big")

    def freeze(pid, fn):
        # Mode 02 needs the frame number byte; python-OBD's built-in mode 02
        # commands omit it, so the replies are mis-parsed on CAN.
        def decode(messages):
            d = bytes(messages[0].data)
            if len(d) < 4 or d[0] != 0x42 or d[1] != pid:
                return None
            return fn(d[3:])
        return decode

    temp = lambda b: b[0] - 40
    ff = {
        "FF_DTC": (0x02, 5, lambda b: _dtc_str(b[0:2])),
        "FF_COOLANT_TEMP_C": (0x05, 4, temp),
        "FF_ENGINE_LOAD_PCT": (0x04, 4, lambda b: round(b[0] * 100 / 255, 1)),
        "FF_RPM": (0x0C, 5, lambda b: (b[0] * 256 + b[1]) / 4),
        "FF_SPEED_KPH": (0x0D, 4, lambda b: b[0]),
        "FF_INTAKE_TEMP_C": (0x0F, 4, temp),
    }
    def decode_vin(messages):
        d = b"".join(bytes(m.data) for m in messages)
        i = d.find(b"\x49\x02")
        text = bytes(c for c in d[i + 2:] if 0x30 <= c <= 0x5A) if i >= 0 else b""
        return text[-17:].decode() if len(text) >= 17 else None

    def decode_ecu_name(messages):
        d = b"".join(bytes(m.data) for m in messages)
        i = d.find(b"\x49\x0a")
        return bytes(c for c in d[i + 3:] if 0x20 <= c < 0x7F).decode().strip() or None if i >= 0 else None

    def decode_odometer(messages):
        d = bytes(messages[0].data)
        return int.from_bytes(d[2:6], "big") / 10 if len(d) >= 6 and d[:2] == b"\x41\xa6" else None

    cmds = {
        "ECU_NAME": OBDCommand("ECU_NAME", "ECU name", b"090A", 0, decode_ecu_name, ECU.ALL, False),
        "ODOMETER": OBDCommand("ODOMETER", "Odometer (km)", b"01A6", 0, decode_odometer, ECU.ENGINE, False),
        "VIN17": OBDCommand("VIN17", "VIN", b"0902", 0, decode_vin, ECU.ENGINE, False),
        "FORD_CHT_RAW": OBDCommand("FORD_CHT_RAW", "Ford CHT raw (22 1624)", CHT_REQUEST,
                                   5, decode_cht, ECU.ENGINE, False, header=b"7E0"),
        "PERMANENT_DTC": OBDCommand("PERMANENT_DTC", "Permanent DTCs", b"0A", 0, dtc,
                                    ECU.ALL, False),
    }
    for name, (pid, nbytes, fn) in ff.items():
        cmds[name] = OBDCommand(name, name, b"02%02X00" % pid, nbytes, freeze(pid, fn),
                                ECU.ENGINE, False)
    return cmds


def can_id(message):
    """The CAN id a python-OBD message came from, from the ELM's header line (e.g. '7E8')."""
    raw = message.frames[0].raw if message.frames else ""
    tok = raw.split()
    if len(tok) > 1 and len(tok[0]) in (3, 8):
        return tok[0].upper()
    raw = raw.replace(" ", "")
    return raw[:3].upper() or None


def _dtc_str(b):
    if len(b) != 2 or b == b"\x00\x00":
        return None
    return "PCBU"[b[0] >> 6] + f"{(b[0] >> 4) & 3}{b[0] & 0xF:X}{b[1]:02X}"


class ReadOnlyCar:
    """python-OBD connection that refuses anything outside READ_ONLY_MODES."""

    def __init__(self, port=None, baud=None, timeout=1.0):
        obd = _require_obd()
        self.obd = obd
        self.conn = obd.OBD(portstr=port, baudrate=baud, fast=False, timeout=timeout)
        self.custom = _make_custom_commands(obd)

    @property
    def connected(self):
        return self.conn.is_connected()

    def ask(self, cmd, force=False):
        """The full python-OBD response (value and raw messages). Every request goes through here."""
        if isinstance(cmd, str):
            cmd = self.custom.get(cmd) or self.obd.commands[cmd]
        raw = cmd.command
        if not raw.startswith(b"AT"):
            mode = raw[:2].decode().upper()
            if mode not in READ_ONLY_MODES:
                raise PermissionError(f"refusing mode {mode} ({cmd.name}): not read-only")
        return self.conn.query(cmd, force=force)

    def query(self, cmd, force=False):
        r = self.ask(cmd, force=force)
        return None if r.is_null() else r.value

    def request(self, header, service, pid):
        """Send one read request to the module at `header` (11-bit CAN, hex) and return every
        reply as (CAN id, payload starting at the service byte). Goes through the same guard."""
        from obd import OBDCommand
        from obd.protocols import ECU
        cmd = OBDCommand(f"RAW_{header}_{service}{pid}", f"{header} {service} {pid}",
                         (service + pid).upper().encode(), 0, lambda m: m, ECU.ALL, False,
                         header=header.upper().encode())
        return [(can_id(m), bytes(m.data)) for m in self.ask(cmd, force=True).messages]

    def supports(self, name):
        return self.conn.supports(self.obd.commands[name])

    def close(self):
        self.conn.close()


def _mag(v, unit):
    if v is None:
        return None
    if hasattr(v, "magnitude"):
        v = v.to(unit) if unit else v
        v = v.magnitude
    return round(float(v), 2)


def _codes(v):
    return [c for c, _ in v] if v else []


# Mode 01 commands that are bitmaps, status words or one-off facts, not readings.
NOT_READINGS = {"PIDS_A", "PIDS_B", "PIDS_C", "STATUS", "FREEZE_DTC", "STATUS_DRIVE_CYCLE",
                "OBD_COMPLIANCE", "FUEL_TYPE", "O2_SENSORS", "O2_SENSORS_ALT",
                "AUX_INPUT_STATUS", "EMISSION_REQ", "FUEL_STATUS"}


def _val(v):
    """Any python-OBD value as a CSV/JSON-friendly scalar."""
    if v is None:
        return None
    if hasattr(v, "magnitude"):
        return round(float(v.magnitude), 3)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode(errors="replace").strip("\x00 ")
    if isinstance(v, (list, tuple)):
        return " ".join(str(_val(x)) for x in v)
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _readiness(st):
    """Readiness monitors from mode 01 PID 01: {name: 'complete'|'incomplete'} for available ones."""
    out = {}
    for name, test in vars(st).items():
        if isinstance(name, str) and hasattr(test, "available") and test.available:
            out[name.lower()] = "complete" if test.complete else "incomplete"
    return out


def _monitor_tests(result):
    """Mode 06 result -> list of dicts (tid, name, value, min, max, unit, passed)."""
    tests = []
    for t in getattr(result, "tests", []) or []:
        unit = str(t.value.units) if hasattr(t.value, "units") else ""
        tests.append({"tid": f"{t.tid:02X}", "name": t.name if t.name and t.name != "Unknown" else "manufacturer-defined",
                      "desc": t.desc or "", "value": _val(t.value), "min": _val(t.min),
                      "max": _val(t.max), "unit": unit, "passed": bool(t.passed)})
    return tests


def _diagnose_adapter(port):
    """Talk to the ELM327 directly to tell 'no adapter' from 'no car'."""
    import serial
    from serial.tools import list_ports
    ports = [port] if port else [p.device for p in list_ports.comports()
                                 if "USB" in p.device or "ACM" in p.device]
    for dev in ports:
        for baud in (115200, 38400):
            try:
                with serial.Serial(dev, baud, timeout=1) as s:
                    replies = []
                    for c, wait in ((b"ATZ\r", 1.2), (b"ATRV\r", 0.4)):
                        s.reset_input_buffer()
                        s.write(c)
                        time.sleep(wait)
                        replies.append(s.read(200).decode(errors="replace"))
            except serial.SerialException as e:
                return f"{dev}: {e} (in the dialout group? `groups`)"
            if "ELM" in replies[0]:
                volts = replies[1].replace("ATRV", "").strip(" \r>")
                if "-" in volts:
                    return (f"Adapter on {dev} answers ({replies[0].split()[-2]}) but reads no "
                            "vehicle voltage. Plug it into the OBD port and turn the ignition on.")
                return (f"Adapter on {dev} sees {volts} but the car's ECU did not answer. "
                        "Turn the ignition to ON (engine may be off).")
    return f"No ELM327 adapter answered on {ports or 'any USB serial port'}."


# --------------------------------------------------------------------------
# check / codes
# --------------------------------------------------------------------------

def _data_dir(vin):
    """Vehicle folder for this VIN in the records repo, or the fallback directory."""
    return records.vehicle_for_vin(vin) or FALLBACK_DIR / "unassigned"


def _save_snapshot(path, kind, data, vin=None):
    """Save to `path` (file or dir) or, by default, the vehicle's snapshots/<YYYY>/."""
    if path == "-":
        return
    path = Path(path) if path else records.stamped_dir(_data_dir(vin), "snapshots")
    if path.is_dir():
        path = path / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"kind": kind, "taken": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "obd2log live read", **data}
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    print(f"\nsaved {path}")


def _mask_vin(vin):
    # Keep the model/year/plant part (first 11), hide the serial number.
    return vin[:11] + "******" if vin and len(vin) == 17 else vin


def _port(args):
    return args.port or records.config().get("port")


def _connect_or_exit(args):
    car = ReadOnlyCar(_port(args))
    if not car.connected:
        car.close()
        sys.exit(_diagnose_adapter(_port(args)))
    car.vin = car.query("VIN17", force=True)
    return car


def cmd_check(args):
    car = _connect_or_exit(args)
    vin = car.vin
    cht = car.query("FORD_CHT_RAW", force=True)
    snap = {
        "port": car.conn.port_name(),
        "protocol": f"{car.conn.protocol_name()} ({car.conn.protocol_id()})",
        "adapter": car.query("ELM_VERSION"),
        "battery_v": _mag(car.query("ELM_VOLTAGE"), "volt"),
        "vin": vin if args.show_vin else _mask_vin(vin),
        "supported": {col: car.supports(name) for col, name, _ in STANDARD_FIELDS},
        "calibration_id": _val(car.query("CALIBRATION_ID", force=True)),
        "cvn": _val(car.query("CVN", force=True)),
        "ford_cht_22_1624": None if cht is None else {"raw": cht, "candidate_f_UNVERIFIED": cht_candidate_f(cht)},
    }
    car.close()
    for k in ("port", "protocol", "adapter", "battery_v", "vin", "calibration_id", "cvn"):
        print(f"{k + ':':16}{snap[k]}")
    print("\nstandard fields:")
    for col, ok in snap["supported"].items():
        print(f"  {'yes' if ok else 'no ':3}  {col}")
    print(f"\nFord CHT (22 1624): {'no response' if cht is None else f'raw={cht} -> {cht_candidate_f(cht):.1f} degF (UNVERIFIED scale)'}")
    _save_snapshot(args.save, "check", snap, vin)


def cmd_codes(args):
    car = _connect_or_exit(args)
    st = car.query("STATUS")
    snap = {"mil_on": None if st is None else bool(st.MIL),
            "status_dtc_count": None if st is None else st.DTC_count,
            "readiness": None if st is None else _readiness(st)}
    for key, name in [("stored", "GET_DTC"), ("pending", "GET_CURRENT_DTC"), ("permanent", "PERMANENT_DTC")]:
        v = car.query(name, force=True)
        snap[key] = None if v is None else [{"code": c, "description": d or ""} for c, d in v]
    snap["freeze_frame"] = {name[3:].lower(): car.query(name, force=True) for name in
                            ["FF_DTC", "FF_COOLANT_TEMP_C", "FF_RPM", "FF_SPEED_KPH",
                             "FF_ENGINE_LOAD_PCT", "FF_INTAKE_TEMP_C"]}
    vin = car.vin
    car.close()
    if st is not None:
        print(f"check-engine light: {'ON' if st.MIL else 'off'}   codes reported by status: {st.DTC_count}")
    for key, label in [("stored", "stored (03)"), ("pending", "pending (07)"), ("permanent", "permanent (0A)")]:
        print(f"\n{label}:")
        if snap[key] is None:
            print("  no response")
        elif not snap[key]:
            print("  none")
        for c in snap[key] or []:
            print(f"  {c['code']}  {c['description'] or '(no description in python-OBD; manufacturer-specific)'}")
    if snap["readiness"]:
        print("\nreadiness monitors:")
        for k, v in snap["readiness"].items():
            print(f"  {k:28} {v}")
    print("\nfreeze frame 0:")
    for k, v in snap["freeze_frame"].items():
        print(f"  {k:18} {v}")
    _save_snapshot(args.save, "codes", snap, vin)


def cmd_monitors(args):
    car = _connect_or_exit(args)
    snap = {}
    for cmd in sorted((c for c in car.conn.supported_commands if c.mode == 6 and not c.name.startswith("MIDS")),
                      key=lambda c: c.command):
        tests = _monitor_tests(car.query(cmd))
        if tests:
            snap[cmd.name.lower()] = tests
    vin = car.vin
    car.close()
    for mid, tests in snap.items():
        print(f"\n{mid}")
        for t in tests:
            print(f"  TID {t['tid']} {t['name']:22} {t['value']} {t['unit']}  "
                  f"[{t['min']}..{t['max']}] {'PASS' if t['passed'] else 'FAIL'}")
    if not snap:
        print("no mode 06 results")
    _save_snapshot(args.save, "monitors", {"monitors": snap}, vin)


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------

def _log_session(car, args, stop):
    """Log until stop, Ctrl-C, or the car stops answering. Returns the CSV path."""
    fields = [(c, n, u) for c, n, u in STANDARD_FIELDS if car.supports(n)]
    skipped = [c for c, n, _ in STANDARD_FIELDS if not car.supports(n)]
    curated = {n for _, n, _ in STANDARD_FIELDS}
    extra = [] if args.basic else sorted(
        (c for c in car.conn.supported_commands
         if c.mode == 1 and c.name not in curated and c.name not in NOT_READINGS),
        key=lambda c: c.command)
    misfire = [] if args.basic else [c for c in car.conn.supported_commands
                                     if c.name.startswith("MONITOR_MISFIRE_CYLINDER_")]
    misfire.sort(key=lambda c: c.command)
    use_cht = args.ford_cht and car.query("FORD_CHT_RAW", force=True) is not None
    if args.ford_cht and not use_cht:
        print("Ford CHT PID 22 1624 not answered; logging without it.")

    vin = car.query("VIN17", force=True)
    out_dir = Path(args.out_dir) if args.out_dir else records.stamped_dir(_data_dir(vin), "logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else out_dir / f"{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    cols = ["timestamp", "elapsed_s"] + [c for c, _, _ in fields]
    if use_cht:
        cols += ["cht_raw", "cht_f_UNVERIFIED"]
    cols += [c.name.lower() for c in extra]
    cols += [f"misfire_cyl{c.name.rsplit('_', 1)[1]}_cycle" for c in misfire]
    cols += ["battery_v", "stored_dtcs", "pending_dtcs"]

    print(f"logging to {out}\n  fields: {', '.join(cols[2:])}")
    if skipped:
        print(f"  not supported by this car: {', '.join(skipped)}")

    t0 = time.monotonic()
    next_dtc = 0.0
    stored = pending = ""
    misfires = [None] * len(misfire)
    next_extra = 0.0
    rows = silent = 0
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        while not stop:
            tick = time.monotonic()
            row = [dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                   round(tick - t0, 2)]
            values = [_mag(car.query(n), u) for _, n, u in fields]
            row += values
            if use_cht:
                raw = car.query("FORD_CHT_RAW", force=True)
                row += [raw, None if raw is None else round(cht_candidate_f(raw), 1)]
            if tick >= next_extra:
                row += [_val(car.query(c)) for c in extra]
                next_extra = tick + args.extra_every
            else:
                row += [None] * len(extra)
            if tick >= next_dtc:
                stored = " ".join(_codes(car.query("GET_DTC", force=True)))
                pending = " ".join(_codes(car.query("GET_CURRENT_DTC", force=True)))
                misfires = [next((t["value"] for t in _monitor_tests(car.query(c)) if t["tid"] == "0C"), None)
                            for c in misfire]
                next_dtc = tick + args.dtc_every
            row += misfires
            row.append(_mag(car.query("ELM_VOLTAGE"), "volt"))
            row += [stored, pending]
            silent = silent + 1 if all(v is None for v in values) else 0
            if silent >= 10 or not car.connected:
                print("\ncar stopped answering (ignition off?); closing this log")
                break
            w.writerow(row)
            rows += 1
            if rows % 10 == 0:
                f.flush()
                if sys.stdout.isatty():
                    print("\r  " + "  ".join(f"{c}={v}" for c, v in zip(cols[2:], row[2:])
                                             if v not in (None, "")), end="", flush=True)
            time.sleep(max(0.0, args.interval - (time.monotonic() - tick)))
    if rows == 0:
        out.unlink()
        print("\nno rows logged")
        return None
    if not args.no_gzip:
        gz = out.with_name(out.name + ".gz")
        with out.open("rb") as src, gzip.GzipFile(gz, "wb", mtime=0) as dst:
            shutil.copyfileobj(src, dst)
        out.unlink()
        out = gz
    print(f"\n{rows} rows -> {out}")
    return out


def cmd_log(args):
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    if not args.auto:
        car = _connect_or_exit(args)
        print("  Ctrl-C to stop")
        _log_session(car, args, stop)
        car.close()
        return
    # --auto: wait for the car, log each ignition cycle to its own file, repeat.
    print("auto mode: waiting for the car (ignition on)...", flush=True)
    while not stop:
        car = ReadOnlyCar(_port(args))
        if car.connected:
            _log_session(car, args, stop)
            print("waiting for the car again...", flush=True)
        car.close()
        for _ in range(args.retry_every):
            if stop:
                break
            time.sleep(1)


# --------------------------------------------------------------------------
# summarize / analyze
# --------------------------------------------------------------------------

def _f(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def open_log(path):
    path = Path(path)
    return gzip.open(path, "rt", newline="") if path.suffix == ".gz" else path.open(newline="")


def summarize(path, table_rows=120):
    with open_log(path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return f"# {path}\n\nempty log\n"
    cols = rows[0].keys()
    num = {c: [_f(r[c]) for r in rows] for c in cols if c not in
           ("timestamp", "stored_dtcs", "pending_dtcs")}
    t = num["elapsed_s"]
    dur = t[-1] - t[0]
    out = [f"# OBD-II log summary: {Path(path).name}", "",
           f"- start: {rows[0]['timestamp']}", f"- duration: {dur / 60:.1f} min, {len(rows)} samples",
           ""]

    out += ["## Ranges", "", "| field | first | min | max | last |", "|---|---|---|---|---|"]
    for c, vals in num.items():
        v = [x for x in vals if x is not None]
        if c == "elapsed_s" or not v:
            continue
        out.append(f"| {c} | {v[0]:g} | {min(v):g} | {max(v):g} | {v[-1]:g} |")

    ect = num.get("coolant_temp_c")
    if ect and any(x is not None for x in ect):
        out += ["", "## Coolant temperature", ""]
        first = next(x for x in ect if x is not None)
        out.append(f"- start {first:g} C ({'cold start' if first < 40 else 'warm start'})")
        for thresh in (80, 90, 100, 105, 110):
            idx = next((i for i, x in enumerate(ect) if x is not None and x >= thresh), None)
            if idx is not None:
                above = sum(t[i + 1] - t[i] for i in range(len(t) - 1)
                            if ect[i] is not None and ect[i] >= thresh)
                out.append(f"- reached {thresh} C at {t[idx] / 60:.1f} min; {above / 60:.1f} min at or above")
        rates = [(ect[i + 30] - ect[i]) / (t[i + 30] - t[i]) * 60
                 for i in range(len(ect) - 30)
                 if ect[i] is not None and ect[i + 30] is not None and t[i + 30] > t[i]]
        if rates:
            out.append(f"- fastest rise over ~30 samples: {max(rates):.1f} C/min")
            out.append(f"- fastest fall over ~30 samples: {min(rates):.1f} C/min")

    if "cht_raw" in cols:
        out += ["", "## Ford CHT (PID 22 1624, scale UNVERIFIED)", ""]
        pairs = [(r, e, i) for r, e, i in zip(num["cht_raw"], ect or [], num.get("intake_air_temp_c", []))
                 if r is not None]
        if pairs:
            r0, e0, i0 = pairs[0]
            out.append(f"- first sample: raw={r0:g} (candidate {cht_candidate_f(r0):.0f} F), "
                       f"ECT={e0} C, IAT={i0} C")
            r1, e1, _ = pairs[-1]
            out.append(f"- last sample: raw={r1:g} (candidate {cht_candidate_f(r1):.0f} F), ECT={e1} C")
            out.append("- On a cold-soaked start, a correct CHT scale reads close to ECT and IAT.")

    events = []
    prev = {"stored_dtcs": "", "pending_dtcs": ""}
    for r in rows:
        for k in prev:
            if r[k] != prev[k]:
                events.append(f"- {r['timestamp']} ({_f(r['elapsed_s']) / 60:.1f} min): "
                              f"{k} -> '{r[k] or 'none'}'")
                prev[k] = r[k]
    out += ["", "## Trouble codes during the log", ""] + (events or ["- none"])

    if table_rows <= 0:
        return "\n".join(out) + "\n"
    step = max(1, len(rows) // table_rows)
    keep = [c for c in cols if c not in ("timestamp", "stored_dtcs", "pending_dtcs")]
    out += ["", f"## Samples (every {step}th row)", "", "| " + " | ".join(keep) + " |",
            "|" + "---|" * len(keep)]
    for r in rows[::step]:
        out.append("| " + " | ".join(r[c] for c in keep) + " |")
    return "\n".join(out) + "\n"


def _all_logs():
    roots = [p / "logs" for p in records.vehicles()] + [
        (records.records_root() or FALLBACK_DIR) / "unassigned" / "logs"]
    return [f for r in roots for pat in ("*.csv", "*.csv.gz") for f in r.rglob(pat)]


def _latest_csv():
    logs = sorted(_all_logs(), key=lambda f: records.file_time(f) or dt.datetime.min)
    if not logs:
        sys.exit("no logs found (is `records` set in ~/.config/obd2log/config.toml?)")
    return logs[-1]


def _vehicle_notes(log):
    """vehicle.md for a log at vehicles/<slug>/logs/<YYYY>/<file>."""
    v = Path(log).resolve().parents[2] / "vehicle.md"
    return v.read_text() if v.exists() else ""


def cmd_summarize(args):
    print(summarize(args.csv or _latest_csv(), args.rows), end="")


def cmd_analyze(args):
    paths = args.csv or [_latest_csv()]
    notes = Path(args.vehicle).read_text() if args.vehicle else _vehicle_notes(paths[0])
    body = "\n\n".join(summarize(p, args.rows) for p in paths)
    prompt = (
        "You are reviewing OBD-II logs from my car. Vehicle notes:\n\n" + notes +
        "\n\nLogs follow. Report: cold-start warm-up behaviour, any temperature spikes or "
        "oscillation (possible air pocket / low coolant / thermostat), how close coolant and "
        "head temperature came to overheat, fuel-trim oddities, and any trouble codes that "
        "appeared. Say what the data cannot show. Be concrete and brief.\n\n" + body)
    if args.print_prompt:
        print(prompt)
        return
    subprocess.run(["claude", "-p", prompt], check=True)


# --------------------------------------------------------------------------
# vehicle / case scaffolding
# --------------------------------------------------------------------------

def _root_or_exit():
    root = records.records_root()
    if not root:
        sys.exit(f"set `records = \"<path>\"` in {records.CONFIG} first")
    return root


def cmd_vehicle_add(args):
    root = _root_or_exit()
    d = root / "vehicles" / args.slug
    if (d / "vehicle.md").exists():
        sys.exit(f"{d} already exists")
    car = _connect_or_exit(args)
    supported = [col for col, name, _ in STANDARD_FIELDS if car.supports(name)]
    protocol = car.conn.protocol_name()
    vin = car.vin
    car.close()
    if not vin:
        sys.exit("the car did not report a VIN; can't create a matchable profile")
    d.mkdir(parents=True)
    (d / "vehicle.md").write_text(f"""---
title: {args.title or args.slug}
vin_masked: {_mask_vin(vin)}
vin_sha256: {records.vin_hash(vin)}
protocol: {protocol}
added: {dt.date.today()}
---

# {args.title or args.slug}

Background for Claude: model, engine, mileage, known issues, quirks,
baselines. Append dated findings.

- Standard fields supported: {", ".join(supported)}
""")
    (d / "maintenance.md").write_text(f"# Maintenance log: {args.title or args.slug}\n\n"
                                      "| date | miles | what |\n|---|---|---|\n")
    print(f"created {d}. Data from this car now lands here.")
    un = root / "unassigned"
    if un.exists() and any(un.rglob("*")):
        print(f"note: {un} has data from unrecognised cars; move what belongs to this one.")


def cmd_case_new(args):
    root = _root_or_exit()
    v = root / "vehicles" / args.vehicle
    if not (v / "vehicle.md").exists():
        sys.exit(f"no vehicle {args.vehicle!r} in {root / 'vehicles'}")
    d = v / "cases" / f"{dt.date.today()}_{args.slug}"
    d.mkdir(parents=True)
    (d / "photos").mkdir()
    (d / "case.md").write_text(f"""---
title: {args.title or args.slug}
opened: {dt.date.today()}
closed:
status: open
include:
exclude:
---

## Summary

## Evidence

## Timeline

| when | what |
|---|---|

## Plan: what to check, when, and when you're done

| # | check | how | when / how often | done when |
|---|---|---|---|---|

### When this case is closed
""")
    print(f"created {d}/case.md")


# --------------------------------------------------------------------------
# inspect (used-car check) and OBDb signalsets
# --------------------------------------------------------------------------

def _signalsets(args):
    """[(signalset, source)] and [error text] for --make/--model/--make-wide/--signalset."""
    from . import obdb
    wanted = []
    if args.make:
        if args.model:
            wanted.append((args.make, args.model))
        if args.make_wide or not args.model:
            wanted.append((args.make, None))
    specs, errors = [], []
    for path in args.signalset or []:
        try:
            specs.append(obdb.load(path))
        except Exception as e:  # noqa: BLE001
            errors.append(f"signalset {path}: {e}")
    for make, model in wanted:
        name = obdb.repo_name(make, model)
        path = obdb.cached(make, model)
        if not path and not args.no_fetch:
            try:
                path = obdb.fetch(make, model)
                print(f"fetched OBDb/{path.parent.name}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"OBDb {name}: could not fetch ({e}). Run `obd2log pids fetch {make}"
                              f"{' ' + model if model else ''}` while online.")
                continue
        if not path:
            errors.append(f"OBDb {name}: not in the cache and --no-fetch given")
            continue
        specs.append(obdb.load(path))
    return specs, errors


def cmd_inspect(args):
    from . import inspection
    specs, errors = _signalsets(args)   # fetch before connecting: the network may be gone at the car
    car = _connect_or_exit(args)
    print("reading everything the car will report; this takes a minute or two...")
    try:
        data = inspection.run(car, specs, args.year, args.all_years, errors)
    finally:
        car.close()
    md = inspection.render(data, show_vin=car.vin if args.show_vin else None)
    print(md)
    if args.save != "-":
        files = inspection.save(data, inspection.render(data), args.save, pdf=not args.no_pdf)
        print("saved " + ", ".join(str(f) for f in files))


def cmd_pids(args):
    from . import obdb
    if args.pcmd == "fetch":
        path = obdb.fetch(args.make, args.model)
        sigset, src = obdb.load(path)
        n = sum(len(c["signals"]) for c in sigset["commands"])
        print(f"{src['repo']} @ {src['commit'][:12]}: {len(sigset['commands'])} requests, {n} signals -> {path}")
    elif args.pcmd == "search":
        print("\n".join(obdb.search(args.text)) or "no matches")
    elif args.pcmd == "list":
        found = sorted(obdb.CACHE.glob("*/source.json")) if obdb.CACHE.exists() else []
        for f in found:
            src = json.loads(f.read_text())
            print(f"{src['repo']:32} {src['commit'][:12]}  fetched {src['fetched']}")
        if not found:
            print(f"nothing cached in {obdb.CACHE}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port (default: auto-detect, e.g. /dev/ttyUSB0)")
    sub = p.add_subparsers(dest="cmd", required=True)
    save_help = "snapshot path (file or dir); default: the vehicle's snapshots/<YYYY>/; '-' = don't save"
    s = sub.add_parser("check")
    s.add_argument("--save", help=save_help)
    s.add_argument("--show-vin", action="store_true", help="print/save the full VIN (masked by default)")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("codes")
    s.add_argument("--save", help=save_help)
    s.set_defaults(fn=cmd_codes)
    s = sub.add_parser("monitors", help="mode 06 self-test results (misfire counts per cylinder, etc.)")
    s.add_argument("--save", help=save_help)
    s.set_defaults(fn=cmd_monitors)
    s = sub.add_parser("inspect", help="used-car check: read everything (standard + --make/--model) into a report")
    s.add_argument("--make", help="e.g. ford: adds OBDb manufacturer-specific requests")
    s.add_argument("--model", help="e.g. escape: uses the OBDb <Make>-<Model> signalset (else the make-wide one)")
    s.add_argument("--year", type=int, help="model year for OBDb year filters (default: from the VIN)")
    s.add_argument("--make-wide", action="store_true",
                   help="with --model, also send the make-wide OBDb set (every model's requests, all debug)")
    s.add_argument("--signalset", action="append", help="a local OBDb-format JSON file (repeatable)")
    s.add_argument("--all-years", action="store_true", help="also send requests OBDb lists for other model years")
    s.add_argument("--no-fetch", action="store_true", help="only use cached OBDb signalsets")
    s.add_argument("--show-vin", action="store_true", help="print the full VIN on screen (files stay masked)")
    s.add_argument("--save", help="report directory; default: the vehicle's inspections/<YYYY>/<stamp>_inspect/; '-' = don't save")
    s.add_argument("--no-pdf", action="store_true", help="skip report.pdf")
    s.set_defaults(fn=cmd_inspect)
    s = sub.add_parser("pids", help="OBDb manufacturer-specific signalsets (github.com/OBDb)")
    ps = s.add_subparsers(dest="pcmd", required=True)
    s = ps.add_parser("fetch", help="download a signalset into the cache (do this while online)")
    s.add_argument("make")
    s.add_argument("model", nargs="?")
    s.set_defaults(fn=cmd_pids)
    s = ps.add_parser("search", help="find OBDb repos, e.g. `pids search escape`")
    s.add_argument("text")
    s.set_defaults(fn=cmd_pids)
    s = ps.add_parser("list", help="show cached signalsets")
    s.set_defaults(fn=cmd_pids)
    s = sub.add_parser("log")
    s.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    s.add_argument("--dtc-every", type=float, default=30.0, help="seconds between code checks")
    s.add_argument("--ford-cht", action="store_true", help="also log Ford CHT (22 1624)")
    s.add_argument("--basic", action="store_true",
                   help="only the core fields (default logs every supported mode 01 reading + misfire counts)")
    s.add_argument("--extra-every", type=float, default=10.0,
                   help="seconds between reads of the non-core readings (they slow a row to ~3 s)")
    s.add_argument("--out-dir", help="directory for the log (default: the vehicle's logs/<YYYY>/)")
    s.add_argument("--no-gzip", action="store_true", help="keep the finished log as plain .csv")
    s.add_argument("--out", help="exact CSV path (single session only)")
    s.add_argument("--auto", action="store_true",
                   help="wait for the car, log every ignition cycle to its own file (for the autostart service)")
    s.add_argument("--retry-every", type=int, default=10, help="--auto: seconds between connection attempts")
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("summarize")
    s.add_argument("csv", nargs="?")
    s.add_argument("--rows", type=int, default=120)
    s.set_defaults(fn=cmd_summarize)
    s = sub.add_parser("analyze")
    s.add_argument("csv", nargs="*")
    s.add_argument("--rows", type=int, default=120)
    s.add_argument("--vehicle", help="vehicle notes file (default: the log's vehicle.md)")
    s.add_argument("--print-prompt", action="store_true", help="print the prompt instead of running claude")
    s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("vehicle", help="add a vehicle profile for the connected car")
    vs = s.add_subparsers(dest="vcmd", required=True)
    s = vs.add_parser("add")
    s.add_argument("slug", help="folder name, e.g. 2014-ford-escape")
    s.add_argument("--title", help="e.g. '2014 Ford Escape Titanium 1.6L'")
    s.set_defaults(fn=cmd_vehicle_add)
    s = sub.add_parser("case", help="start a case for a vehicle")
    cs = s.add_subparsers(dest="ccmd", required=True)
    s = cs.add_parser("new")
    s.add_argument("vehicle", help="vehicle folder name")
    s.add_argument("slug", help="short name, e.g. p0300-misfire")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_case_new)
    s = sub.add_parser("report", help="build a PDF report for a case folder")
    s.add_argument("case", help="vehicles/<vehicle>/cases/<case>")
    s.set_defaults(fn=lambda a: _report().build(Path(a.case)))
    s = sub.add_parser("upload", help="copy the records repo to Google Drive via rclone")
    s.add_argument("path", nargs="?", help="sub-path to upload (default: the whole records repo)")
    s.add_argument("--remote", help="rclone remote:path (default: rclone_remote in config)")
    s.set_defaults(fn=lambda a: _report().upload(a.path, a.remote))
    args = p.parse_args()
    args.fn(args)


def _report():
    from . import report  # matplotlib etc. only when needed
    return report


if __name__ == "__main__":
    main()
