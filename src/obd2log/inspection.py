"""`obd2log inspect`: one read-only pass over everything the car will tell us, for a used-car check.

Standard OBD-II first (identity, check-engine status and readiness monitors,
trouble codes, freeze frame, the "since codes were cleared" counters, live
readings, mode 06 self-tests, every other supported mode 01 reading), then,
with --make/--model, every manufacturer-specific request in the matching OBDb
signalset.

Every request is recorded with its outcome: ok, no response, negative
response (with the module's reason), not decodable, error, or not sent (with
the reason). The report lists all of them; nothing tried is left out.

Findings are heuristics for a buyer, not diagnoses; the report says so.
"""

import datetime as dt
import json
from pathlib import Path

from . import cli, obdb, records

# (group, python-OBD or custom command, label, unit to convert to)
STANDARD = [
    ("identity", "CALIBRATION_ID", "Calibration ID (engine software)", None),
    ("identity", "CVN", "Calibration verification number", None),
    ("identity", "ECU_NAME", "ECU name", None),
    ("identity", "OBD_COMPLIANCE", "OBD standard", None),
    ("identity", "FUEL_TYPE", "Fuel type", None),
    ("identity", "ELM_VERSION", "Adapter", None),
    ("identity", "ELM_VOLTAGE", "Battery voltage at the OBD port (adapter)", "volt"),
    ("status", "STATUS", "Check-engine light and readiness monitors (since codes cleared)", None),
    ("status", "STATUS_DRIVE_CYCLE", "Monitors this drive cycle", None),
    ("codes", "GET_DTC", "Stored codes (03)", None),
    ("codes", "GET_CURRENT_DTC", "Pending codes (07)", None),
    ("codes", "PERMANENT_DTC", "Permanent codes (0A)", None),
    ("freeze", "FF_DTC", "Freeze frame: code that stored it", None),
    ("freeze", "FF_COOLANT_TEMP_C", "Freeze frame: coolant °C", None),
    ("freeze", "FF_RPM", "Freeze frame: rpm", None),
    ("freeze", "FF_SPEED_KPH", "Freeze frame: speed km/h", None),
    ("freeze", "FF_ENGINE_LOAD_PCT", "Freeze frame: load %", None),
    ("freeze", "FF_INTAKE_TEMP_C", "Freeze frame: intake air °C", None),
    ("history", "DISTANCE_SINCE_DTC_CLEAR", "Distance since codes cleared", "km"),
    ("history", "WARMUPS_SINCE_DTC_CLEAR", "Warm-ups since codes cleared", None),
    ("history", "TIME_SINCE_DTC_CLEARED", "Engine run time since codes cleared", "minute"),
    ("history", "DISTANCE_W_MIL", "Distance driven with check-engine light on", "km"),
    ("history", "RUN_TIME_MIL", "Engine run time with check-engine light on", "minute"),
    ("history", "ODOMETER", "Odometer (01 A6, mostly 2019+ cars)", None),
    ("history", "RUN_TIME", "Engine run time this start", "second"),
    ("live", "RPM", "Engine speed", None),
    ("live", "SPEED", "Vehicle speed", "kph"),
    ("live", "COOLANT_TEMP", "Coolant temperature", "degC"),
    ("live", "INTAKE_TEMP", "Intake air temperature", "degC"),
    ("live", "AMBIANT_AIR_TEMP", "Ambient air temperature", "degC"),
    ("live", "OIL_TEMP", "Oil temperature", "degC"),
    ("live", "CONTROL_MODULE_VOLTAGE", "Control module voltage", "volt"),
    ("live", "SHORT_FUEL_TRIM_1", "Short-term fuel trim, bank 1", None),
    ("live", "LONG_FUEL_TRIM_1", "Long-term fuel trim, bank 1", None),
    ("live", "SHORT_FUEL_TRIM_2", "Short-term fuel trim, bank 2", None),
    ("live", "LONG_FUEL_TRIM_2", "Long-term fuel trim, bank 2", None),
    ("live", "ENGINE_LOAD", "Engine load", None),
    ("live", "FUEL_LEVEL", "Fuel level", None),
    ("live", "BAROMETRIC_PRESSURE", "Barometric pressure", "kilopascal"),
    ("live", "HYBRID_BATTERY_REMAINING", "Hybrid battery charge", None),
]
UNIT_LABEL = {"volt": "V", "km": "km", "minute": "min", "second": "s", "kph": "km/h", "degC": "°C",
              "kilopascal": "kPa"}
