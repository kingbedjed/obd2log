"""Manufacturer-specific requests from OBDb (https://github.com/OBDb), a community database.

OBDb keeps one GitHub repo per make (`OBDb/Ford`, an aggregate of every model's
requests, all marked debug) and per model (`OBDb/Ford-Escape`). Each repo has
`signalsets/v3/default.json`: commands (module header, service, PID, model-year
filter) and the signals to decode from each reply (bit position, length,
scaling, unit). Data is CC-BY-SA-4.0; it is downloaded into a local cache, never
vendored into this repo.

These definitions are community-sourced and unverified for any particular car:
the inspection report shows the raw reply next to every decoded value.

Decoding follows OBDb's reference implementation (OBDb/.schemas,
python/can/signals.py): bits are counted MSB-first from the first byte after
the service and PID echo; value = raw * mul / div + add, clamped to
[min, max]; values at nullmin / nullmax are null.
"""

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ORG = "OBDb"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "obd2log" / "obdb"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
SIGNALSET = "signalsets/v3/default.json"

# Negative response codes (ISO 14229-1 / ISO 14230-3) as plain words.
NRC = {
    0x10: "general reject", 0x11: "service not supported", 0x12: "sub-function not supported",
    0x13: "wrong message length", 0x14: "response too long", 0x21: "busy, repeat request",
    0x22: "conditions not correct", 0x24: "request sequence error", 0x31: "request out of range (PID not supported)",
    0x33: "security access denied", 0x78: "response pending", 0x7E: "not supported in active session",
    0x7F: "service not supported in active session",
}


# --------------------------------------------------------------------------
# fetch / cache
# --------------------------------------------------------------------------

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "obd2log", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def repo_name(make, model=None):
    """'ford', 'escape' -> 'Ford-Escape' (a guess at OBDb's capitalisation; resolve() checks it)."""
    part = lambda s: "-".join(w[:1].upper() + w[1:] for w in re.split(r"[\s_-]+", s.strip()) if w)
    return part(make) + (f"-{part(model)}" if model else "")


def search(text):
    """OBDb repo names containing `text` (case-insensitive), via the GitHub search API."""
    q = urllib.parse.quote(f"{text} org:{ORG}")
    data = json.loads(_get(f"{API}/search/repositories?q={q}&per_page=100"))
    return sorted(i["name"] for i in data.get("items", []) if not i["name"].startswith("."))


def resolve(make, model=None):
    """The exact OBDb repo name for make/model, or raise LookupError with suggestions."""
    want = repo_name(make, model)
    try:
        _get(f"{API}/repos/{ORG}/{want}")
        return want
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    found = search(model or make)
    for name in found:
        if name.lower() == want.lower():
            return name
    near = [n for n in found if n.lower().startswith(repo_name(make).lower())]
    raise LookupError(f"no OBDb repo {want!r}" + (f"; similar: {', '.join(near[:15])}" if near else ""))


def fetch(make, model=None):
    """Download the repo's signalset at its current commit into the cache. Returns the cache path."""
    repo = resolve(make, model)
    sha = json.loads(_get(f"{API}/repos/{ORG}/{repo}/commits/HEAD"))["sha"]
    body = _get(f"{RAW}/{ORG}/{repo}/{sha}/{SIGNALSET}")
    json.loads(body)  # must parse
    d = CACHE / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "default.json").write_bytes(body)
    (d / "source.json").write_text(json.dumps({
        "repo": f"{ORG}/{repo}", "commit": sha,
        "url": f"https://github.com/{ORG}/{repo}/blob/{sha}/{SIGNALSET}",
        "license": "CC-BY-SA-4.0", "fetched": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }, indent=2) + "\n")
    return d / "default.json"


def cached(make, model=None):
    """Cached signalset path for make/model (case-insensitive match), or None."""
    want = repo_name(make, model).lower()
    if CACHE.exists():
        for d in CACHE.iterdir():
            if d.name.lower() == want and (d / "default.json").exists():
                return d / "default.json"
    return None


def load(path):
    """(signalset dict, source dict) for a signalset file; source comes from source.json beside it."""
    path = Path(path)
    src = path.parent / "source.json"
    source = json.loads(src.read_text()) if src.exists() else {"repo": str(path), "commit": None, "url": None}
    return json.loads(path.read_text()), source


# --------------------------------------------------------------------------
# planning: what to send, and why not
# --------------------------------------------------------------------------

def year_matches(flt, year):
    """OBDb filter semantics (signals.py Filter.matches). No filter = every year."""
    if not flt or year is None:
        return True
    a, b, years = flt.get("from"), flt.get("to"), flt.get("years") or []
    if a is not None and b is not None:
        if (a < b and a <= year <= b) or (a >= b and (year >= a or year <= b)):
            return True
    elif b is not None and year <= b:
        return True
    elif a is not None and year >= a:
        return True
    return year in years


def describe_filter(flt):
    parts = []
    if flt.get("from") is not None and flt.get("to") is not None and flt["from"] < flt["to"]:
        parts.append(f"{flt['from']}-{flt['to']}")
    else:
        if flt.get("to") is not None:
            parts.append(f"up to {flt['to']}")
        if flt.get("from") is not None:
            parts.append(f"{flt['from']} on")
    if flt.get("years"):
        parts.append(", ".join(map(str, flt["years"])))
    return " or ".join(parts)


