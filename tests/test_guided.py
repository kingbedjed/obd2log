import gzip

import obd

from obd2log import guided
from test_inspect import FakeCar, Msg, resp

Q = obd.Unit.Quantity


class DrivingCar(FakeCar):
    """Engine off for 30 s of fake time, then idles, then drives at 72 km/h; coolant warms up."""

    def __init__(self, io):
        super().__init__()
        self.io = io
        self.answers["GET_DTC"] = resp([], [Msg("7E8", "4300")])
        self.answers["GET_CURRENT_DTC"] = resp([], [Msg("7E8", "4700")])

    def ask(self, cmd, force=False):
        name = cmd if isinstance(cmd, str) else cmd.name
        t = self.io.t
        live = {"RPM": Q(0 if t < 30 else 850, "rpm"),
                "SPEED": Q(0 if t < 45 else 72, "kph"),
                "COOLANT_TEMP": Q(min(90, 15 + t / 4), "degC"),
                "ELM_VOLTAGE": Q(12.4 if t < 30 else 14.4, "volt"),
                "LONG_FUEL_TRIM_1": Q(3.1, "percent")}
        if name in live:
            return resp(live[name], [Msg("7E8", "41")])
        if name == "GET_CURRENT_DTC" and t > 150:
            return resp([("P0171", "System Too Lean")], [Msg("7E8", "47")])
        return super().ask(cmd, force)

    def supports(self, name):
        return name in ("RPM", "SPEED", "COOLANT_TEMP", "LONG_FUEL_TRIM_1") or super().supports(name)


class FakeIO:
    def __init__(self):
        self.t, self.said, self.shown, self.keys = 0.0, [], [], []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.5)

    def show(self, text):
        self.shown.append(text)

    def status(self, text):
        pass

    def say(self, text, spoken=None):
        self.said.append(text)
        if text.startswith("Drive complete"):
            self.keys.append("")  # the user drives back and presses Enter

    def key(self):
        return self.keys.pop(0) if self.keys else None


def test_guided_session_waits_reads_drives_and_reports(tmp_path):
    io = FakeIO()
    car = DrivingCar(io)
    tries = []

    def connect():
        tries.append(1)
        if len(tries) < 3:
            return None, "Adapter on /dev/fake reads no vehicle voltage."
        return car, None

    data, files = guided.run_guided(connect, io, target_km=3.0, out=tmp_path, pdf=False, interval=1.0)
    assert any("no vehicle voltage" in s for s in io.shown)            # told what was missing, once
    assert sum("no vehicle voltage" in s for s in io.shown) == 1
    assert any("generic OBD-II report" in s for s in io.shown)         # no --make: warned
    assert [p["name"] for p in data["phases"]] == ["Before start (ignition on, engine off)",
                                                  "After the drive (engine running)"]
    order = [s.split(".")[0] for s in io.said]
    assert order[0].startswith("First read done") and any(s.startswith("Engine running") for s in order)
    assert any(s.startswith("Drive complete") for s in io.said)
    d = data["drive"]
    assert d["completed"] and d["distance_km"] >= 3.0 and d["max_coolant_c"] >= 80
    assert d["code_events"] and "P0171" in d["code_events"][0]["what"]
    assert d["median_running_v"] == 14.4
    with gzip.open(d["log"], "rt") as f:
        assert f.readline().startswith("timestamp,elapsed_s")
    md = (tmp_path / "report.md").read_text()
    assert "## Drive" in md and "P0171" in md and "## After the drive (engine running)" in md
    assert "Before start" in md and "[before start]" in md


def test_offline_without_cached_codes_warns_and_goes_generic(monkeypatch, tmp_path):
    import types
    import urllib.error
    from obd2log import cli, obdb

    monkeypatch.setattr(obdb, "cached", lambda make, model=None: None)

    def offline(make, model=None):
        raise urllib.error.URLError("no internet")
    monkeypatch.setattr(obdb, "fetch", offline)
    args = types.SimpleNamespace(make="ford", model="escape", make_wide=False, signalset=None, no_fetch=False)
    specs, errors = cli._signalsets(args)
    assert specs == [] and "could not fetch" in errors[0] and "no internet" in errors[0]

    io = FakeIO()
    car = DrivingCar(io)
    data, _ = guided.run_guided(lambda: (car, None), io, specs, errors, out=tmp_path, pdf=False)
    assert any(s.startswith("WARNING: no manufacturer-specific codes") and "no internet" in s for s in io.shown)
    assert "generic OBD-II report" in (tmp_path / "report.md").read_text()