NO_RAW = {"VIN17"}  # raw VIN replies would put the full VIN in the report


# --------------------------------------------------------------------------
# running the requests
# --------------------------------------------------------------------------

def _request_text(cmd):
    c = cmd.command.decode()
    return c if c.startswith("AT") else f"{c[:2]} {c[2:]}".strip()


def _raw_lines(r):
    return [f.raw for m in r.messages for f in m.frames][:8]


def _negative(r):
    for m in r.messages:
        d = bytes(m.data)
        if len(d) >= 3 and d[0] == 0x7F:
            return f"7F {d[1]:02X} {d[2]:02X}: {obdb.NRC.get(d[2], 'unknown code')}"
    return None


def _standard_value(name, v, unit):
    if name in ("STATUS", "STATUS_DRIVE_CYCLE"):
        monitors = {}
        for k, t in vars(v).items():
            if isinstance(k, str) and hasattr(t, "available"):
                monitors[k.lower()] = ("complete" if t.complete else "incomplete") if t.available else "not on this car"
        return {"mil_on": bool(v.MIL), "dtc_count": v.DTC_count,
                "ignition": str(getattr(v, "ignition_type", "")), "monitors": monitors}
    if name in ("GET_DTC", "GET_CURRENT_DTC", "PERMANENT_DTC"):
        return [{"code": c, "description": d or ""} for c, d in v]
    if unit:
        return cli._mag(v, unit)
    return cli._val(v)


def attempt(car, group, name, label, unit=None):
    """Send one standard request; return a record whatever happens."""
    cmd = car.custom.get(name) or car.obd.commands[name]
    rec = {"group": group, "name": name, "label": label, "request": _request_text(cmd),
           "header": "7E0" if group != "identity" else "", "status": None, "value": None,
           "unit": UNIT_LABEL.get(unit, ""), "detail": "", "raw": []}
    if cmd.command[:2] == b"01" and name in car.obd.commands and not car.conn.supports(cmd):
        rec["detail"] = "car's supported-PID list says no; asked anyway. "
    try:
        r = car.ask(cmd, force=True)
    except Exception as e:  # noqa: BLE001 - every failure goes in the report
        rec.update(status="error", detail=rec["detail"] + f"{type(e).__name__}: {e}")
        return rec
    if name not in NO_RAW:
        rec["raw"] = _raw_lines(r)
    if not r.messages:
        rec.update(status="no response", detail=rec["detail"] + "no reply (NO DATA / timeout)")
    elif r.value is None:
        neg = _negative(r)
        rec.update(status="negative response" if neg else "not decodable",
                   detail=rec["detail"] + (neg or "reply received but python-OBD could not decode it"))
    else:
        try:
            rec.update(status="ok", value=_standard_value(name, r.value, unit))
        except Exception as e:  # noqa: BLE001
            rec.update(status="not decodable", detail=rec["detail"] + f"{type(e).__name__}: {e}")
    return rec


def _monitor_records(car):
    out = []
    cmds = sorted((c for c in car.conn.supported_commands if c.mode == 6 and not c.name.startswith("MIDS")),
                  key=lambda c: c.command)
    for cmd in cmds:
        rec = {"group": "mode06", "name": cmd.name, "label": cmd.desc, "request": _request_text(cmd),
               "status": None, "value": None, "detail": "", "raw": []}
        try:
            r = car.ask(cmd, force=True)
            rec["raw"] = _raw_lines(r)
            tests = cli._monitor_tests(r.value) if r.value is not None else []
            if tests:
                rec.update(status="ok", value=tests)
            else:
                rec.update(status="no response" if not r.messages else "not decodable",
                           detail=_negative(r) or ("no reply" if not r.messages else "no test results in reply"))
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", detail=f"{type(e).__name__}: {e}")
        out.append(rec)
    return out


