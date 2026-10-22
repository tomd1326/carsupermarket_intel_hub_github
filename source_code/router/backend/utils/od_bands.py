"""Price, mileage and age bands for the Orders & Deliveries page.

The bands used to be fixed CASE statements in dbt. They are now worked out
here from the raw figures on each row, using limits the admin edits in
Settings > Orders & Deliveries Bands (saved in kpi_layout under 'od_bands').

The defaults reproduce the dbt bands exactly, including which side of a limit a
vehicle sitting right on it falls:
  price    up to and including the limit   (<=)   as in vehicles__c.sql
  mileage  below the limit                 (<)    as in vehicles__c.sql
  age      below the limit, in 365-day years (<)  as in sales__c.sql
"""
import datetime

KPI_KEY = 'od_bands'

DEFAULT_LIMITS = {
    'price': [10000, 12500, 15000, 20000, 25000, 30000],
    'mileage': [10000, 20000, 30000, 40000, 50000, 60000, 70000],
    'age': [1, 2, 3, 4, 5, 6, 7, 8],
}

BAND_TITLES = {'price': 'Price Band', 'mileage': 'Mileage Band', 'age': 'Age Band'}

# Shown after the price bands for 0% finance deals, as dbt does.
INTEREST_FREE_LABEL = 'Interest Free'


def _num(v) -> str:
    """12500 -> '12.5k', 10000 -> '10k'."""
    k = round(v / 1000, 2)
    return (f"{k:.2f}".rstrip('0').rstrip('.')) + 'k'


def _plain(v) -> str:
    return f"{v:g}"


def labels(kind: str, limits: list) -> list:
    """Band labels in order, one more than the number of limits."""
    out = []
    if kind == 'age':
        first = limits[0]
        out.append(f"<{_plain(first)} year" + ('' if first == 1 else 's'))
        for lo, hi in zip(limits, limits[1:]):
            out.append(f"{_plain(lo)}-{_plain(hi)} years")
        out.append(f"{_plain(limits[-1])} years+")
        return out
    out.append(f"<{_num(limits[0])}")
    for lo, hi in zip(limits, limits[1:]):
        out.append(f"{_num(lo)[:-1]}-{_num(hi)}")
    out.append(f"{_num(limits[-1])}+")
    return out


def validate(limits_by_kind) -> str | None:
    """Error text, or None when the limits are usable."""
    if not isinstance(limits_by_kind, dict):
        return "Bands must be sent as an object"
    for kind in DEFAULT_LIMITS:
        vals = limits_by_kind.get(kind)
        title = BAND_TITLES[kind]
        if not isinstance(vals, list) or not vals:
            return f"{title} needs at least one limit"
        if len(vals) > 20:
            return f"{title} has too many limits (20 at most)"
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return f"{title} limits must be numbers"
            if v <= 0:
                return f"{title} limits must be above zero"
        if kind == 'age' and any(v != int(v) for v in vals):
            return "Age Band limits must be whole years"
        if any(b <= a for a, b in zip(vals, vals[1:])):
            return f"{title} limits must go up, each larger than the one before"
    return None


def clean(limits_by_kind: dict) -> dict:
    """Whole numbers stored as ints so they read cleanly."""
    return {k: [int(v) if float(v) == int(v) else float(v) for v in limits_by_kind[k]]
            for k in DEFAULT_LIMITS}


def load_limits() -> dict:
    """Saved limits, falling back to the defaults for anything missing or invalid."""
    from routers import kpi
    try:
        saved = kpi._read_kpi_layout_value(KPI_KEY)
    except Exception:
        saved = None
    if isinstance(saved, dict):
        merged = {k: saved.get(k) or DEFAULT_LIMITS[k] for k in DEFAULT_LIMITS}
        if validate(merged) is None:
            return clean(merged)
    return {k: list(v) for k, v in DEFAULT_LIMITS.items()}


def _index_below(value, limits):
    for i, lim in enumerate(limits):
        if value < lim:
            return i
    return len(limits)


def _index_up_to(value, limits):
    for i, lim in enumerate(limits):
        if value <= lim:
            return i
    return len(limits)


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def apply(rows: list, limits: dict) -> dict:
    """Set price_band, mileage_band and age_band on every row, in place.
    Returns the label order for each, for the slicers."""
    price_l = labels('price', limits['price'])
    mile_l = labels('mileage', limits['mileage'])
    age_l = labels('age', limits['age'])
    age_days = [int(y * 365) for y in limits['age']]

    for r in rows:
        # Mileage: 0 or blank has no band.
        m = _f(r.get('mileage'))
        r['mileage_band'] = mile_l[_index_below(m, limits['mileage'])] if m else None

        # Age: days from first registration to the order date.
        age = None
        dfr, od = r.get('dfr'), r.get('order_date')
        if dfr and od:
            try:
                days = (datetime.date.fromisoformat(od[:10]) - datetime.date.fromisoformat(dfr[:10])).days
                age = age_l[_index_below(days, age_days)]
            except ValueError:
                age = None
        r['age_band'] = age

        # Price: the vehicle's selling price; if it has none, the sale price
        # after discount. Cancelled orders have no band, 0% deals are their own.
        if r.get('invoice_status') == 'STATUS_CANCELLED':
            r['price_band'] = None
        elif r.get('sales_type_bucket') == '0%':
            r['price_band'] = INTEREST_FREE_LABEL
        else:
            p = _f(r.get('vehicle_selling_price'))
            if not p:
                p = _f(r.get('sale_price_after_discount'))
            if not p:
                r['price_band'] = 'Not priced'
            else:
                r['price_band'] = price_l[_index_up_to(p, limits['price'])]

    return {
        'price_band': price_l + [INTEREST_FREE_LABEL, 'Not priced'],
        'mileage_band': mile_l,
        'age_band': age_l,
    }
