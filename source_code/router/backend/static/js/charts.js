/* ─────────────────────────────────────────────────────────────────────────
   Shared code for the chart pages (KPI Dashboard, Google Ads Campaigns).

   Load AFTER Chart.js and BEFORE the page's own script:

       <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
       <script src="{{ static_url('js/charts.js') }}"></script>

   Holds everything the chart pages have in common: small helpers, y-axis
   scaling, the green/red target background, value labels, the chart card
   HTML and the day-window scroller. Change these HERE so every chart page
   stays the same. Pair with static/css/charts.css.
   ───────────────────────────────────────────────────────────────────────── */

// Disable animations globally for all charts
Chart.defaults.animation = false;

const DAY_NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const BG_GREEN = 'rgba(147, 196, 125, 0.30)';
const BG_RED   = 'rgba(224, 102, 102, 0.30)';
// Dashed N-1 (same period last year) line. Matches .legend-dot-dashed in charts.css.
const N1_COLOUR = '#6b7280';

function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// "2026-09-11" -> "Thu 11/09"
function dateLabel(d) {
    const dt = new Date(d + 'T00:00:00');
    const dd = String(dt.getDate()).padStart(2, '0');
    const mm = String(dt.getMonth() + 1).padStart(2, '0');
    return DAY_NAMES[dt.getDay()] + ' ' + dd + '/' + mm;
}

const hasValues = arr => !!arr && arr.some(v => v !== null && v !== undefined);

// ── Y-range helpers ─────────────────────────────────────────────────────────
const _cleanVals = arr => (arr || []).filter(v => v !== null && v !== undefined && !isNaN(v));
const _mean = a => a.reduce((x, y) => x + y, 0) / a.length;

function _roundTick(value, unit) {
    const decimals = Math.max(0, -Math.floor(Math.log10(unit)) + 2);
    return Number(value.toFixed(decimals));
}

function _niceUnit(span) {
    const rawStep = span / 8;
    const pow = Math.pow(10, Math.floor(Math.log10(rawStep)));
    return [1, 2, 5, 10].map(m => m * pow).find(u => u >= rawStep) || 10 * pow;
}

// Zero-based y-range.
//
// A line sits mid-chart (yMax = 2 x mean), which leaves room above and below
// for the shape of the line to read. Bars are drawn from zero, so that rule
// wastes the top half of the chart whenever the data is fairly flat; pass
// fromMax for bars, which scales from the max with a little headroom so the
// tallest column nearly fills the plot.
//
// max * 1.05 keeps the largest point off the top edge, and minYMax stops an
// all-zero series reaching _niceUnit(0), which returns 0 and produces an
// unrenderable range. Assumes a non-negative series — a metric that can go
// negative would be clipped at the zero baseline.
function zeroBasedRange(values, minYMax = 1, wholeNumbers = false, fromMax = false) {
    const vals = _cleanVals(values);
    const span = vals.length
        ? (fromMax
            ? Math.max(minYMax, Math.max(...vals) * 1.05)
            : Math.max(minYMax, _mean(vals) * 2, Math.max(...vals) * 1.05))
        : minYMax;
    let unit = _niceUnit(span);
    if (wholeNumbers) unit = Math.max(1, Math.round(unit));
    const yMax = Math.max(unit, Math.ceil(span / unit) * unit);
    // Re-round to the unit's own precision: ceil(span/unit)*unit leaves
    // floating-point noise (0.35000000000000003) on fractional units.
    const decimals = Math.max(0, -Math.floor(Math.log10(unit)) + 2);
    return { yMin: 0, yMax: Number(yMax.toFixed(decimals)), unit };
}

// Y-range that starts at yMin instead of 0. The top is worked out the same
// way as zeroBasedRange, but from the data ABOVE the minimum, so raising the
// minimum stretches the part of the chart where the line actually moves.
// A blank or zero minimum is just zeroBasedRange.
function offsetRange(values, yMin = null, minYMax = 1, wholeNumbers = false, fromMax = false) {
    if (!yMin) return zeroBasedRange(values, minYMax, wholeNumbers, fromMax);
    const above = _cleanVals(values).map(v => Math.max(0, v - yMin));
    // minYMax only guards an all-zero series. Applied to data a little above
    // the minimum it would set the height instead: a ratio 0.12 above a 0.90
    // minimum would get an axis to 1.90, squashing the line at the bottom.
    const floor = above.some(v => v > 0) ? 0 : minYMax;
    const r = zeroBasedRange(above, floor, wholeNumbers, fromMax);
    const decimals = Math.max(0, -Math.floor(Math.log10(r.unit)) + 2);
    return { yMin, yMax: Number((yMin + r.yMax).toFixed(decimals)), unit: r.unit };
}

