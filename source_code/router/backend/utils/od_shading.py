"""Cell highlighting settings for the Orders & Deliveries page.

Edited in the page's own Settings popup (Highlighting tab) and saved in
kpi_layout under 'od_shading'. The defaults reproduce how the page shaded
before these settings existed.
"""
import re

KPI_KEY = 'od_shading'

DEFAULTS = {
    # Column key -> {direction, scheme, and that scheme's own colours}.
    # direction: 'high-good' (higher is better) or 'high-bad' (higher is worse).
    # scheme: 'default' uses the colours below; 'rag' or 'single' use the
    # column's own rag_low/rag_mid/rag_high or single_colour.
    # Columns not listed are not shaded.
    'columns': {
        'ack_dis': {'direction': 'high-bad', 'scheme': 'default'},
        'dts': {'direction': 'high-bad', 'scheme': 'default'},
        'chassis_mgn_aft_subs': {'direction': 'high-good', 'scheme': 'default'},
        'gpu_m': {'direction': 'high-good', 'scheme': 'default'},
        'gpu_ms': {'direction': 'high-good', 'scheme': 'default'},
        'at_rating': {'direction': 'high-good', 'scheme': 'default'},
    },
    # The default scheme, used by every column set to 'default'.
    # 'rag' = red, amber, green.  'single' = pale to strong shades of one colour.
    'scheme': 'rag',
    # Same values as --shade-low / --shade-mid / --shade-high in theme.css.
    'rag_low': '#d66e6f', 'rag_mid': '#fff3b4', 'rag_high': '#63be7b',
    'single_colour': '#3b82f6',
    # Standard deviations from the column's average that reach full colour.
    # Lower = stronger colours, higher = more of the column stays mid-colour.
    'strength': 1.5,
    # DIS and DTS above this many days are treated as bad data and left
    # uncoloured. 0 turns the cap off.
    'day_cap': 365,
    # Tables with fewer rows than this are left uncoloured.
    'min_rows': 2,
    'shade_sales_type': False,
    'shade_sub_rows': True,
    'shade_popup': True,
    # On: colours are scaled against every vehicle in the selected date range
    # and supplier channel, ignoring the other filters, so a poor slice reads
    # red rather than its best row reading green and each channel is judged
    # against itself. Off: scaled against the rows on screen.
    'shade_scale_all': False,
}

_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')
_KEY = re.compile(r'^[a-z0-9_]{1,60}$')


def validate(s) -> str | None:
    if not isinstance(s, dict):
        return "Highlighting settings must be sent as an object"
    cols = s.get('columns')
    if not isinstance(cols, dict):
        return "Highlighted columns are missing"
    for k, c in cols.items():
        if not isinstance(k, str) or not _KEY.match(k):
            return f"Unknown column '{k}'"
        if not isinstance(c, dict):
            return f"Column '{k}' settings are missing"
        if c.get('direction') not in ('high-good', 'high-bad'):
            return f"Column '{k}' needs Higher Is Better or Higher Is Worse"
        if c.get('scheme') not in ('default', 'rag', 'single'):
            return f"Column '{k}' needs Use Default, Red Amber Green or Single Colour"
        need = {'rag': ('rag_low', 'rag_mid', 'rag_high'), 'single': ('single_colour',)}.get(c['scheme'], ())
        for ck in need:
            if not isinstance(c.get(ck), str) or not _HEX.match(c[ck]):
                return f"Column '{k}' colours must be picked from the colour boxes"
    if s.get('scheme') not in ('rag', 'single'):
        return "Colour scheme must be Red Amber Green or Single Colour"
    for k in ('rag_low', 'rag_mid', 'rag_high', 'single_colour'):
        if not isinstance(s.get(k), str) or not _HEX.match(s[k]):
            return "Colours must be picked from the colour boxes"
    strength = s.get('strength')
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0.25 <= strength <= 5:
        return "Colour Strength must be between 0.25 and 5"
    for k, lo, hi, name in (('day_cap', 0, 5000, 'Day Cap'), ('min_rows', 2, 100, 'Minimum Rows')):
        v = s.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return f"{name} must be a whole number from {lo} to {hi}"
    for k in ('shade_sales_type', 'shade_sub_rows', 'shade_popup', 'shade_scale_all'):
        if not isinstance(s.get(k), bool):
            return "Tick boxes must be on or off"
    return None


_COLOUR_KEYS = {'rag': ('rag_low', 'rag_mid', 'rag_high'), 'single': ('single_colour',)}


def clean(s: dict) -> dict:
    out = {k: s[k] for k in DEFAULTS}
    # Keep only the colours that belong to each column's chosen scheme.
    out['columns'] = {
        k: {'direction': c['direction'], 'scheme': c['scheme'],
            **{ck: c[ck] for ck in _COLOUR_KEYS.get(c['scheme'], ())}}
        for k, c in s['columns'].items()
    }
    return out


def _upgrade(saved: dict) -> dict:
    """Settings saved before per-column schemes held a plain direction string
    per column; read those as 'use the default scheme'."""
    cols = saved.get('columns')
    if isinstance(cols, dict):
        saved = {**saved, 'columns': {
            k: ({'direction': c, 'scheme': 'default'} if isinstance(c, str) else c)
            for k, c in cols.items()}}
    return saved


def load() -> dict:
    """Saved settings, with any missing key taken from the defaults."""
    from routers import kpi
    try:
        saved = kpi._read_kpi_layout_value(KPI_KEY)
    except Exception:
        saved = None
    if isinstance(saved, dict):
        saved = _upgrade(saved)
        merged = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
        if validate(merged) is None:
            return merged
    return clean(DEFAULTS)
