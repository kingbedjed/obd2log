"""Build a self-contained PDF report for a case folder, and upload it.

A case is one car problem: vehicles/<vehicle>/cases/<date>_<slug>/case.md
(frontmatter title, opened, closed, status, include, exclude; then the
write-up). Its data is the vehicle's logs and snapshots dated opened..closed
(see records.case_files). Image paths in case.md are relative to case.md.

`obd2log report <case>` writes <case>/report/: figures/*.png, report.html and
report.pdf. The logs, snapshots and case.md are embedded in the PDF as file
attachments, so the PDF alone is the complete record.
"""

import csv
import datetime as dt
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import cli as obd2log
from . import records

# Categorical slots 1-4 and the critical status colour from the dataviz
# reference palette (light mode).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
CRITICAL = "#d03b3b"
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

PANELS = [
    # (title, unit, [(column, label), ...]) - a panel is drawn if any column exists
    ("Temperatures", "°C", [("coolant_temp_c", "coolant"), ("intake_air_temp_c", "intake air"),
                            ("ambient_air_temp_c", "ambient")]),
    ("Speed", "km/h", [("speed_kph", "speed")]),
    ("Engine speed", "rpm", [("rpm", "rpm")]),
    ("Engine load", "%", [("engine_load_pct", "load")]),
    ("Fuel trim, bank 1", "%", [("stft_b1_pct", "short-term"), ("ltft_b1_pct", "long-term")]),
    ("Fuel trim, bank 2", "%", [("stft_b2_pct", "short-term"), ("ltft_b2_pct", "long-term")]),
    ("Ford CHT raw (scale unverified)", "raw", [("cht_raw", "raw")]),
    ("Misfires this drive cycle, per cylinder", "count",
     [(f"misfire_cyl{i}_cycle", f"cyl {i}") for i in range(1, 5)]),
    ("Battery", "V", [("battery_v", "battery")]),
]


def _read_log(path):
    with obd2log.open_log(path) as f:
        rows = list(csv.DictReader(f))
    cols = {c: [obd2log._f(r[c]) for r in rows] for c in rows[0]} if rows else {}
    return rows, cols


def _code_events(rows):
    events, prev = [], {"stored_dtcs": None, "pending_dtcs": None}
    for r in rows:
        for k in prev:
            if k in r and r[k] != prev[k]:
                if prev[k] is not None:
                    events.append((obd2log._f(r["elapsed_s"]) / 60, k.split("_")[0], r[k] or "none"))
                prev[k] = r[k]
    return events


def plot_log(path, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows, cols = _read_log(path)
    t = [x / 60 for x in cols["elapsed_s"]]
    panels = [(title, unit, [(c, lab) for c, lab in series if c in cols and any(v is not None for v in cols[c])])
              for title, unit, series in PANELS]
    panels = [p for p in panels if p[2]]
    events = _code_events(rows)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
                         "text.color": INK})
    fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(7.2, 1.35 * len(panels) + 0.6))
    axes = [axes] if len(panels) == 1 else axes
    for ax, (title, unit, series) in zip(axes, panels):
        for i, (c, lab) in enumerate(series):
            ax.plot(t, cols[c], color=SERIES[i], lw=1.4, label=lab, solid_capstyle="round")
        ax.set_title(f"{title} ({unit})", loc="left", fontsize=9, color=INK, pad=3)
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(length=0)
        if len(series) > 1:
            ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8)
        for m, kind, codes in events:
            ax.axvline(m, color=CRITICAL, lw=0.9, ls=(0, (3, 2)))
    for m, kind, codes in events:
        axes[0].annotate(f"{kind}: {codes}", (m, 1.0), xycoords=("data", "axes fraction"),
                         xytext=(2, 2), textcoords="offset points", fontsize=7.5, color=CRITICAL)
    axes[-1].set_xlabel("minutes since log start")
    fig.suptitle(f"{Path(path).name.removesuffix('.gz')}   start {rows[0]['timestamp'][:19].replace('T', ' ')}",
                 x=0.01, ha="left", fontsize=9.5, color=INK_2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=160)
    plt.close(fig)