// ── Green/red target background ─────────────────────────────────────────────
// Green above the target, red below; invert swaps them (lower is better).
// Whole chart green where there is no target at all. Only values > 0 count
// as a target, so a zero target draws as no target.
//
// The target is read off chart.$bgTarget, so scrolling the day window can
// replace it in place. initialTarget covers the first draw, which happens
// inside new Chart() before the caller can set $bgTarget — without it the
// chart is drawn white until the next redraw.
function makeBgPlugin(invert = false, initialTarget = null) {
    return {
        id: 'bgShading',
        beforeDatasetsDraw(chart) {
            const { ctx, chartArea: { left, right, top, bottom }, scales: { x, y } } = chart;
            const targetArr = chart.$bgTarget || initialTarget || [];
            const n = targetArr.length;
            if (!n) return;

            const anyTarget = targetArr.some(t => t !== null && t !== undefined && t > 0);
            if (!anyTarget) {
                ctx.save();
                ctx.fillStyle = BG_GREEN;
                ctx.fillRect(left, top, right - left, bottom - top);
                ctx.restore();
                return;
            }

            const aboveColor = invert ? BG_RED : BG_GREEN;
            const belowColor = invert ? BG_GREEN : BG_RED;

            const points = targetArr.map((t, i) => {
                const px = x.getPixelForValue(i);
                const hasTarget = t !== null && t !== undefined && t > 0;
                const tpx = hasTarget
                    ? Math.min(Math.max(y.getPixelForValue(t), top), bottom)
                    : top;   // gaps in the target read as above-target
                return { px, tpx };
            });

            ctx.save();

            // Below-target area: bottom edge to the target line
            ctx.beginPath();
            ctx.moveTo(points[0].px, bottom);
            points.forEach(p => ctx.lineTo(p.px, p.tpx));
            ctx.lineTo(points[n - 1].px, bottom);
            ctx.closePath();
            ctx.fillStyle = belowColor;
            ctx.fill();

            // Above-target area: target line to the top edge
            ctx.beginPath();
            ctx.moveTo(points[0].px, top);
            points.forEach(p => ctx.lineTo(p.px, p.tpx));
            ctx.lineTo(points[n - 1].px, top);
            ctx.closePath();
            ctx.fillStyle = aboveColor;
            ctx.fill();

            ctx.restore();
        }
    };
}

// ── Value labels on the data points ─────────────────────────────────────────
// getAngle returns the tilt in degrees (read at draw time, so a settings
// change only needs a redraw). Tilted labels are anchored at the point so
// they rise away from it: negative runs up to the right, positive up to the left.
function makeDataLabelPlugin(id, dsLabel, fontSize, fmt, getAngle = () => 0) {
    return {
        id,
        afterDatasetsDraw(chart) {
            const dsIdx = chart.data.datasets.findIndex(d => d.label === dsLabel);
            if (dsIdx < 0) return;
            const meta = chart.getDatasetMeta(dsIdx);
            const angle = Number(getAngle()) || 0;
            const c2 = chart.ctx;
            c2.save();
            c2.font = `bold ${fontSize}px DM Sans, system-ui, sans-serif`;
            c2.fillStyle = '#0f172a';
            meta.data.forEach((point, i) => {
                const v = chart.data.datasets[dsIdx].data[i];
                if (v === null || v === undefined) return;
                c2.save();
                c2.translate(point.x, point.y - 6);
                if (angle) {
                    c2.rotate(angle * Math.PI / 180);
                    c2.textAlign = angle < 0 ? 'left' : 'right';
                    c2.textBaseline = 'middle';
                } else {
                    c2.textAlign = 'center';
                    c2.textBaseline = 'bottom';
                }
                c2.fillText(fmt(v), 0, 0);
                c2.restore();
            });
            c2.restore();
        }
    };
}

// ── Chart HTML ──────────────────────────────────────────────────────────────

// One chart card: heading, legend line, plot. dayScroller puts the day-window
// scroller on the legend line. Ids may be omitted when the page does not need
// to reach that element.
// legend: false drops the legend line and puts the day scroller on the
// title line instead, so the plot gets the freed height (KPI page).
// subtitle, if given, sits under the title in smaller grey text (no-legend cards only).
function chartCardHtml({ canvasId, titleId = '', title = '', subtitle = '', legendId = '', dayScroller = false, legend = true }) {
    if (!legend) {
        return `
        <div class="chart-card">
            <div class="chart-card-head">
                <h2${titleId ? ` id="${titleId}"` : ''}>${escHtml(title)}</h2>
                ${dayScroller ? DayWindow.scrollerHtml() : ''}
            </div>
            ${subtitle ? `<div class="chart-card-subtitle">${escHtml(subtitle)}</div>` : ''}
            <div class="chart-wrap">
                <canvas id="${canvasId}"></canvas>
            </div>
        </div>`;
    }
    return `
        <div class="chart-card">
            <h2${titleId ? ` id="${titleId}"` : ''}>${escHtml(title)}</h2>
            <div class="chart-legend-row">
                <div class="chart-legend"${legendId ? ` id="${legendId}"` : ''}></div>
                ${dayScroller ? DayWindow.scrollerHtml() : ''}
            </div>
            <div class="chart-wrap">
                <canvas id="${canvasId}"></canvas>
            </div>
        </div>`;
}