def make_specific(car, spec, year, all_years=False):
    """Send every request in an OBDb signalset; one record per signal (or per request when nothing decodes)."""
    signalset, source = spec
    recs = []
    for req in obdb.plan(signalset, year, cli.READ_ONLY_MODES, all_years, source):
        base = {"group": "make", "source": req["source"], "header": req["header"], "rax": req["rax"],
                "request": f"{req['service']} {req['pid']}", "debug": req["debug"], "raw": [], "reply_from": []}
        sigs = req["signals"]
        if req["skip"]:
            recs += [{**base, "label": s["name"], "path": s.get("path", ""), "status": "not sent",
                      "value": None, "unit": obdb.unit(s), "detail": req["skip"]} for s in sigs]
            continue
        try:
            lines = car.request(req["header"], req["service"], req["pid"])
        except Exception as e:  # noqa: BLE001
            recs += [{**base, "label": s["name"], "path": s.get("path", ""), "status": "error", "value": None,
                      "unit": obdb.unit(s), "detail": f"{type(e).__name__}: {e}"} for s in sigs]
            continue
        status, detail, payload, ids = obdb.parse_reply(lines, req["service"], req["pid"])
        base.update(raw=[f"{i} {d.hex(' ').upper()}" for i, d in lines][:6], reply_from=ids)
        if req["rax"] and ids and req["rax"] not in ids:
            detail = (detail + "; " if detail else "") + f"answered from {', '.join(ids)}, OBDb expects {req['rax']}"
        for s in sigs:
            rec = {**base, "label": s["name"], "path": s.get("path", ""), "unit": obdb.unit(s),
                   "metric": s.get("suggestedMetric"), "status": status, "value": None, "detail": detail}
            if payload is not None:
                try:
                    v, note = obdb.decode_signal(s, payload)
                    rec.update(value=v, detail="; ".join(x for x in (note, detail) if x),
                               status="ok" if v is not None else "not decodable")
                except Exception as e:  # noqa: BLE001
                    rec.update(status="not decodable", detail=f"{e}")
            recs.append(rec)
    return recs


def model_year(vin, today=None):
    """Model year from VIN position 10 (the most recent year it can mean, up to next year)."""
    codes = "ABCDEFGHJKLMNPRSTVWXY123456789"
    if not vin or len(vin) != 17 or vin[9] not in codes:
        return None
    y = 1980 + codes.index(vin[9])
    limit = (today or dt.date.today()).year + 1
    while y + 30 <= limit:
        y += 30
    return y


def run(car, specs=(), year=None, all_years=False, spec_errors=()):
    """The whole inspection as a dict (see render())."""
    vin = car.vin
    data = {"kind": "inspect", "taken": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "obd2log live read", "vin_masked": cli._mask_vin(vin), "model_year": year or model_year(vin),
            "year_from": "--year" if year else "VIN position 10",
            "port": car.conn.port_name(), "protocol": f"{car.conn.protocol_name()} ({car.conn.protocol_id()})",
            "_vin": vin, "signalsets": [s[1] for s in specs], "signalset_errors": list(spec_errors), "attempts": []}
    if not vin:
        data["year_from"] = "--year" if year else "unknown (no VIN)"
    att = data["attempts"]
    att.append({"group": "identity", "name": "VIN17", "label": "VIN (masked)", "request": "09 02",
                "status": "ok" if vin else "no response", "value": cli._mask_vin(vin),
                "detail": "" if vin else "no VIN reply", "raw": []})
    for group, name, label, unit in STANDARD:
        att.append(attempt(car, group, name, label, unit))
    att += _monitor_records(car)
    done = {n for _, n, _, _ in STANDARD}
    for cmd in sorted(car.conn.supported_commands, key=lambda c: c.command):
        if cmd.mode == 1 and cmd.name not in done and cmd.name not in cli.NOT_READINGS:
            att.append(attempt(car, "other", cmd.name, cmd.desc))
    data["not_requested"] = sorted(
        c.name for c in car.obd.commands[1]
        if c and not c.name.startswith("PIDS_") and c.name not in done and not car.conn.supports(c))
    for spec in specs:
        att += make_specific(car, spec, data["model_year"], all_years)
    data["findings"] = findings(data)
    return data


# --------------------------------------------------------------------------
# findings (buyer heuristics)
# --------------------------------------------------------------------------

def _get(data, name):
    for a in data["attempts"]:
        if a.get("name") == name:
            return a
    return {}


def _ok(data, name):
    a = _get(data, name)
    return a.get("value") if a.get("status") == "ok" else None