def plan(signalset, year, allowed_modes, all_years=False, source=None):
    """Every command in the signalset as a request, with `skip` set to the reason it won't be sent.

    Requests with the same header, service and PID are merged (their signals combined)."""
    out, seen = [], {}
    for c in signalset.get("commands", []):
        (svc, pid), = c["cmd"].items()
        hdr = c.get("hdr", "7DF").upper()
        key = (hdr, c.get("rax"), svc.upper(), pid.upper())
        if key in seen:
            seen[key]["signals"] += c["signals"]
            continue
        skip = None
        if svc.upper() not in allowed_modes:
            skip = (f"not sent: service {svc} is outside obd2log's read-only allowlist "
                    f"({' '.join(sorted(allowed_modes))})")
        elif len(hdr) != 3:
            skip = f"not sent: 29-bit CAN header {hdr}; obd2log only addresses 11-bit CAN modules"
        elif c.get("eax") or c.get("tst"):
            skip = "not sent: needs CAN extended addressing (eax/tst), not supported"
        elif c.get("din"):
            skip = (f"not sent: needs diagnostic session {c['din']} first; obd2log never sends "
                    "session control (service 10)")
        elif c.get("proto") or c.get("baud") or c.get("init"):
            skip = f"not sent: needs protocol {c.get('proto', 'K-line')}; obd2log uses the car's CAN link"
        elif not all_years and not year_matches(c.get("filter"), year):
            skip = (f"not sent: OBDb lists this for model years {describe_filter(c['filter'])}, "
                    f"car is {year} (--all-years to try anyway)")
        req = {"header": hdr, "rax": (c.get("rax") or "").upper() or None, "service": svc.upper(),
               "pid": pid.upper(), "signals": list(c["signals"]), "skip": skip,
               "debug": bool(c.get("dbg")), "source": (source or {}).get("repo")}
        seen[key] = req
        out.append(req)
    return out


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

def _bits(data, bix, length, lsb=False):
    end = bix + length
    if end > len(data) * 8:
        raise ValueError(f"reply too short: signal needs bits {bix}..{end - 1}, reply has {len(data) * 8}")
    data = bytearray(data)
    if lsb and length > 8:
        s = bix // 8
        n = (length + 7) // 8
        data[s:s + n] = bytes(reversed(data[s:s + n]))
    v = 0
    for i in range(bix, end):
        if data[i // 8] & (1 << (7 - i % 8)):
            v |= 1 << (end - i - 1)
    return v


def decode_signal(sig, data):
    """(value, note) for one signal from the payload after the service+PID echo.

    value is None when it can't be decoded; note explains why or flags clamping."""
    f = sig["fmt"]
    raw = _bits(data, f.get("bix", 0), f["len"], f.get("blsb", False))
    if "map" in f:
        m = f["map"].get(str(raw))
        if m is None:
            return None, f"raw value {raw} is not in OBDb's map ({', '.join(f['map'])})"
        return (m["value"] if isinstance(m, dict) else str(m)), f"raw {raw}"
    if f.get("sign") and raw & (1 << (f["len"] - 1)):
        raw -= 1 << f["len"]
    v = raw * f.get("mul", 1) / f.get("div", 1) + f.get("add", 0)
    lo, hi = f.get("min", 0), f["max"]
    note = f"raw {raw}"
    if hi > lo and not lo <= v <= hi:
        note += f"; scaled {v:g} is outside OBDb's range {lo:g}..{hi:g}, clamped (suspect)"
        v = max(lo, min(v, hi))
    if f.get("nullmin") is not None and v <= f["nullmin"]:
        return None, note + f"; at OBDb's null bound (<= {f['nullmin']:g}): no reading"
    if f.get("nullmax") is not None and v >= f["nullmax"]:
        return None, note + f"; at OBDb's null bound (>= {f['nullmax']:g}): no reading"
    return round(v, 4), note


UNITS = {"celsius": "°C", "percent": "%", "kilometers": "km", "kilometersPerHour": "km/h", "volts": "V",
         "kilopascal": "kPa", "degrees": "°", "seconds": "s", "newtonMeters": "N·m", "scalar": "",
         "offon": "(0=off 1=on)", "noyes": "(0=no 1=yes)", "yesno": "(0=yes 1=no)"}


def unit(sig):
    u = sig["fmt"].get("unit", "")
    return UNITS.get(u, u)


def parse_reply(lines, service, pid):
    """Classify the raw reply to one request.

    lines: [(can_id, payload bytes)] for each reassembled message.
    Returns (status, detail, payload_after_echo or None, replying ids)."""
    ids = sorted({i for i, _ in lines if i})
    if not lines:
        return "no response", "no reply (NO DATA / timeout): module absent, asleep, or doesn't know this request", None, ids
    s = int(service, 16)
    echo = bytes([s + 0x40]) + bytes.fromhex(pid)
    for _, data in lines:
        if data[:len(echo)] == echo:
            return "ok", "", data[len(echo):], ids
    for _, data in lines:
        if len(data) >= 3 and data[0] == 0x7F and data[1] == s:
            return ("negative response", f"7F {service} {data[2]:02X}: {NRC.get(data[2], 'unknown code')}",
                    None, ids)
    return "unexpected reply", "reply doesn't echo this request: " + "; ".join(d.hex(" ").upper() for _, d in lines), None, ids
