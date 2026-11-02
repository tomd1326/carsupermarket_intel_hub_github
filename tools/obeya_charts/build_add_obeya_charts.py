"""Builds add_obeya_charts.js from add_obeya_charts.template.js, with the
Daily OBEYA Targets workbook's targets put in.

Each target column of the workbook's cs_daily sheet that the template names
is read from the figures Excel last saved, and stored as runs of dates with
the same figure ([first date, last date, figure]), so a target that never
changes is one run and becomes a fixed target on the page.

Run from this folder, with the workbook closed or saved:
    python build_add_obeya_charts.py "C:/Users/tom.drayson/OneDrive - Motor Depot/Car Supermarket Operational Data - Reports/Daily OBEYA Targets v2.xlsx"
"""

import datetime
import json
import re
import sys
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "add_obeya_charts.template.js"
OUTPUT = HERE / "add_obeya_charts.js"
PLACEHOLDER = "{} /*TARGET_RUNS*/"
SHEET = "cs_daily"
HEADER_ROW = 3


def template_targets(text: str) -> list:
    """The cs_daily header names the template's charts use as targets."""
    names = re.findall(r"target: '([^']+)'", text) + re.findall(r"fallback: '([^']+)'", text)
    return sorted(set(names))


def target_runs(workbook: Path, headers: list) -> dict:
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    rows = list(wb[SHEET].iter_rows(min_row=HEADER_ROW, values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    missing = [h for h in headers if h not in header]
    if missing:
        raise SystemExit(f"Not found in {SHEET} row {HEADER_ROW}: {', '.join(missing)}")
    days = [r for r in rows[1:] if isinstance(r[0], (datetime.date, datetime.datetime))]
    out = {}
    for name in headers:
        col = header.index(name)
        runs = []
        for r in days:
            day = (r[0].date() if isinstance(r[0], datetime.datetime) else r[0]).isoformat()
            value = r[col]
            value = round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            if runs and runs[-1][2] == value:
                runs[-1][1] = day
            else:
                runs.append([day, day, value])
        out[name] = [run for run in runs if run[2] is not None]
    return out


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    text = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        raise SystemExit(f"{TEMPLATE.name} has lost its {PLACEHOLDER} line")
    runs = target_runs(Path(sys.argv[1]), template_targets(text))
    built = text.replace(PLACEHOLDER, json.dumps(runs, separators=(",", ":")))
    built = built.replace("// add_obeya_charts.js is built from this file", "// Built from add_obeya_charts.template.js")
    OUTPUT.write_text(built, encoding="utf-8")
    for name, r in runs.items():
        print(f"{name}: {'none' if not r else 'fixed ' + str(r[0][2]) if len(r) == 1 else f'{len(r)} changes'}")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