def _snapshot_html(snap):
    kind = snap.get("kind")
    out = [f"<h3>{html.escape(kind)} snapshot · {html.escape(snap.get('taken', ''))}</h3>"]
    if snap.get("source") and snap["source"] != "obd2log live read":
        out.append(f"<p class=note>Source: {html.escape(snap['source'])}</p>")
    if kind == "codes":
        mil = snap.get("mil_on")
        out.append(f"<p>Check-engine light: <b>{'ON' if mil else 'off' if mil is not None else 'unknown'}</b></p>")
        out.append("<table><tr><th>type</th><th>codes</th></tr>")
        for k in ("stored", "pending", "permanent"):
            v = snap.get(k)
            txt = "no response" if v is None else ", ".join(c["code"] for c in v) or "none"
            out.append(f"<tr><td>{k}</td><td>{html.escape(txt)}</td></tr>")
        out.append("</table>")
        rd = snap.get("readiness") or {}
        if rd:
            out.append("<p>Readiness monitors</p><table>" + "".join(
                f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in rd.items())
                + "</table>")
        ff = snap.get("freeze_frame") or {}
        if ff:
            out.append("<p>Freeze frame 0</p><table>" + "".join(
                f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>" for k, v in ff.items())
                + "</table>")
    elif kind == "monitors":
        out.append("<p>Mode 06 on-board self-test results. Standard misfire TIDs: 0B = average over the "
                   "last ten drive cycles, 0C = this/last drive cycle. Other TIDs are manufacturer-defined; "
                   "the car supplies their units and limits.</p>")
        out.append("<table><tr><th>monitor</th><th>TID</th><th>test</th><th>value</th><th>limits</th><th>result</th></tr>")
        for mid, tests in (snap.get("monitors") or {}).items():
            for t in tests:
                out.append(f"<tr><td>{html.escape(mid.removeprefix('monitor_'))}</td><td>{t['tid']}</td>"
                           f"<td>{html.escape(t['name'])}</td><td>{t['value']} {html.escape(t['unit'])}</td>"
                           f"<td>{t['min']} .. {t['max']}</td><td>{'pass' if t['passed'] else '<b>FAIL</b>'}</td></tr>")
        out.append("</table>")
    else:
        rows = []
        for k, v in snap.items():
            if k in ("kind", "taken", "source"):
                continue
            if k == "supported" and isinstance(v, dict):
                v = "yes: " + ", ".join(c for c, ok in v.items() if ok) + "; no: " + (
                    ", ".join(c for c, ok in v.items() if not ok) or "none")
            elif v is None:
                v = "no response"
            rows.append((k, v))
        out.append("<table>" + "".join(
            f"<tr><td>{html.escape(k)}</td><td>{html.escape(json.dumps(v) if isinstance(v, (dict, list)) else str(v))}</td></tr>"
            for k, v in rows) + "</table>")
    return "\n".join(out)


CSS = """
@page { size: Letter; margin: 16mm 15mm; }
body { font-family: 'DejaVu Sans', sans-serif; color: #0b0b0b; background: #fff; font-size: 10pt; line-height: 1.45; }
h1 { font-size: 18pt; margin: 0 0 4pt; } h2 { font-size: 13pt; margin: 18pt 0 6pt; border-bottom: 1px solid #e4e3df; padding-bottom: 3pt; }
h3 { font-size: 10.5pt; margin: 12pt 0 4pt; }
.meta { color: #52514e; margin-bottom: 10pt; } .note { color: #52514e; font-size: 9pt; }
table { border-collapse: collapse; margin: 4pt 0 8pt; font-size: 9pt; }
td, th { border-bottom: 1px solid #e4e3df; padding: 2pt 8pt 2pt 0; text-align: left; vertical-align: top; }
th { color: #52514e; font-weight: 600; }
img { width: 100%; } code { font-size: 9pt; background: #f3f2ef; padding: 0 2pt; }
.log { break-before: page; }
"""


def build(case):
    import markdown

    case = case.resolve()
    meta, body = records.frontmatter((case / "case.md").read_text())
    vehicle, logs, snaps = records.case_files(case)
    vmeta = records.frontmatter((vehicle / "vehicle.md").read_text())[0] if (vehicle / "vehicle.md").exists() else {}
    out = case / "report"
    figs = out / "figures"
    figs.mkdir(parents=True, exist_ok=True)

    md = markdown.Markdown(extensions=["tables", "fenced_code"])
    parts = [f"<h1>{html.escape(meta.get('title', case.name))}</h1>",
             f"<div class=meta>{html.escape(vmeta.get('title', vehicle.name))} · opened {html.escape(meta.get('opened', ''))}"
             f"{' · closed ' + html.escape(meta['closed']) if meta.get('closed') else ''}"
             f" · status: {html.escape(meta.get('status', ''))} · report built {dt.date.today()}</div>",
             # case.md image paths are relative to case.md; the HTML lives one level down
             re.sub(r'src="(?![a-z]+:|/)', 'src="../', md.convert(body))]

    if snaps:
        parts.append("<h2>Code and link snapshots</h2>")
        parts += [_snapshot_html(json.loads(p.read_text())) for p in snaps]

    for log in logs:
        png = figs / f"{log.name.split('.')[0]}.png"
        plot_log(log, png)
        summary = obd2log.summarize(log, table_rows=0).split("\n", 1)[1]
        parts.append(f"<section class=log><h2>Log {html.escape(log.name.removesuffix('.gz'))}</h2>"
                     f"<img src='figures/{png.name}'>{md.reset().convert(summary)}</section>")

    parts.append("<h2>Appendix: data files</h2><p>Embedded in this PDF as attachments "
                 "(open your PDF viewer's attachments pane) and stored next to it.</p><table>"
                 "<tr><th>file</th><th>rows</th><th>sha256 (first 16)</th></tr>")
    for f in [case / "case.md", *logs, *snaps]:
        n = sum(1 for _ in obd2log.open_log(f)) - 1 if ".csv" in f.suffixes else ""
        parts.append(f"<tr><td>{html.escape(str(f.relative_to(vehicle)))}</td><td>{n}</td>"
                     f"<td><code>{hashlib.sha256(f.read_bytes()).hexdigest()[:16]}</code></td></tr>")
    from importlib.metadata import version
    parts.append(f"</table><p class=note>Generated by obd2log {version('obd2log')}. All data was "
                 "read with read-only OBD-II services (01, 02, 03, 06, 07, 09, 0A, 22); nothing was "
                 "cleared or written to the vehicle.</p>")

    page = (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(meta.get('title', case.name))}"
            f"</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>")
    (out / "report.html").write_text(page)

    pdf = out / "report.pdf"
    try:
        html_pdf(out / "report.html", pdf)
    except FileNotFoundError as e:
        sys.exit(f"wrote {out / 'report.html'}; {e}")

    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(clone_from=PdfReader(pdf))
    for f in [case / "case.md", *logs, *snaps]:
        w.add_attachment(str(f.relative_to(vehicle)).replace("/", "_"), f.read_bytes())
    with pdf.open("wb") as fh:
        w.write(fh)
    print(f"report: {pdf}  ({len(logs)} logs, {len(snaps)} snapshots attached)")
    return pdf


