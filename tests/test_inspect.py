import types

import obd
import pytest

from obd2log import inspection, obdb
from obd2log.cli import READ_ONLY_MODES, ReadOnlyCar, _make_custom_commands

# Replies recorded from 2014 Escapes, from OBDb/Ford-Escape tests/test_cases/2014.
BATT_AGE = {"id": "ESCAPE_BATT_AGE", "name": "Battery age", "fmt": {"len": 16, "max": 1825, "unit": "scalar"}}
ODO = {"id": "ESCAPE_ODO_726", "name": "Odometer, instrument panel",
       "fmt": {"len": 24, "max": 16777215, "unit": "kilometers"}}
LOCK = {"id": "ESCAPE_CENTRAL_LOCK_STATUS", "name": "Lock status",
        "fmt": {"len": 8, "map": {"0": {"description": "Unlocked", "value": "UNLOCKED"},
                                  "1": {"description": "Locked", "value": "LOCKED"}}}}


def payload(reply, svc, pid):
    status, _, data, _ = obdb.parse_reply([("72E", bytes.fromhex(reply))], svc, pid)
    assert status == "ok"
    return data


def test_decodes_like_obdb_reference():
    assert obdb.decode_signal(BATT_AGE, payload("6240270E50", "22", "4027"))[0] == 1825
    assert obdb.decode_signal(ODO, payload("62DD0103736D", "22", "DD01"))[0] == 226157
    assert obdb.decode_signal(LOCK, payload("62C25900", "22", "C259"))[0] == "UNLOCKED"
    signed = {"fmt": {"len": 8, "sign": True, "min": -128, "max": 127, "unit": "scalar"}}
    assert obdb.decode_signal(signed, b"\xff")[0] == -1
    lsb = {"fmt": {"len": 16, "blsb": True, "max": 65535, "unit": "scalar"}}
    assert obdb.decode_signal(lsb, b"\x01\x02")[0] == 0x0201
    v, note = obdb.decode_signal({"fmt": {"len": 8, "max": 100, "unit": "percent"}}, b"\xc8")
    assert v == 100 and "clamped" in note
    with pytest.raises(ValueError, match="too short"):
        obdb.decode_signal(BATT_AGE, b"\x01")


def test_reply_classification():
    assert obdb.parse_reply([], "22", "1624")[0] == "no response"
    st, detail, _, _ = obdb.parse_reply([("7E8", bytes.fromhex("7F2231"))], "22", "1624")
    assert st == "negative response" and "out of range" in detail
    assert obdb.parse_reply([("7E8", bytes.fromhex("620301AA"))], "22", "1624")[0] == "unexpected reply"


def test_plan_says_why_requests_are_not_sent():
    sig = [BATT_AGE]
    ss = {"commands": [
        {"hdr": "7E0", "cmd": {"22": "0001"}, "signals": sig},
        {"hdr": "7E0", "cmd": {"21": "01"}, "signals": sig},
        {"hdr": "DA10", "cmd": {"22": "0002"}, "signals": sig},
        {"hdr": "7E0", "din": "03", "cmd": {"22": "0003"}, "signals": sig},
        {"hdr": "7E0", "cmd": {"22": "0004"}, "filter": {"from": 2020}, "signals": sig},
        {"hdr": "7E0", "cmd": {"22": "0005"}, "filter": {"to": 2019, "from": 2027}, "signals": sig},
    ]}
    p = {r["pid"]: r["skip"] for r in obdb.plan(ss, 2014, READ_ONLY_MODES)}
    assert p["0001"] is None and p["0005"] is None
    assert "service 21" in p["01"] and "29-bit" in p["0002"] and "session" in p["0003"]
    assert "2020 on" in p["0004"]
    assert obdb.plan(ss, 2014, READ_ONLY_MODES, all_years=True)[4]["skip"] is None


def test_make_specific_requests_go_through_the_read_only_guard():
    car = ReadOnlyCar.__new__(ReadOnlyCar)
    car.obd, car.custom, car.conn = obd, _make_custom_commands(obd), None
    with pytest.raises(PermissionError):
        car.request("7E0", "2E", "F190")
    with pytest.raises(PermissionError):
        car.request("726", "31", "0100")


def test_model_year_from_vin():
    import datetime as dt
    assert inspection.model_year("1FMCU0GX5EU000000", dt.date(2026, 11, 1)) == 2014
    assert inspection.model_year("1FMCU0GX5AU000000", dt.date(2026, 11, 1)) == 2010
    assert inspection.model_year("short") is None


