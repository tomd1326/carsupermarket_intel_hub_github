"""Moves the saved KPI charts and data sources to the new method. See
utils/kpi_conversion.py for what changes.

Run from source_code/router/backend, with a valid AWS SSO session:

    python scripts/convert_kpi_charts.py                       dry run: writes nothing
    python scripts/convert_kpi_charts.py --apply-after-deploy  converts
    python scripts/convert_kpi_charts.py --undo                puts everything back
    python scripts/convert_kpi_charts.py --repair              converts the charts again,
        for when a page opened before the conversion has saved the old charts back
        over the converted ones. Keeps every chart in the saved layout, new ones too.

The dry run compares every changed metric's daily figures with the old ones in
Snowflake. A source with any figure that differs is left as it is, with its
charts, in the real run as well.

The website must already be running the new code: it shares these saved values
with this script, and older code cannot read a converted chart.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import kpi
from utils import kpi_conversion as conv
from utils import kpi_data_sources as kds
from utils.snowflake_client import SnowflakeClient

BACKUP_KEY = 'kpi_conversion_backup'
KEYS = ('layout', 'data_sources', 'metric_names', 'metric_formats', 'derived_names')
# Figures are rounded by Snowflake differently when summed or averaged.
TOLERANCE = 1e-6


def read_by_date(entry: dict, metrics: list, start, end, columns: dict) -> dict:
    sql, params = kds.fetch_sql({**entry, "table": f"{kpi._VEHICLE_DB}.{entry['table']}"}, metrics, start, end, columns)
    with SnowflakeClient() as sf:
        rows, names = sf.execute(sql, params)
    return kds.rows_to_by_date(entry, metrics, rows, names)


def same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    return abs(a - b) <= TOLERANCE * max(1.0, abs(a), abs(b))


def compare_source(entry: dict, pairs: dict, start, end) -> list:
    """Each difference between the old and new daily figures, as text."""
    columns = kpi._source_columns(entry["table"])
    built = {**entry, "shape": "records"}
    old_metrics = [m for key in pairs for m in (key if isinstance(key, tuple) else (key,))]
    old = read_by_date(entry, old_metrics, start, end, columns)
    new = read_by_date(built, list(pairs.values()), start, end, columns)
    problems = []
    if set(old) != set(new):
        problems.append(f"dates differ: {len(old)} old, {len(new)} new")
    for key, metric in pairs.items():
        wrong, first = 0, None
        for day in sorted(set(old) & set(new)):
            if isinstance(key, tuple):
                total, count = (old[day].get(key[0]), old[day].get(key[1]))
                before = float(total) / float(count) if count else None
            else:
                before = old[day].get(key)
            if not same(before, new[day].get(metric)):
                wrong += 1
                first = first or (day, before, new[day].get(metric))
        label = key if isinstance(key, str) else f"{key[0]} / {key[1]}"
        print(f"    {'OK ' if not wrong else 'DIFF'} {label}" + (f" - {wrong} days differ, first {first}" if wrong else ""))
        if wrong:
            problems.append(label)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Move the KPI charts and data sources to the new method")
    parser.add_argument('--apply-after-deploy', action='store_true', help="write the changes; the website must be on the new code")
    parser.add_argument('--undo', action='store_true', help="put back what was saved before the conversion")
    parser.add_argument('--repair', action='store_true', help="convert the charts again after old charts were saved back")
    args = parser.parse_args()

    stored = kpi._read_kpi_layout_values(list(KEYS) + [BACKUP_KEY])
    if args.repair:
        backup = stored.get(BACKUP_KEY)
        if not backup:
            print("Nothing to repair: no conversion has been run.")
            return 1
        # The old metric names only mean anything to the sources as they were.
        layout = stored.get('layout') or {}
        plan = conv.plan_conversion(layout, backup['data_sources'], stored.get('metric_names') or {},
                                    stored.get('metric_formats') or {}, stored.get('derived_names') or {},
                                    kpi._data_source_names)
        err = kpi._validate_layout(plan['layout'], kpi._kpi_sections(layout), kpi._section_locks(kpi._kpi_sections(layout)))
        if err:
            print(f"The repaired layout fails the page's own check, so nothing was written: {err}")
            return 1
        kpi._write_kpi_layout_values({k: plan[k] for k in ('layout', 'metric_names', 'metric_formats', 'derived_names')},
                                     saved_by="conversion script (repair)")
        print(f"Repaired: {len(plan['layout'].get('rows', []))} charts, {plan['summary']['series_marked']} series marked, "
              f"{len(plan['summary']['charts_on_new_metrics'])} charts back on the new metrics. Reload every KPI page.")
        return 0

    if args.undo:
        backup = stored.get(BACKUP_KEY)
        if not backup:
            print("Nothing to undo: no conversion has been run.")
            return 1
        kpi._write_kpi_layout_values({k: backup[k] for k in KEYS if k in backup}, saved_by="conversion script (undo)")
        print(f"Put back the charts and data sources saved at {backup.get('saved_at')}.")
        return 0

    layout, sources = stored.get('layout') or {}, stored.get('data_sources') or []
    by_id = {s['id']: s for s in sources}
    today = datetime.date.today()
    months, weeks, days, after = kpi._period_args({})
    start, end = kpi._ds_fetch_range(today, months, weeks, days, after)
    print(f"{len(layout.get('rows', []))} charts, {len(sources)} data sources. Comparing {start} to {end}.")

    pairs = conv.metric_pairs(layout, by_id)
    skip = []
    for source_id, source_pairs in sorted(pairs.items()):
        entry = by_id[source_id]
        print(f"  {entry['name']} ({entry['table']}): {len(source_pairs)} metrics")
        try:
            problems = compare_source(entry, source_pairs, start, end)
        except Exception as e:
            problems = [str(e)]
            print(f"    could not compare: {e}")
        if problems:
            skip.append(source_id)
            print(f"    LEFT AS IT IS: {len(problems)} problems")

    plan = conv.plan_conversion(layout, sources, stored.get('metric_names') or {}, stored.get('metric_formats') or {},
                                stored.get('derived_names') or {}, kpi._data_source_names, skip=skip)
    kept = [i for i in plan['summary']['sources_converted']]
    print("\nWould change:" if not args.apply_after_deploy else "\nChanging:")
    print(f"  series marked column or line: {plan['summary']['series_marked']}")
    print(f"  charts moved to new metrics:  {len(plan['summary']['charts_on_new_metrics'])}")
    print(f"  ratios made into averages:    {plan['summary']['ratios_to_averages']}")
    print(f"  data sources made into tables: {', '.join(kept) or 'none'}")
    print(f"  data sources left as they are: {', '.join(skip) or 'none'}")

    err = kpi._validate_layout(plan['layout'], kpi._kpi_sections(layout), kpi._section_locks(kpi._kpi_sections(layout)))
    if err:
        print(f"\nThe converted layout fails the page's own check, so nothing was written: {err}")
        return 1
    if not args.apply_after_deploy:
        print("\nDry run: nothing was written.")
        return 0

    values = {k: plan[k] for k in KEYS}
    if not stored.get(BACKUP_KEY):
        values[BACKUP_KEY] = {**{k: stored[k] for k in KEYS if k in stored},
                              "saved_at": datetime.datetime.now().isoformat(timespec='seconds')}
    kpi._write_kpi_layout_values(values, saved_by="conversion script")
    print("\nDone. --undo puts it all back, or use Settings > Charts > Restore A Previous Layout for the charts alone.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