def findings(data):
    """[(level, text)]; level is 'red flag', 'check' or 'info'."""
    out = []
    add = lambda lvl, txt: out.append((lvl, txt))
    st = _ok(data, "STATUS")
    if st is None:
        add("check", "Couldn't read the check-engine status or readiness monitors (01 01): see the table.")
    else:
        if st["mil_on"]:
            add("red flag", "Check-engine light is ON.")
        inc = [k for k, v in st["monitors"].items() if v == "incomplete"]
        if len(inc) >= 2:
            add("red flag", f"{len(inc)} readiness monitors incomplete ({', '.join(inc)}). Typical right after "
                "codes are cleared or the battery is disconnected; they need days of mixed driving to finish. "
                "Ask when and why. Most states won't pass an emissions test like this.")
        elif inc:
            add("check", f"1 readiness monitor incomplete ({inc[0]}). EVAP and catalyst are often the last to "
                "finish; one alone is common, but ask.")
        else:
            add("info", "All readiness monitors the car has are complete.")
    codes = {k: _ok(data, k) for k in ("GET_DTC", "GET_CURRENT_DTC", "PERMANENT_DTC")}
    for k, label in (("GET_DTC", "Stored"), ("GET_CURRENT_DTC", "Pending"), ("PERMANENT_DTC", "Permanent")):
        if codes[k]:
            add("red flag", f"{label} code(s): " + "; ".join(
                f"{c['code']} {c['description'] or '(manufacturer-specific)'}" for c in codes[k]) + ".")
    if codes["PERMANENT_DTC"] and not codes["GET_DTC"]:
        add("red flag", "Permanent code(s) with no stored codes: codes were cleared, but the fault hasn't been "
            "proven fixed by the car's own test.")

    d, w, t = (_ok(data, n) for n in ("DISTANCE_SINCE_DTC_CLEAR", "WARMUPS_SINCE_DTC_CLEAR", "TIME_SINCE_DTC_CLEARED"))
    recent = []
    if d is not None and d < 300:
        recent.append(f"{d:g} km")
    if w is not None and w < 20:
        recent.append(f"{w} warm-ups")
    if t is not None and t < 600:
        recent.append(f"{t:g} min of engine running")
    if recent:
        add("red flag", "Codes were cleared recently: " + ", ".join(recent) + " ago (or the battery was "
            "disconnected). Ask why; a cleared code often comes back within a week.")
    elif any(x is not None for x in (d, w, t)):
        add("info", "Codes last cleared " + ", ".join(
            s for s in (d is not None and f"{d:g} km", w is not None and f"{w} warm-ups" + (" (counter max)" if w == 255 else ""),
                        t is not None and f"{t:g} min engine time") if s) + " ago.")
    else:
        add("check", "The car didn't report when codes were last cleared (01 30/31/4E); can't rule out a recent clear.")
    mil_km = _ok(data, "DISTANCE_W_MIL")
    if mil_km:
        add("red flag" if not (st and st["mil_on"]) else "check",
            f"Driven {mil_km:g} km with the check-engine light on" +
            (" and the light is off now: a fault was present recently." if not (st and st["mil_on"]) else "."))

    rpm, cool = _ok(data, "RPM"), _ok(data, "COOLANT_TEMP")
    air = [x for x in (_ok(data, "AMBIANT_AIR_TEMP"), _ok(data, "INTAKE_TEMP")) if x is not None]
    running = bool(rpm and rpm > 300)
    if not running and cool is not None and air and cool - min(air) > 15:
        add("check", f"Engine off but coolant is {cool:g} °C vs {min(air):g} °C air: the engine ran recently. "
            "If this was your first look, it was warmed up before you arrived, which hides cold-start problems.")
    volts = _ok(data, "CONTROL_MODULE_VOLTAGE") or _ok(data, "ELM_VOLTAGE")
    if volts is not None:
        if not running and volts < 12.2:
            add("check", f"{volts:g} V with the engine off: battery weak or discharged.")
        if running and volts < 13.2:
            add("check", f"{volts:g} V with the engine running: charging looks low (alternator or wiring).")
        if running and volts > 15.2:
            add("check", f"{volts:g} V with the engine running: charging looks high (regulator).")
    for n in ("LONG_FUEL_TRIM_1", "LONG_FUEL_TRIM_2"):
        v = _ok(data, n)
        if v is not None and abs(v) > 10 and running:
            add("check", f"{n.lower()} {v:+g} %: the engine is correcting a lot (vacuum leak, fuel delivery, "
                "MAF sensor or injectors). Recheck when fully warm.")
    for a in data["attempts"]:
        if a["group"] == "mode06" and a["status"] == "ok":
            for t in a["value"]:
                if not t["passed"]:
                    add("red flag", f"Mode 06 self-test failed: {a['name'].lower()} TID {t['tid']} {t['name']} = "
                        f"{t['value']} {t['unit']} (limits {t['min']}..{t['max']}).")
                elif a["name"].startswith("MONITOR_MISFIRE_CYLINDER") and t["tid"] in ("0B", "0C") and t["value"]:
                    add("check", f"Misfires recorded on cylinder {a['name'].rsplit('_', 1)[1]}: "
                        f"{t['value']} (TID {t['tid']}).")

    odo = [(a.get("label") or a["name"], a.get("source") or "standard", a["value"]) for a in data["attempts"]
           if a["status"] == "ok" and isinstance(a["value"], (int, float))
           and (a.get("name") == "ODOMETER" or a.get("metric") == "odometer" or "odometer" in (a.get("label") or "").lower())]
    if odo:
        add("info", "Odometer readings: " + "; ".join(f"{l} = {v:g} km ({v / 1.609344:,.0f} mi)" for l, _, v in odo)
            + ". Compare with the dashboard.")
        vals = [v for *_, v in odo]
        if max(vals) - min(vals) > 50:
            add("red flag", f"Modules disagree on the odometer by {max(vals) - min(vals):g} km: possible "
                "instrument-cluster swap or rollback (or a wrongly decoded community PID; check the raw replies).")
    else:
        add("info", "No module reported the odometer; check mileage against the title and a history report.")

    mk = [a for a in data["attempts"] if a["group"] == "make"]
    if mk:
        by = {}
        for a in mk:
            by[a["status"]] = by.get(a["status"], 0) + 1
        add("info", "Manufacturer-specific (OBDb) signals: " + ", ".join(f"{n} {s}" for s, n in sorted(by.items())) + ".")
    for e in data.get("signalset_errors", []):
        add("check", f"Manufacturer-specific signals not read: {e}")
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        v = f"{v:g}"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _show(a):
    v = a["value"]
    if a["name"] in ("GET_DTC", "GET_CURRENT_DTC", "PERMANENT_DTC") and v is not None:
        return ", ".join(f"{c['code']} {c['description']}".strip() for c in v) or "none"
    if isinstance(v, dict):
        return f"light {'ON' if v['mil_on'] else 'off'}, {v['dtc_count']} codes"
    return f"{_cell(v)} {a.get('unit') or ''}".strip()