// Title bar above a row of charts. buttonHtml is the page's own button, if any.
// subtitle, if given, sits under the title in smaller grey text.
function chartRowTitleHtml(title, buttonHtml = '', extraClass = '', subtitle = '') {
    const text = subtitle
        ? `<span class="chart-title-text">${escHtml(title)}<span class="chart-subtitle-text">${escHtml(subtitle)}</span></span>`
        : `<span class="chart-title-text">${escHtml(title)}</span>`;
    return `
        <h3 class="chart-row-title${extraClass ? ' ' + extraClass : ''}">
            ${text}${buttonHtml}
        </h3>`;
}

// Legend items: the series, then N-1 and Target when they are drawn.
function chartLegendHtml(title, showN1, showTarget) {
    const items = [`<div class="legend-item"><span class="legend-dot"></span> ${escHtml(title)}</div>`];
    if (showN1) items.push('<div class="legend-item"><span class="legend-dot-dashed"></span> N-1</div>');
    if (showTarget) items.push('<div class="legend-item"><span class="legend-target"></span> Target</div>');
    return items.join('');
}

// ── Day window ──────────────────────────────────────────────────────────────
// Day charts are sent their whole history; only a window of it is plotted.
// All day charts on a page share one window and move together.
//
// A day chart opts in with makeDayChart(): it is built from the window's
// slice, and keeps the full arrays on chart.$dayFull so scrolling only
// repoints the existing chart at a new slice — nothing is rebuilt, so
// dragging stays smooth.
//
// The page calls DayWindow.configure() once, telling it how many days there
// are, how many to show, and what to do when the window moves (usually:
// DayWindow.applyToCharts(...) and update the day headings).
const DayWindow = {
    _getCount: () => 0,
    _getSize: () => 25,
    _onChange: () => {},
    // End index of the window; null = pinned to the latest day, so a reload
    // follows new data.
    offset: null,

    configure({ getCount, getSize, onChange }) {
        if (getCount) this._getCount = getCount;
        if (getSize) this._getSize = getSize;
        if (onChange) this._onChange = onChange;
    },

    count() { return this._getCount(); },

    // Days on screen, never more than there are.
    size() {
        const count = this.count();
        return Math.max(1, Math.min(this._getSize(), count || this._getSize()));
    },

    maxOffset() { return Math.max(0, this.count() - 1); },

    // The earliest end index that still has a full window behind it.
    // Scrolling further left would shrink the window instead of moving it.
    minOffset() { return Math.min(this.size() - 1, this.maxOffset()); },

    endIndex() {
        if (this.offset === null) return this.maxOffset();
        return Math.min(Math.max(this.offset, this.minOffset()), this.maxOffset());
    },

    startIndex() { return Math.max(0, this.endIndex() - this.size() + 1); },

    slice(arr) {
        if (!arr) return arr;
        return arr.slice(this.startIndex(), this.endIndex() + 1);
    },

    // "Mon 18/08 - Thu 11/09" for the dates on screen.
    rangeLabel(dates) {
        if (!dates || !dates.length) return '';
        return `${dateLabel(dates[this.startIndex()])} - ${dateLabel(dates[this.endIndex()])}`;
    },

    reset() { this.offset = null; },

    slide(offset) {
        const previous = this.endIndex();
        this.offset = offset >= this.maxOffset() ? null : Math.max(offset, this.minOffset());
        if (this.endIndex() === previous) return;
        this._onChange();
        this.updateScrollers();
    },

    step(direction) { this.slide(this.endIndex() + direction * this.size()); },

    latest() { this.slide(this.maxOffset()); },

    // Repoint every day chart in `charts` at the current window.
    applyToCharts(charts) {
        charts.forEach(chart => {
            if (!chart || !chart.$dayFull) return;
            const full = chart.$dayFull;
            chart.data.labels = this.slice(full.labels);
            chart.data.datasets.forEach((dataset, i) => { dataset.data = this.slice(full.data[i]); });
            // The background reads this, so it has to move with the line.
            chart.$bgTarget = this.slice(full.target);
            chart.update('none');
        });
    },

    scrollerHtml() {
        return `
            <div class="chart-day-scroller" data-day-scroller style="display:none;">
                <button type="button" class="chart-day-step" data-day-prev title="Earlier">
                    <i class="fas fa-chevron-left"></i>
                </button>
                <div class="chart-day-track" data-day-track>
                    <div class="chart-day-thumb" data-day-thumb></div>
                </div>
                <button type="button" class="chart-day-step" data-day-next title="Later">
                    <i class="fas fa-chevron-right"></i>
                </button>
                <button type="button" class="chart-day-latest" data-day-latest>Latest</button>
            </div>`;
    },

    // Every day chart carries its own copy of the scroller; all are set to
    // the same position.
    updateScrollers() {
        const count = this.count();
        const scrollers = document.querySelectorAll('[data-day-scroller]');
        // Nothing to scroll when everything already fits on one screen.
        if (!count || count <= this.size()) {
            scrollers.forEach(s => { s.style.display = 'none'; });
            return;
        }
        const start = this.startIndex();
        const end = this.endIndex();
        // The thumb spans the visible days as a fraction of the whole history,
        // so its width shows how much of the history is on screen.
        const left = (start / count) * 100;
        const width = (this.size() / count) * 100;
        scrollers.forEach(scroller => {
            scroller.style.display = '';
            const thumb = scroller.querySelector('[data-day-thumb]');
            thumb.style.left = left + '%';
            thumb.style.width = width + '%';
            scroller.querySelector('[data-day-prev]').disabled = start <= 0;
            scroller.querySelector('[data-day-next]').disabled = end >= this.maxOffset();
        });
    },

    // ── Dragging the thumb ──
    _drag: null,

    _trackToOffset(track, clientX, grabOffsetPx) {
        const rect = track.getBoundingClientRect();
        const count = this.count();
        const size = this.size();
        const thumbWidth = rect.width * (size / count);
        // Position of the thumb's left edge, in track pixels.
        const leftPx = clientX - rect.left - grabOffsetPx;
        const usable = Math.max(1, rect.width - thumbWidth);
        const fraction = Math.min(Math.max(leftPx / usable, 0), 1);
        // Fraction maps onto the range of valid START days, then to an end index.
        const startDay = Math.round(fraction * Math.max(0, count - size));
        return startDay + size - 1;
    },
};

