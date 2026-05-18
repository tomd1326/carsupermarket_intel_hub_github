"""
Test: fetch Cars Bought and Daily Buyers Sheet from SharePoint.
- Dedup VRMs: DBS priority, then Snowflake stock=1, then Cars Bought remainder
- Use CAP ID from sheet where present; CAP API only for missing ones
- Report "Cars Bought not on Apex" count per (cap_id, price_band)

Run from repo root:
    python source_code/router/backend/tests/test_cars_bought.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils.sharepoint_client import SharePointClient
from list_processor.cap_client import CAPClient
from list_processor.structs import VehicleRow

# ── Sheet config ──────────────────────────────────────────────────────────────

_DBS_SUBFOLDER = "Department/Buying/B2B"
_DBS_FILENAME  = "NEW DAILY BUYERS SHEET.xlsx"
_DBS_SHEET     = "Today"

_CB_SUBFOLDER  = "Cars Bought"
_CB_FILENAME   = "CARS BOUGHT 2026.xlsx"
_CB_SHEET      = "TO GO ON"


class CarsBoughtSharePointClient(SharePointClient):
    TEAM_SITE = "/sites/CarSupermarketOperationalData"


# ── Price band ────────────────────────────────────────────────────────────────

_PRICE_BANDS = [
    (10_000,  "<10k"),
    (12_500,  "10-12.5k"),
    (15_000,  "12.5-15k"),
    (20_000,  "15-20k"),
    (25_000,  "20-25k"),
    (30_000,  "25-30k"),
]
_PRICE_BAND_DEFAULT = "30k+"


def _parse_price(v) -> float | None:
    if v is None or str(v).strip() in ('', 'None'):
        return None
    try:
        return float(str(v).replace('£', '').replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _price_band(rrp_val: float | None, rrp0_val: float | None) -> str:
    if rrp0_val is not None and rrp0_val > 0:
        return "interest_free"
    if rrp_val is None:
        return _PRICE_BAND_DEFAULT
    for ceiling, label in _PRICE_BANDS:
        if rrp_val < ceiling:
            return label
    return _PRICE_BAND_DEFAULT


def _parse_cap_id(v) -> int | None:
    if v is None or str(v).strip() in ('', 'None'):
        return None
    try:
        return int(float(str(v).replace(',', '').strip()))
    except (TypeError, ValueError):
        return None


# ── Sheet parsers ─────────────────────────────────────────────────────────────

def _find_header(values, required_col):
    """Return (header_idx, headers_list) for the first row containing required_col."""
    for i, row in enumerate(values[:5]):
        strs = [str(c).strip() for c in row]
        if required_col in strs:
            return i, strs
    return None, []


def fetch_dbs() -> list[dict]:
    """Fetch Daily Buyers Sheet. Returns list of {vrm, cap_id, rrp, rrp0, price_band}."""
    client = SharePointClient()
    values = client.get_worksheet_values(_DBS_SUBFOLDER, _DBS_FILENAME, _DBS_SHEET)
    header_idx, headers = _find_header(values, 'REG')
    if not headers:
        print("ERROR: DBS header row not found")
        return []
    try:
        reg_idx  = headers.index('REG')
        cap_idx  = headers.index('CAP ID')
        rrp_idx  = headers.index('RRP')
        rrp0_idx = headers.index('RRP 0%')
    except ValueError as e:
        print(f"ERROR: DBS missing column: {e}")
        return []

    rows = []
    for row in values[header_idx + 1:]:
        vrm = str(row[reg_idx]).strip().replace(' ', '').upper() if reg_idx < len(row) and row[reg_idx] not in (None, '') else ''
        if not vrm or vrm in ('NONE', 'N/A'):
            continue
        cap_id   = _parse_cap_id(row[cap_idx]  if cap_idx  < len(row) else None)
        rrp_val  = _parse_price(row[rrp_idx]   if rrp_idx  < len(row) else None)
        rrp0_val = _parse_price(row[rrp0_idx]  if rrp0_idx < len(row) else None)
        rows.append({
            'vrm':        vrm,
            'cap_id':     cap_id,
            'rrp':        rrp_val,
            'rrp0':       rrp0_val,
            'price_band': _price_band(rrp_val, rrp0_val),
            'source':     'dbs',
        })
    return rows


def fetch_cars_bought() -> list[dict]:
    """Fetch Cars Bought sheet. Returns list of {vrm, cap_id, rrp, rrp0, price_band}."""
    client = CarsBoughtSharePointClient()
    values = client.get_worksheet_values(_CB_SUBFOLDER, _CB_FILENAME, _CB_SHEET)
    header_idx, headers = _find_header(values, 'REG')
    if not headers:
        print("ERROR: Cars Bought header row not found")
        return []
    try:
        reg_idx  = headers.index('REG')
        cap_idx  = headers.index('CAP ID')
        rrp_idx  = headers.index('RRP')
        rrp0_idx = headers.index('0%RRP')
    except ValueError as e:
        print(f"ERROR: Cars Bought missing column: {e}")
        return []

    rows = []
    for row in values[header_idx + 1:]:
        vrm = str(row[reg_idx]).strip().replace(' ', '').upper() if reg_idx < len(row) and row[reg_idx] not in (None, '') else ''
        if not vrm or vrm in ('NONE', 'N/A'):
            continue
        cap_id   = _parse_cap_id(row[cap_idx]  if cap_idx  < len(row) else None)
        rrp_val  = _parse_price(row[rrp_idx]   if rrp_idx  < len(row) else None)
        rrp0_val = _parse_price(row[rrp0_idx]  if rrp0_idx < len(row) else None)
        rows.append({
            'vrm':        vrm,
            'cap_id':     cap_id,
            'rrp':        rrp_val,
            'rrp0':       rrp0_val,
            'price_band': _price_band(rrp_val, rrp0_val),
            'source':     'cb',
        })
    return rows


def fetch_apex_vrms() -> set[str]:
    """Return uppercased VRMs from Snowflake buying_requirements where stock=1."""
    from utils.snowflake_client import SnowflakeClient
    with SnowflakeClient() as sf:
        rows, _ = sf.execute(
            "SELECT VRM FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.buying_requirements WHERE STOCK = 1"
        )
    return {row[0].strip().upper() for row in rows if row[0]}


# ── CAP API lookup (only for missing CAP IDs) ─────────────────────────────────

def _lookup_one(cap: CAPClient, r: dict) -> dict:
    try:
        vehicle = VehicleRow(vrm=r['vrm'], mileage=1000)
        resp = cap.vrm_valuation(vehicle)
        cap_id_raw = resp.get('capid')
        cap_id = int(cap_id_raw) if cap_id_raw and cap_id_raw != 'Not Found' else None
        return {**r, 'cap_id': cap_id, 'cap_note': resp.get('note')}
    except Exception as e:
        return {**r, 'cap_id': None, 'cap_note': str(e)}


def enrich_cap_ids(rows: list[dict], workers: int = 25) -> list[dict]:
    """Fill in missing CAP IDs via API. Rows with existing CAP IDs are returned as-is."""
    need_lookup = [r for r in rows if r['cap_id'] is None]
    if not need_lookup:
        return rows

    print(f"  CAP API lookup needed for {len(need_lookup)} VRMs (no CAP ID in sheet)...")
    cap = CAPClient()
    lookup_results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_lookup_one, cap, r): r['vrm'] for r in need_lookup}
        for future in as_completed(futures):
            result = future.result()
            lookup_results[result['vrm']] = result

    return [lookup_results.get(r['vrm'], r) if r['cap_id'] is None else r for r in rows]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching Daily Buyers Sheet, Cars Bought, and Apex VRMs in parallel...")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_dbs   = ex.submit(fetch_dbs)
        f_cb    = ex.submit(fetch_cars_bought)
        f_apex  = ex.submit(fetch_apex_vrms)
        dbs_rows   = f_dbs.result()
        cb_rows    = f_cb.result()
        apex_vrms  = f_apex.result()

    print(f"DBS: {len(dbs_rows)} rows | Cars Bought: {len(cb_rows)} rows | Apex stock=1: {len(apex_vrms)} VRMs")

    # Step 1: DBS is priority — keep all, collect their VRMs
    dbs_vrms = {r['vrm'] for r in dbs_rows}

    # Step 2: From Cars Bought, remove VRMs already in DBS or on Apex
    excluded_vrms = dbs_vrms | apex_vrms
    cb_deduped = []
    cb_skipped = []
    seen_cb_vrms = set()
    for r in cb_rows:
        if r['vrm'] in excluded_vrms or r['vrm'] in seen_cb_vrms:
            cb_skipped.append(r)
        else:
            cb_deduped.append(r)
            seen_cb_vrms.add(r['vrm'])

    print(f"\nCars Bought after dedup: {len(cb_deduped)} remaining ({len(cb_skipped)} removed — on DBS or Apex)")
    if cb_skipped:
        print(f"  Skipped: {', '.join(r['vrm'] for r in cb_skipped)}")

    # Step 3: Enrich missing CAP IDs via API
    print("\nEnriching Cars Bought CAP IDs...")
    cb_enriched = enrich_cap_ids(cb_deduped)

    have_cap    = [r for r in cb_enriched if r['cap_id'] is not None]
    missing_cap = [r for r in cb_enriched if r['cap_id'] is None]

    # Step 4: Print results
    print(f"\n{'VRM':<12}  {'CAP ID':>8}  {'RRP':>8}  {'0%RRP':>8}  {'Price Band':<14}  {'Note'}")
    print("-" * 72)
    for r in cb_enriched:
        rrp_str  = f"£{r['rrp']:,.0f}"  if r['rrp']  is not None else '-'
        rrp0_str = f"£{r['rrp0']:,.0f}" if r['rrp0'] is not None else '-'
        note = r.get('cap_note') or ''
        src  = '' if r['cap_id'] is not None and not r.get('cap_note') else f"API" if not r.get('cap_note') else note
        print(f"{r['vrm']:<12}  {str(r['cap_id'] or '-'):>8}  {rrp_str:>8}  {rrp0_str:>8}  {r['price_band']:<14}  {src}")

    # Step 5: Count by (cap_id, price_band)
    print(f"\n=== Cars Bought NOT on Apex: {len(have_cap)} vehicles with CAP ID ===")
    counts: dict = {}
    for r in have_cap:
        key = (r['cap_id'], r['price_band'])
        counts[key] = counts.get(key, 0) + 1
    for (cap_id, band), count in sorted(counts.items()):
        print(f"  CAP {cap_id:>8}  {band:<14}  {count} purchase(s)")

    if missing_cap:
        print(f"\nNo CAP ID found for {len(missing_cap)}: {', '.join(r['vrm'] for r in missing_cap)}")


if __name__ == "__main__":
    main()
