"""Where data lives: config, vehicles (matched by VIN hash) and cases.

Config: ~/.config/obd2log/config.toml (override with $OBD2LOG_CONFIG)

    records = "~/git/car-records"          # the data repo
    rclone_remote = "gdrive:Car Records"    # optional, for `obd2log upload`
    port = "/dev/serial/by-id/..."          # optional adapter port

Records layout:

    <records>/vehicles/<slug>/vehicle.md    frontmatter incl. vin_sha256
    <records>/vehicles/<slug>/logs/<YYYY>/<YYYYmmdd_HHMMSS>.csv.gz
    <records>/vehicles/<slug>/snapshots/<YYYY>/<YYYYmmdd_HHMMSS>_<kind>.json
    <records>/vehicles/<slug>/cases/<YYYY-MM-DD>_<slug>/case.md
    <records>/unassigned/...                 data from a car with no vehicle folder yet
"""

import datetime as dt
import hashlib
import os
import re
import tomllib
from pathlib import Path

CONFIG = Path(os.environ.get("OBD2LOG_CONFIG", "~/.config/obd2log/config.toml")).expanduser()
STAMP = re.compile(r"(\d{8})_(\d{6})")


def config():
    if CONFIG.exists():
        return tomllib.loads(CONFIG.read_text())
    return {}


def records_root():
    r = config().get("records")
    return Path(r).expanduser() if r else None


def vin_hash(vin):
    return hashlib.sha256(vin.strip().upper().encode()).hexdigest() if vin else None


def frontmatter(text):
    """Minimal YAML-ish frontmatter: `key: value`; comma-separated lists for include/exclude."""
    meta, body = {}, text
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        for line in head.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    return meta, body


def vehicles():
    root = records_root()
    if not root:
        return []
    return sorted(p.parent for p in (root / "vehicles").glob("*/vehicle.md"))


def vehicle_for_vin(vin):
    """The vehicle folder whose vin_sha256 matches, else <records>/unassigned, else None."""
    root = records_root()
    if not root:
        return None
    h = vin_hash(vin)
    for v in vehicles():
        if h and frontmatter((v / "vehicle.md").read_text())[0].get("vin_sha256") == h:
            return v
    return root / "unassigned"


def stamped_dir(base, kind, when=None):
    """<base>/<kind>/<YYYY>/, created."""
    d = Path(base) / kind / f"{(when or dt.datetime.now()):%Y}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_time(path):
    m = STAMP.search(Path(path).name)
    return dt.datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S") if m else None


def _date(s):
    return dt.date.fromisoformat(s) if s else None


def _names(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


def case_files(case):
    """Logs and snapshots for a case: its vehicle's files dated opened..closed (inclusive),
    plus frontmatter `include`, minus `exclude` (file names)."""
    case = Path(case).resolve()
    meta, _ = frontmatter((case / "case.md").read_text())
    vehicle = case.parent.parent
    start, end = _date(meta.get("opened")), _date(meta.get("closed")) or dt.date.today()
    include, exclude = _names(meta.get("include")), _names(meta.get("exclude"))

    def pick(kind, patterns):
        files = [f for pat in patterns for f in (vehicle / kind).rglob(pat)]
        keep = []
        for f in files:
            t = file_time(f)
            in_window = t is not None and (start is None or t.date() >= start) and t.date() <= end
            if (in_window or f.name in include) and f.name not in exclude:
                keep.append(f)
        return sorted(keep, key=lambda f: (file_time(f) or dt.datetime.min, f.name))

    return vehicle, pick("logs", ["*.csv", "*.csv.gz"]), pick("snapshots", ["*.json"])
