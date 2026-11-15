import obd
import pytest

from obd2log.cli import READ_ONLY_MODES, ReadOnlyCar, _make_custom_commands


def test_write_and_clear_services_are_refused_before_sending():
    car = ReadOnlyCar.__new__(ReadOnlyCar)          # no adapter needed
    car.obd, car.custom = obd, _make_custom_commands(obd)
    car.conn = None                                  # any send would raise AttributeError
    with pytest.raises(PermissionError):
        car.query("CLEAR_DTC", force=True)
    for mode in ("04", "2E", "2F", "31", "3B"):
        with pytest.raises(PermissionError):
            car.query(obd.OBDCommand("X", "x", mode.encode() + b"00", 0, lambda m: None), force=True)
    assert "04" not in READ_ONLY_MODES
