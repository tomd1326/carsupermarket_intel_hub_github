/* ─────────────────────────────────────────────────────────────────────────
   Shared conditional-shading helpers for data tables.

       <link href="{{ static_url('css/theme.css') }}" rel="stylesheet">
       <script src="{{ static_url('js/shading.js') }}"></script>

   Colours come from theme.css (--shade-low / --shade-mid / --shade-high), so
   the scale is defined once for every page rather than duplicated per page.

   Two directions:
     'high-good' — high values green, low values red (margins, GPU, ratings)
     'high-bad'  — high values red, low values green (days in stock, days to sell)

   Typical use, per table render:

       const shade = shadeScale(rows, r => r.gpu_m);            // build once
       ...
       cell.style.background = shade(row.gpu_m, 'high-good');   // per cell
   ───────────────────────────────────────────────────────────────────────── */

/* Reads the three scale endpoints out of theme.css. Cached after the first
   call: this hits getComputedStyle, which is far too slow to repeat per cell.
   Falls back to the same values theme.css defines, so a table still shades
   sensibly if the stylesheet has not applied yet. */
let _shadeStopsCache = null;
function shadeStops() {
    if (_shadeStopsCache) return _shadeStopsCache;
    const css = getComputedStyle(document.documentElement);
    const read = (name, fallback) => {
        const raw = css.getPropertyValue(name).trim();
        if (!raw) return fallback;
        const parts = raw.split(',').map(n => Number(n.trim()));
        return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
    };
    _shadeStopsCache = {
        low:  read('--shade-low',  [214, 110, 111]),
        mid:  read('--shade-mid',  [255, 243, 180]),
        high: read('--shade-high', [ 99, 190, 123]),
    };
    return _shadeStopsCache;
}

/* Position on the scale (0 = worst, 1 = best) to an rgb() string.
   Interpolates low -> mid over the first half and mid -> high over the second,
   so the midpoint is the amber rather than a muddy red/green blend. */
function shadeColour(ratio) {
    if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '';
    const { low, mid, high } = shadeStops();
    const r = Math.min(1, Math.max(0, ratio));
    const [from, to, t] = r < 0.5 ? [low, mid, r * 2] : [mid, high, (r - 0.5) * 2];
    const lerp = (a, b, f) => Math.round(a + (b - a) * f);
    return `rgb(${[0, 1, 2].map(i => lerp(from[i], to[i], t)).join(',')})`;
}

/* Builds a shading function for one column, scaled to that column's own
   min..max across the rows given.
     rows     — the rows the table is about to draw
     getValue — pulls the column's value out of a row
     options.inScale — optional (value) => boolean deciding which values SET the
        min and max. Use it to keep known anomalies from stretching the scale:
        one 900-day vehicle otherwise pushes every real value to the good end,
        so the whole column reads green. Excluded values are still coloured —
        they just land at the end of the scale rather than defining it.
   Returns (value, direction) => css colour string ('' when not shadeable).

   Returns a no-op shader when there are fewer than two known values, or when
   every value is identical: with nothing to compare against, colouring cells
   would imply a spread that is not there. That is also why a two- or
   three-row table is usually better left unshaded entirely — min..max across
   three rows only marks "biggest of three". */
function shadeScale(rows, getValue, options) {
    const inScale = (options && options.inScale) || null;
    const all = rows
        .map(getValue)
        .filter(v => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v)))
        .map(Number);
    // Values outside the scale set its endpoints aside but still get coloured,
    // clamped to whichever end they sit past.
    const vals = inScale ? all.filter(inScale) : all;
    if (vals.length < 2) return () => '';
    const min = Math.min(...vals), max = Math.max(...vals);
    if (max === min) return () => '';

    return (value, direction) => {
        if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '';
        const ratio = (Number(value) - min) / (max - min);
        return shadeColour(direction === 'high-bad' ? 1 - ratio : ratio);
    };
}

/* Fixed-zero variant: red below zero, green above, scaled against the largest
   magnitude seen in each direction. Use where zero is a meaningful boundary
   (a shortfall vs a surplus) rather than just the bottom of the range. */
function shadeAroundZero(value, min, max) {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '';
    const v = Number(value);
    const { low, mid, high } = shadeStops();
    const MIN_INTENSITY = 0.25;   // never so pale it reads as unshaded
    const lerp = (a, b, f) => Math.round(a + (b - a) * f);
    const blend = (to, f) => `rgb(${[0, 1, 2].map(i => lerp(255, to[i], f)).join(',')})`;
    if (v < 0 && min < 0) return blend(low,  MIN_INTENSITY + (1 - MIN_INTENSITY) * Math.min(1, v / min));
    if (v > 0 && max > 0) return blend(high, MIN_INTENSITY + (1 - MIN_INTENSITY) * Math.min(1, v / max));
    return v === 0 ? blend(mid, MIN_INTENSITY) : '';
}