def render(data, show_vin=None):
    """Markdown report. Every attempt appears with its status."""
    A = data["attempts"]
    g = lambda grp: [a for a in A if a["group"] == grp]
    md = [f"# Vehicle inspection, {data['taken'][:16].replace('T', ' ')}", ""]
    md.append(f"- **VIN:** {show_vin or data['vin_masked'] or 'not reported'}"
              + ("" if show_vin else " (masked; `--show-vin` prints it on screen only)") + ". "
              "Compare with the dash plate, door sticker and title.")
    md.append(f"- **Model year:** {data['model_year'] or 'unknown'} (from {data['year_from']})")
    rpm = _ok(data, "RPM")
    md.append(f"- **Engine:** {'running, ' + _cell(rpm) + ' rpm' if rpm and rpm > 300 else 'off (ignition on)' if rpm is not None else 'unknown'}"
              f" · link {data['protocol']} on {data['port']}")
    for s in data["signalsets"]:
        md.append(f"- **Manufacturer signals:** {s['repo']}" + (f" @ `{s['commit'][:12]}`" if s.get("commit") else "")
                  + (f" ({s['url']})" if s.get("url") else "") + ", community data, CC-BY-SA-4.0, unverified on this car")
    tot = {}
    for a in A:
        tot[a["status"]] = tot.get(a["status"], 0) + 1
    md.append(f"- **Requests:** {len(A)} readings tried: " + ", ".join(f"{n} {s}" for s, n in sorted(tot.items())))

    md += ["", "## Findings", "", "Heuristics for a buyer, not a diagnosis. Thresholds are rules of thumb.", ""]
    order = {"red flag": 0, "check": 1, "info": 2}
    md += [f"- **{lvl}:** {txt}" for lvl, txt in sorted(data["findings"], key=lambda f: order[f[0]])]
    md += ["", "OBD can't see: mileage on most pre-2019 cars, ABS/airbag/body faults (generic OBD covers "
           "engine and emissions), accident or flood damage, rust, brakes, suspension, leaks, or how the "
           "transmission shifts. Get a pre-purchase inspection and a history report."]

    st = _get(data, "STATUS")
    md += ["", "## Readiness monitors (01 01)", ""]
    if st.get("status") == "ok":
        cyc = _ok(data, "STATUS_DRIVE_CYCLE") or {"monitors": {}}
        md.append(_table(["monitor", "since codes cleared", "this drive cycle (01 41)"],
                         [(k, v, cyc["monitors"].get(k, "not read")) for k, v in st["value"]["monitors"].items()]))
        md.append(f"\nCheck-engine light: **{'ON' if st['value']['mil_on'] else 'off'}**, "
                  f"{st['value']['dtc_count']} codes reported, ignition type {st['value']['ignition']}.")
    else:
        md.append(f"Not read: {st.get('status')}: {st.get('detail')}")

    def section(title, rows, note=None):
        nonlocal md
        md += ["", f"## {title}", ""] + ([note, ""] if note else [])
        md.append(_table(["reading", "request", "status", "value", "detail"],
                         [(a["label"], a["request"], a["status"], _show(a) if a["status"] == "ok" else "", a["detail"])
                          for a in rows]) if rows else "None.")

    section("Trouble codes", g("codes"))
    section("Freeze frame (conditions when a code was stored)", g("freeze"))
    section("History counters", g("history"),
            "Distance and warm-ups since codes were cleared reset when codes are cleared or the battery is "
            "disconnected. The warm-up counter stops at 255 and distance at 65,535 km.")
    section("Live readings", g("live"))
    section("Identity", g("identity"))

    md += ["", "## Mode 06 self-tests", ""]
    rows = []
    for a in g("mode06"):
        if a["status"] == "ok":
            rows += [(a["name"].lower().removeprefix("monitor_"), t["tid"], t["name"], f"{t['value']} {t['unit']}",
                      f"{t['min']} .. {t['max']}", "pass" if t["passed"] else "**FAIL**") for t in a["value"]]
        else:
            rows.append((a["name"].lower().removeprefix("monitor_"), "", a["status"], "", "", a["detail"]))
    md.append(_table(["monitor", "TID", "test", "value", "limits", "result"], rows) if rows
              else "The car listed no mode 06 monitors.")
    md.append("\nMisfire TIDs 0B = average of the last ten drive cycles, 0C = this or the last cycle.")

    section("Other standard readings (every other mode 01 PID the car supports)", g("other"))
    if data.get("not_requested"):
        md += ["", "Not requested (the car's supported-PID list says it doesn't have them): "
               + ", ".join(n.lower() for n in data["not_requested"]) + "."]

    mk = g("make")
    if mk or data.get("signalset_errors"):
        md += ["", "## Manufacturer-specific (OBDb)", "",
               "Community-sourced requests and scalings, unverified on this car. The raw reply is in "
               "report.json for every request. `debug` = OBDb marks the request as still being worked out."]
        for e in data.get("signalset_errors", []):
            md.append(f"\n**Not read:** {e}")
        for src in dict.fromkeys(a["source"] for a in mk):
            rows = [a for a in mk if a["source"] == src]
            by = {}
            for a in rows:
                by[a["status"]] = by.get(a["status"], 0) + 1
            md += ["", f"### {src}", "", ", ".join(f"{n} {s}" for s, n in sorted(by.items())), ""]
            md.append(_table(["group", "signal", "module → request", "status", "value", "detail"],
                             [(a["path"], a["label"] + (" (debug)" if a["debug"] else ""),
                               f"{a['header']} → {a['request']}", a["status"],
                               f"{_cell(a['value'])} {a['unit']}".strip() if a["status"] == "ok" else "",
                               a["detail"]) for a in rows]))
    md += ["", "---", "", "Read-only: only services 01, 02, 03, 06, 07, 09, 0A and 22 were sent. Nothing was "
           "cleared, written or actuated."]
    return "\n".join(md) + "\n"


def save(data, md, out=None, pdf=True):
    """Write report.md, report.json (and report.pdf) to `out` or the vehicle's inspections/<YYYY>/<stamp>_inspect/."""
    if out:
        d = Path(out)
    else:
        d = records.stamped_dir(cli._data_dir(data.get("_vin")), "inspections") \
            / f"{dt.datetime.now():%Y%m%d_%H%M%S}_inspect"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(md)
    (d / "report.json").write_text(json.dumps({k: v for k, v in data.items() if not k.startswith("_")},
                                              indent=2, default=str) + "\n")
    made = [d / "report.md", d / "report.json"]
    if pdf:
        from . import report
        try:
            made.append(report.markdown_pdf(md, d / "report.pdf", "Vehicle inspection"))
        except Exception as e:  # noqa: BLE001
            print(f"PDF not made: {e}")
    return made
