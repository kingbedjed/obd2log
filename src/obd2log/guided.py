"""Guided used-car inspection: `obd2log inspect` without --once.

1. Waits for the adapter and the ignition (tells you which is missing).
2. Full read with the ignition on, ideally before the engine has been started.
3. Asks you to start the engine and drive; logs the drive, tracks distance
   from vehicle speed, watches for new codes, and says when the drive is done.
4. When you're back and press Enter, does a second full read (warm engine),
   then prints the report and saves it with a PDF.

Prompts are printed and, if `spd-say` is installed, spoken (--quiet to turn
speech off), since you'll be driving.
"""

import csv
import datetime as dt
import gzip
import logging
import queue
import signal
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import cli, inspection

MI = 1.609344


def miles(km):
    return f"{round(km / MI, 1):g}"


class IO:
    """Terminal, speech, keyboard and clock; tests replace it."""

    def __init__(self, speak=True):
        self.speak = speak and shutil.which("spd-say") is not None
        self.keys = queue.Queue()
        self.tty = sys.stdout.isatty()
        threading.Thread(target=self._read_keys, daemon=True).start()

    def _read_keys(self):
        for line in sys.stdin:
            self.keys.put(line.strip())

    def key(self):
        """A line the user entered since the last call, or None."""
        try:
            return self.keys.get_nowait()
        except queue.Empty:
            return None

    def show(self, text):
        print(("\r\033[K" if self.tty else "") + text, flush=True)

    def status(self, text):
        if self.tty:
            print("\r\033[K" + text, end="", flush=True)

    def say(self, text, spoken=None):
        """An instruction: printed prominently, with a bell, and spoken."""
        print(("\r\033[K" if self.tty else "") + f"\a\n>>> {text}\n", flush=True)
        if self.speak:
            subprocess.Popen(["spd-say", spoken or text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    clock = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


def default_connect(port):
    """(car, None) when the car answers, else (None, what's missing)."""
    def connect():
        car = cli.ReadOnlyCar(port)
        if car.connected:
            car.vin = car.query("VIN17", force=True)
            return car, None
        car.close()
        return None, cli._diagnose_adapter(port)
    return connect


def wait_for_car(connect, io, retry=3.0):
    shown = None
    while True:
        car, why = connect()
        if car:
            return car
        if why != shown:
            io.show(f"waiting: {why}")
            shown = why
        io.sleep(retry)


def _answering(car):
    return car.query("RPM", force=True) is not None or car.query("STATUS", force=True) is not None


def read(car, io, name, specs, year, all_years):
    total = inspection.count_requests(car, specs, year, all_years)
    io.show(f"{name}: reading everything the car reports ({total} requests, about "
            f"{max(1, round(total * 0.25 / 60))} min). Keep the ignition on.")
    t0, n = io.clock(), [0]

    def tick():
        n[0] += 1
        left = (io.clock() - t0) / n[0] * (total - n[0])
        io.status(f"  {n[0]}/{total} · about {left:.0f} s left")

    ph = inspection.read_phase(car, name, specs, year, all_years, tick)
    io.show(f"  done in {io.clock() - t0:.0f} s: {inspection._counts(ph['attempts'])}")
    return ph


def _codes(car):
    st = car.query("STATUS", force=True)
    stored = cli._codes(car.query("GET_DTC", force=True))
    pending = cli._codes(car.query("GET_CURRENT_DTC", force=True))
    return (None if st is None else bool(st.MIL)), stored, pending


def drive(car, io, target_km, log_path, interval=1.0, dtc_every=30.0):
    """Log until the user presses Enter. Returns the drive summary for the report."""
    fields = [(c, n, u) for c, n, u in cli.STANDARD_FIELDS if car.supports(n)]
    cols = ["timestamp", "elapsed_s"] + [c for c, _, _ in fields] + \
           ["battery_v", "distance_km", "mil", "stored_dtcs", "pending_dtcs"]
    csv_path = Path(log_path).with_suffix("")  # drive.csv while logging, drive.csv.gz after
    s = {"target_km": target_km, "distance_km": 0.0, "samples": 0, "completed": False, "code_events": [],
         "max_speed_kph": None, "start_coolant_c": None, "max_coolant_c": None, "minutes_to_80c": None,
         "counter_km": None, "log": None, "note": ""}
    volts, ltft1, ltft2 = [], [], []
    t0 = last = io.clock()
    started = None
    silent = 0
    next_dtc = 0.0
    mil, stored, pending = _codes(car)
    first_counter = car.query("DISTANCE_SINCE_DTC_CLEAR", force=True)
    half_said = warm_wait_said = False
    stop = []
    old_int = signal.signal(signal.SIGINT, lambda *_: stop.append(1)) \
        if threading.current_thread() is threading.main_thread() else None
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        while io.key() is None and not stop:
            t = io.clock()
            vals = {c: cli._mag(car.query(n), u) for c, n, u in fields}
            if all(v is None for v in vals.values()):
                silent += 1
                if silent == 3:
                    io.say("The car isn't answering. If the ignition is off, turn it back on to keep logging, "
                           "or press Enter to finish.", "The car isn't answering.")
                io.sleep(2.0)
                last = io.clock()
                continue
            if silent >= 3:
                io.show("car answering again; logging resumed")
            silent = 0
            rpm, spd, cool = vals.get("rpm"), vals.get("speed_kph"), vals.get("coolant_temp_c")
            if spd:
                s["distance_km"] += spd * min(t - last, 5.0) / 3600
            last = t
            if started is None and rpm and rpm > 300:
                started = t
                io.say(f"Engine running. Drive about {miles(target_km)} miles: some stop-and-go, a stretch at "
                       "45 mph or more, and a few firm accelerations. I'll tell you when that's done.",
                       f"Engine running. Drive about {miles(target_km)} miles.")
            mins = (t - (started or t0)) / 60
            if cool is not None:
                s["start_coolant_c"] = cool if s["start_coolant_c"] is None else s["start_coolant_c"]
                s["max_coolant_c"] = max(cool, s["max_coolant_c"] or cool)
                if cool >= 80 and s["minutes_to_80c"] is None and started:
                    s["minutes_to_80c"] = round(mins, 1)
            if spd is not None:
                s["max_speed_kph"] = max(spd, s["max_speed_kph"] or 0)
            batt = cli._mag(car.query("ELM_VOLTAGE"), "volt")
            if rpm and rpm > 300:
                volts += [batt] if batt else []
                if cool and cool >= 80:
                    ltft1 += [vals["ltft_b1_pct"]] if vals.get("ltft_b1_pct") is not None else []
                    ltft2 += [vals["ltft_b2_pct"]] if vals.get("ltft_b2_pct") is not None else []
            if t >= next_dtc:
                m2, st2, pe2 = _codes(car)
                new = [c for c in st2 if c not in stored] + [c for c in pe2 if c not in pending]
                if new:
                    s["code_events"].append({"minute": round(mins, 1), "what": "new code(s) " + ", ".join(new)})
                    io.say(f"New trouble code: {', '.join(new)}.", "New trouble code.")
                if m2 and not mil:
                    s["code_events"].append({"minute": round(mins, 1), "what": "check-engine light came on"})
                    io.say("The check-engine light just came on.")
                mil = m2 if m2 is not None else mil
                stored, pending = st2 or stored, pe2 or pending
                next_dtc = t + dtc_every
            km = s["distance_km"]
            if started and not half_said and km >= target_km / 2:
                half_said = True
                io.say(f"Halfway: {km / MI:.1f} miles.")
            if started and not s["completed"] and km >= target_km:
                if cool is None or cool >= 80:
                    s["completed"] = True
                    io.say("Drive complete. Head back whenever you're ready, and press Enter when you're parked "
                           "with the engine still running.", "Drive complete. Head back whenever you're ready.")
                elif not warm_wait_said:
                    warm_wait_said = True
                    io.say(f"Distance done. Keep driving until the engine is fully warm; coolant is {cool:g} °C.",
                           "Distance done. Keep driving until the engine is warm.")
            w.writerow([dt.datetime.now().astimezone().isoformat(timespec="milliseconds"), round(t - t0, 2)]
                       + list(vals.values()) + [batt, round(km, 3), mil, " ".join(stored), " ".join(pending)])
            s["samples"] += 1
            if s["samples"] % 10 == 0:
                f.flush()
            state = ("waiting for the engine to start (press Enter to skip the drive)" if not started else
                     "drive complete, press Enter when parked" if s["completed"] else "driving")
            io.status(f"  {km / MI:.1f}/{target_km / MI:.1f} mi · {(spd or 0) / MI:.0f} mph · coolant "
                      f"{cool if cool is not None else '?'} °C · {batt or '?'} V · codes "
                      f"{' '.join(stored + pending) or 'none'} · {state}")
            io.sleep(max(0.0, interval - (io.clock() - t)))
    if old_int is not None:
        signal.signal(signal.SIGINT, old_int)
    io.show("")
    last_counter = car.query("DISTANCE_SINCE_DTC_CLEAR", force=True)
    if first_counter is not None and last_counter is not None:
        s["counter_km"] = round(cli._mag(last_counter, "km") - cli._mag(first_counter, "km"), 1)
    s["minutes"] = round((io.clock() - (started or t0)) / 60, 1)
    s["median_running_v"] = round(statistics.median(volts), 2) if volts else None
    s["ltft_b1_warm_median"] = round(statistics.median(ltft1), 1) if ltft1 else None
    s["ltft_b2_warm_median"] = round(statistics.median(ltft2), 1) if ltft2 else None
    s["distance_km"] = round(s["distance_km"], 2)
    if not started:
        s["note"] = "the engine was never started"
    if s["samples"]:
        gz = csv_path.with_name(csv_path.name + ".gz")
        with csv_path.open("rb") as src, gzip.GzipFile(gz, "wb", mtime=0) as dst:
            shutil.copyfileobj(src, dst)
        s["log"] = str(gz)
    csv_path.unlink()
    return s


def run_guided(connect, io, specs=(), spec_errors=(), year=None, all_years=False, target_km=5 * MI,
               out=None, pdf=True, interval=1.0, show_vin=False):
    """The whole guided session. Returns (data, files)."""
    logging.getLogger("obd").setLevel(logging.CRITICAL)
    if spec_errors or not specs:
        io.show("WARNING: no manufacturer-specific codes: " + ("; ".join(spec_errors) if spec_errors else
                "no --make/--model given") + ". This will be a generic OBD-II report.")
    else:
        io.show("manufacturer signals: " + ", ".join(s[1]["repo"] for s in specs))
    io.show("Plug the adapter into the OBD port (under the dash, driver's side) and turn the ignition on. "
            "Ideally don't start the engine yet.")
    car = wait_for_car(connect, io)
    data = inspection.new_report(car, specs, year, spec_errors)
    d = Path(out) if out else inspection.report_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    rpm = car.query("RPM", force=True)
    running = bool(rpm is not None and cli._mag(rpm, None) > 300)
    io.show(f"connected: {data['vin_masked'] or 'no VIN'}, model year {data['model_year'] or '?'}; engine "
            + ("ALREADY RUNNING (no cold-start check possible)" if running else "off: good, this is the cold read"))
    first = "Before start (ignition on, engine off)" if not running else "At arrival (engine already running)"
    data["phases"].append(read(car, io, first, specs, data["model_year"], all_years))
    io.say(f"First read done. Start the engine and drive about {miles(target_km)} miles. "
           "I'll track the distance and tell you when you're done.",
           f"First read done. Start the engine and drive about {miles(target_km)} miles.")
    while io.key() is not None:   # ignore Enter presses from before the drive
        pass
    data["drive"] = drive(car, io, target_km, d / "drive.csv.gz", interval)
    if data["drive"].get("log"):
        try:
            from . import report
            fig = d / "figures" / "drive.png"
            fig.parent.mkdir(exist_ok=True)
            report.plot_log(data["drive"]["log"], fig)
            data["drive"]["figure"] = str(fig)
        except Exception as e:  # noqa: BLE001
            io.show(f"drive figure not made: {e}")

    if not _answering(car):
        io.say("For the final read, turn the ignition on (engine can run). Type s and Enter to skip it.",
               "Turn the ignition on for the final read.")
        while io.key() is not None:
            pass
        while not _answering(car):
            if (io.key() or "").lower().startswith("s"):
                break
            io.sleep(2.0)
    if _answering(car):
        rpm = car.query("RPM", force=True)
        running = bool(rpm is not None and cli._mag(rpm, None) > 300)
        data["phases"].append(read(car, io, f"After the drive (engine {'running' if running else 'off'})",
                                   specs, data["model_year"], all_years))
    else:
        io.show("final read skipped")
    car.close()
    data["findings"] = inspection.findings(data)
    print(inspection.render(data, show_vin=car.vin if show_vin else None))
    files = inspection.save(data, inspection.render(data), d, pdf=pdf)
    io.say(f"Report ready: {d / 'report.pdf' if pdf else d / 'report.md'}", "Report ready.")
    return data, files