# ---- whole inspection against a fake car -------------------------------------------

class Msg:
    def __init__(self, can, hexdata):
        self.data = bytearray(bytes.fromhex(hexdata))
        self.frames = [types.SimpleNamespace(raw=f"{can} {hexdata}")]


def resp(value=None, msgs=()):
    return types.SimpleNamespace(value=value, messages=list(msgs))


class FakeCar:
    """Answers a few requests; everything else gets no reply."""

    def __init__(self):
        self.obd, self.custom, self.vin = obd, _make_custom_commands(obd), "1FMCU0GX5EU000000"
        status = obd.decoders.status([types.SimpleNamespace(data=bytearray(bytes.fromhex("4101000763E0")))])
        self.answers = {
            "STATUS": resp(status, [Msg("7E8", "4101000763E0")]),
            "GET_DTC": resp([], [Msg("7E8", "4300")]),
            "PERMANENT_DTC": resp([("P1299", "")], [Msg("7E8", "4A0112 99")]),
            "DISTANCE_SINCE_DTC_CLEAR": resp(obd.Unit.Quantity(12, "km"), [Msg("7E8", "4131000C")]),
            "COOLANT_TEMP": resp(obd.Unit.Quantity(60, "degC"), [Msg("7E8", "410564")]),
            "AMBIANT_AIR_TEMP": resp(obd.Unit.Quantity(5, "degC"), [Msg("7E8", "41462D")]),
            "RPM": resp(obd.Unit.Quantity(0, "rpm"), [Msg("7E8", "410C0000")]),
            "OIL_TEMP": resp(None, [Msg("7E8", "7F0131")]),
        }
        self.sent = []
        self.conn = types.SimpleNamespace(
            port_name=lambda: "/dev/fake", protocol_name=lambda: "ISO 15765-4 (CAN 11/500)",
            protocol_id=lambda: "6", supports=lambda c: c.name in self.answers,
            supported_commands=[obd.commands[n] for n in ("STATUS", "RPM", "ENGINE_LOAD")])

    def ask(self, cmd, force=False):
        return self.answers.get(cmd.name, resp())

    def request(self, header, service, pid):
        self.sent.append((header, service, pid))
        if (header, pid) == ("726", "4027"):
            return [("72E", bytes.fromhex("6240270E50"))]
        if (header, pid) == ("726", "DD01"):
            return [("72E", bytes.fromhex("62DD0103736D"))]
        if pid == "1624":
            return [("7E8", bytes.fromhex("7F2231"))]
        return []


def test_inspection_reports_every_attempt_including_failures():
    ss = {"commands": [
        {"hdr": "726", "rax": "72E", "cmd": {"22": "4027"}, "signals": [BATT_AGE]},
        {"hdr": "726", "rax": "72E", "cmd": {"22": "DD01"}, "signals": [ODO]},
        {"hdr": "7E0", "rax": "7E8", "cmd": {"22": "1624"}, "signals": [dict(BATT_AGE, name="CHT")]},
        {"hdr": "7E0", "cmd": {"22": "0999"}, "signals": [dict(BATT_AGE, name="Silent")]},
        {"hdr": "7E0", "cmd": {"21": "01"}, "signals": [dict(BATT_AGE, name="Mode21")]},
    ]}
    car = FakeCar()
    data = inspection.run(car, [(ss, {"repo": "OBDb/Test", "commit": "abc", "url": None})])
    md = inspection.render(data)
    make = {a["label"]: a for a in data["attempts"] if a["group"] == "make"}
    assert make["Battery age"]["value"] == 1825
    assert make["CHT"]["status"] == "negative response" and "7F 22 31" in make["CHT"]["detail"]
    assert make["Silent"]["status"] == "no response"
    assert make["Mode21"]["status"] == "not sent"
    assert ("7E0", "21", "01") not in car.sent
    for label in ("Battery age", "CHT", "Silent", "Mode21"):
        assert label in md
    std = {a["name"]: a for a in data["attempts"] if "name" in a}
    assert std["OIL_TEMP"]["status"] == "negative response"
    assert std["WARMUPS_SINCE_DTC_CLEAR"]["status"] == "no response"
    assert "Warm-ups since codes cleared" in md and "no response" in md
    text = " ".join(t for _, t in data["findings"])
    assert "cleared recently" in text and "P1299" in text and "226157 km" in text
    assert "1FMCU0GX5EU000000" not in md and "1FMCU0GX5EU******" in md