// Build a line/bar chart whose x-axis is the day window. labels, every
// dataset's data and target are the FULL day arrays; the chart is created
// from the current slice and keeps the full arrays for scrolling.
// `build(labels, datasets, target)` creates and returns the Chart from the
// sliced arrays; the caller supplies it so each page keeps its own options.
function makeDayChart(labels, datasets, target, build) {
    const full = { labels, target, data: datasets.map(ds => ds.data) };
    datasets.forEach(ds => { ds.data = DayWindow.slice(ds.data); });
    const chart = build(DayWindow.slice(labels), datasets, DayWindow.slice(target));
    chart.$dayFull = full;
    return chart;
}

// Scroller buttons and dragging are handled once on the document: the
// scrollers are rebuilt with their rows, so per-element listeners would have
// to be rebound each time. Pointer events (not mouse) so a drag keeps working
// when the pointer leaves the track; capture means we still get the release.
document.addEventListener('click', event => {
    if (event.target.closest('[data-day-prev]')) DayWindow.step(-1);
    else if (event.target.closest('[data-day-next]')) DayWindow.step(1);
    else if (event.target.closest('[data-day-latest]')) DayWindow.latest();
});

document.addEventListener('pointerdown', event => {
    const track = event.target.closest('[data-day-track]');
    if (!track || !DayWindow.count()) return;
    const thumb = track.querySelector('[data-day-thumb]');
    const thumbRect = thumb.getBoundingClientRect();
    // Grabbing the thumb keeps the point under the cursor; clicking the bare
    // track jumps so the thumb centres on the click.
    const grabOffsetPx = event.target === thumb ? event.clientX - thumbRect.left : thumbRect.width / 2;
    DayWindow._drag = { track, grabOffsetPx };
    track.classList.add('dragging');
    track.setPointerCapture(event.pointerId);
    DayWindow.slide(DayWindow._trackToOffset(track, event.clientX, grabOffsetPx));
    event.preventDefault();
});

document.addEventListener('pointermove', event => {
    const drag = DayWindow._drag;
    if (!drag) return;
    DayWindow.slide(DayWindow._trackToOffset(drag.track, event.clientX, drag.grabOffsetPx));
    event.preventDefault();
});

function _endDayDrag() {
    if (!DayWindow._drag) return;
    DayWindow._drag.track.classList.remove('dragging');
    DayWindow._drag = null;
}
document.addEventListener('pointerup', _endDayDrag);
document.addEventListener('pointercancel', _endDayDrag);