def html_pdf(html_path, pdf):
    """Print an HTML file to PDF with headless Chrome/Chromium."""
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise FileNotFoundError("no Chrome/Chromium found for the PDF")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", Path(html_path).resolve().as_uri()],
                   check=True, capture_output=True)
    return pdf


def markdown_pdf(md_text, pdf, title):
    """Markdown -> HTML (kept next to the PDF) -> PDF, in the case-report style."""
    import markdown
    body = markdown.markdown(md_text, extensions=["tables"])
    css = CSS + "img { width: auto; max-width: 100%; max-height: 135mm; display: block; margin: 0 auto; }"
    page = (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>{body}</body></html>")
    h = Path(pdf).with_suffix(".html")
    h.write_text(page)
    return html_pdf(h, pdf)


def upload(path=None, remote=None):
    """Copy the records repo (or a sub-path of it) to the rclone remote, mirroring its layout."""
    root = records.records_root()
    if not root:
        sys.exit(f"set `records` in {records.CONFIG} first")
    remote = remote or records.config().get("rclone_remote")
    if not remote:
        sys.exit(f"set `rclone_remote` in {records.CONFIG} or pass --remote")
    if not shutil.which("rclone"):
        sys.exit("rclone not found")
    rname = remote.split(":", 1)[0] + ":"
    have = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
    if rname not in have:
        sys.exit(f"no rclone remote {rname!r}. Set one up with:\n"
                 f"  rclone config create {rname[:-1]} drive scope=drive.file")
    src = (Path(path).resolve() if path else root)
    rel = src.relative_to(root)
    dest = remote.rstrip("/") + ("" if str(rel) == "." else f"/{rel}")
    subprocess.run(["rclone", "copy", str(src), dest, "--exclude", ".git/**", "--exclude", ".githooks/**",
                    "--exclude", "report.html", "-v"], check=True)
    print(f"uploaded {src} -> {dest}")
