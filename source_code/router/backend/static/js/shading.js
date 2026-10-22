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

/* Builds a shading function for one column, centred on that column's own mean
   across the rows given: the mean is amber, and values shade toward red or
   green in proportion to how many standard deviations they sit from it. A
   column whose values are all close together therefore stays mostly amber,
   rather than having its extremes forced to full red and full green.
     rows     — the rows the table is about to draw
     getValue — pulls the column's value out of a row
     options.inScale — optional (value) => boolean deciding which values SET the
        min and max, AND which values get a colour at all. Use it to keep known
        anomalies out of the scale entirely: one 900-day vehicle otherwise
        pushes every real value to the good end, so the whole column reads
        green. Values failing this test are left UNSHADED (no background), not
        clamped to an end of the scale — an anomaly is not "the worst case",
        it is a data problem, and colouring it as though it were on the same
        scale as real values is misleading.
   Returns (value, direction) => css colour string ('' when not shadeable).

   Returns a no-op shader when there are fewer than two known values, when
   every value is identical, or when the standard deviation is zero: with
   nothing to compare against, colouring cells would imply a spread that is
   not there. A two- or three-row table is usually better left unshaded
   entirely for the same reason — across three rows the shading only really
   marks "biggest of three". */
function shadeScale(rows, getValue, options) {
    const inScale = (options && options.inScale) || null;
    const all = rows
        .map(getValue)
        .filter(v => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v)))
        .map(Number);
    const vals = inScale ? all.filter(inScale) : all;
    if (vals.length < 2) return () => '';
    const min = Math.min(...vals), max = Math.max(...vals);
    if (max === min) return () => '';

    // Anchor the amber midpoint on the MEAN, not halfway between min and max,
    // and measure each value in standard deviations from it.
    //
    // Scaling edge-to-edge (ratio = (v - min) / (max - min)) forces the lowest
    // value to full red and the highest to full green no matter how close
    // together they are: a column spanning £1,542..£1,694 — a 10% spread —
    // came out as one saturated red row and one saturated green row, with
    // almost nothing in between, because only a value landing near the exact
    // centre ever reached amber. That reads as "worst vs best" when it should
    // read "all much of a muchness".
    //
    // Against the mean, a value one SPREAD_SDS deviation away hits full
    // colour and everything nearer sits in the amber band in proportion, so a
    // tight column stays mostly yellow and only genuine outliers saturate.
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const variance = vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length;
    const sd = Math.sqrt(variance);
    if (!sd) return () => '';
    // options.spreadSds and options.colour (ratio -> css colour) let a page
    // use its own saved strength and colours; other pages get the defaults.
    const SPREAD_SDS = (options && options.spreadSds) || 1.5;
    const colourOf = (options && options.colour) || shadeColour;

    return (value, direction) => {
        if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '';
        // Outside the scale means no colour at all, not a clamped one.
        if (inScale && !inScale(Number(value))) return '';
        // 0.5 = mean (amber), 0 = SPREAD_SDS below, 1 = SPREAD_SDS above.
        const ratio = 0.5 + (Number(value) - mean) / (sd * SPREAD_SDS * 2);
        return colourOf(direction === 'high-bad' ? 1 - ratio : ratio);
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
