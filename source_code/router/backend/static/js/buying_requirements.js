/* ── Status helpers ──────────────────────────────────────── */
const SF_LABEL = '<i class="fas fa-snowflake me-1 text-info"></i>Snowflake';
const SP_LABEL = '<i class="fas fa-file-excel me-1 text-success"></i>SharePoint';

function setStatus(elId, label, state) {
    const el = document.getElementById(elId);
    if (!el) return;
    const icons = {
        idle:    `${label} <span class="text-muted">—</span>`,
        running: `${label} <span class="spinner-border spinner-border-sm ms-1" style="width:.7rem;height:.7rem"></span>`,
        done:    `${label} <i class="fas fa-check-circle text-success ms-1"></i>`,
        error:   `${label} <i class="fas fa-times-circle text-danger ms-1"></i>`,
    };
    el.innerHTML = icons[state] ?? icons.idle;
}

/* ── Column widths ───────────────────────────────────────── */
const COL_UNITS = [
    0.7,  // 0  CAP ID
    2.45, // 1  Make/Model/Deriv/Price Band
    0.55, // 2  View Vehicles
    0.7,  // 3  Fuel
    0.7,  // 4  Total Sales
    0.7,  // 5  7d Sales
    0.7,  // 6  Stock
    0.7,  // 7  Ideal Stock
    0.7,  // 8  Required
    0.7,  // 9  Today's Purchases
    0.7,  // 10 Remaining
    0.7,  // 11 Avg Days in Stock
    0.7,  // 12 Avg Days to Sell
    0.7,  // 13 Avg Sell Price
    0.7,  // 14 Avg AT PTM
    0.7,  // 15 Avg AT PTM %
    0.7,  // 16 Avg AT Rating
    0.7,  // 17 Avg GPU-M
    0.7,  // 18 Avg GPU-S
    0.7,  // 19 Avg GPU-MS
];
const TOTAL_UNITS = COL_UNITS.reduce((a, b) => a + b, 0);

// Font / column sizing — scale together
const BASE_FONT_PX  = 11;
const BASE_FIXED_PX = 70;
const BASE_DESC_PX  = 605;
const BASE_OVERRIDE = { 2: 56, 3: 46, 8: 72, 10: 82 }; // View, Fuel narrow, Required, Remaining wider
const FONT_STEP     = 1;  // px per click
const FONT_MIN      = 8;
const FONT_MAX      = 16;

let currentFontPx = BASE_FONT_PX; // 11px default

function scaledFixed()    { return Math.round(BASE_FIXED_PX * currentFontPx / BASE_FONT_PX); }
function scaledDesc()     { return Math.round(BASE_DESC_PX  * currentFontPx / BASE_FONT_PX); }
function scaledOverride(i){ return Math.round((BASE_OVERRIDE[i] ?? BASE_FIXED_PX) * currentFontPx / BASE_FONT_PX); }

function changeFontSize(delta) {
    currentFontPx = Math.min(FONT_MAX, Math.max(FONT_MIN, currentFontPx + delta));
    document.getElementById('fontSizeLabel').textContent = currentFontPx + 'px';
    colWidths = new Array(COL_UNITS.length).fill(null); // reset manual resizes
    applyColWidths();
}

let colWidths = new Array(COL_UNITS.length).fill(null);

const NUM_FIXED_COLS = COL_UNITS.length - 1;

function resetColWidths() {
    colWidths.fill(null);
    const table = document.getElementById('data-table');
    if (table) table.style.width = '';
    requestAnimationFrame(applyColWidths);
}

function applyColWidths() {
    const table = document.getElementById('data-table');
    if (!table) return;
    table.style.fontSize = currentFontPx + 'px';
    const lbl = document.getElementById('fontSizeLabel');
    if (lbl) lbl.textContent = currentFontPx + 'px';
    let cg = table.querySelector('colgroup');
    if (!cg) { cg = document.createElement('colgroup'); table.prepend(cg); }
    cg.innerHTML = '';
    let totalW = 0;
    COL_UNITS.forEach((_, i) => {
        const w = colWidths[i] !== null ? colWidths[i] : (i === 1 ? scaledDesc() : scaledOverride(i));
        totalW += w;
        const col = document.createElement('col');
        col.style.width = w + 'px';
        cg.appendChild(col);
    });
    table.style.width = totalW + 'px';
}

window.addEventListener('resize', applyColWidths);

/* ── Filters ─────────────────────────────────────────────── */
let filterBand = new Set(['All']), filterFuel = new Set(['All']), filterCapId = null;

function applyCapIdFilter() {
    const val = parseInt(document.getElementById('capIdSearch').value, 10);
    filterCapId = isNaN(val) ? null : val;
    const clearBtn = document.getElementById('capIdClearBtn');
    if (clearBtn) clearBtn.style.display = filterCapId !== null ? '' : 'none';
    if (tableData.length) applyFilter();
}

function clearCapIdFilter() {
    filterCapId = null;
    const inp = document.getElementById('capIdSearch');
    if (inp) inp.value = '';
    const clearBtn = document.getElementById('capIdClearBtn');
    if (clearBtn) clearBtn.style.display = 'none';
    if (tableData.length) applyFilter();
}

document.addEventListener('DOMContentLoaded', () => {
    const inp = document.getElementById('capIdSearch');
    if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') applyCapIdFilter(); });
});

const ALL_BANDS = new Set(['<10k','10-12.5k','12.5-15k','15-20k','20-25k','25-30k','30k+','interest_free']);
const ALL_FUELS = new Set(['Diesel','Electric','Hybrid','Other','Petrol']);

const FUEL_NORM = { p:'P', petrol:'P', d:'D', diesel:'D', e:'E', electric:'E', ev:'E', h:'H', hybrid:'H', phev:'H', mhev:'H' };
function normFuel(v) { return FUEL_NORM[String(v ?? '').trim().toLowerCase()] || 'O'; }

function applyFilter() {
    const maxDIS  = getMaxDIS();
    const maxDTS  = getMaxDTS();
    const minGPUM = getMinGPUM();
    const totalSales = rawData.reduce((s, r) => s + (Number(r[5]) || 0), 0);
    const nowMs = Date.now();
    const filteredSales = rawData.reduce((s, r) => {
        const ackDate      = r[17] ? new Date(r[17]) : null;
        const daysSinceAck = ackDate ? Math.floor((nowMs - ackDate.getTime()) / 86400000) : null;
        const dts   = r[8]  != null ? Number(r[8])  : null;
        const gpu_m = r[22] != null ? Number(r[22]) : null;
        const passDIS  = maxDIS  === null || daysSinceAck === null || daysSinceAck <= maxDIS;
        const passDTS  = maxDTS  === null || dts   === null || dts  <= maxDTS;
        const passGPUM = minGPUM === null || gpu_m === null || gpu_m >= minGPUM;
        return (passDIS && passDTS && passGPUM) ? s + (Number(r[5]) || 0) : s;
    }, 0);
    const visibleRows = [];
    document.querySelectorAll('#table-body tr').forEach((tr, i) => {
        const row = tableData[i];
        if (!row) return;
        const band = row[22] ?? String(row[1] ?? '').split(' / ').pop().trim();
        const fuel = normFuel(row[2]);
        const visible = (filterBand.has('All') || filterBand.has(band)) &&
                        (filterFuel.has('All') || filterFuel.has(fuel)) &&
                        (filterCapId === null || Number(row[0]) === filterCapId);
        tr.style.display = visible ? '' : 'none';
        if (visible) visibleRows.push(row);
    });
    const lbl = document.getElementById('filtered-sales-label');
    if (lbl) lbl.textContent = `Filtered ${getMaxDIS() ?? 90}d Sales: ${filteredSales.toLocaleString()} / ${totalSales.toLocaleString()}`;
    renderTotalsRow(visibleRows);
}

document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const group = btn.dataset.group, value = btn.dataset.value;
        const allSet = group === 'band' ? ALL_BANDS : ALL_FUELS;
        const state  = group === 'band' ? filterBand : filterFuel;
        if (value === 'All') { state.clear(); state.add('All'); }
        else {
            state.delete('All');
            if (state.has(value)) state.delete(value); else state.add(value);
            if ([...allSet].every(v => state.has(v)) || state.size === 0) { state.clear(); state.add('All'); }
        }
        document.querySelectorAll(`.filter-btn[data-group="${group}"]`).forEach(b => {
            const on = state.has(b.dataset.value);
            b.classList.toggle('btn-dark', on);
            b.classList.toggle('btn-outline-secondary', !on);
        });
        if (tableData.length) applyFilter();
    });
});

/* ── Sortable table ──────────────────────────────────────── */
let rawData = [], tableData = [], spPurchases = {}, sortCol = null, sortDir = 1;

function getMaxDIS()  { const v = parseFloat(document.getElementById('maxDIS')?.value);  return isNaN(v) ? null : v; }

function updateSalesColumnLabel() {
    const days = getMaxDIS() ?? 90;
    const label = `${days}d Sales`;
    const th = document.getElementById('th-total-sales');
    if (th) th.childNodes[0].textContent = label;
    const summary = document.getElementById('sf-summary-sales-label');
    if (summary) summary.textContent = label + ':';
    const flbl = document.getElementById('filtered-sales-label');
    if (flbl && flbl.textContent) {
        flbl.textContent = flbl.textContent.replace(/Filtered \S+ Sales:/, `Filtered ${label}:`);
    }
}
document.getElementById('maxDIS')?.addEventListener('change', updateSalesColumnLabel);
function getMaxDTS()  { const v = parseFloat(document.getElementById('maxDTS')?.value);  return isNaN(v) ? null : v; }
function getMinGPUM() { const v = parseFloat(document.getElementById('minGPUM')?.value); return isNaN(v) ? null : v; }

/* ── Aggregate raw per-vehicle rows into per-derivative rows ─
 *
 * Raw row layout (from Snowflake):
 *   [0:cap_id, 1:label, 2:fuel, 3:price_band, 4:stock, 5:sales90, 6:sales7,
 *    7:dis, 8:dts, 9:gpu_m, 10:gpu_s, 11:gpu_ms,
 *    12:purchase_price, 13:sell_price, 14:at_val, 15:at_pct, 16:at_rating,
 *    17:ack_date, 18:vrm, 19:supplier_name, 20:vehicle_url, 21:vehicle_status,
 *    22:gpu_m_resolved (COALESCE of sales gpu_m and stock_evolution gpu_m)]
 *
 * Output tableData row layout (same indices used throughout the rest of the JS):
 *   [0:cap_id, 1:label, 2:fuel, 3:stock, 4:sales90, 5:pct(set later),
 *    6:today(0), 7:sales7, 8:avg_dis, 9:avg_dts, 10:avg_gpu_m,
 *    14:avg_sell_price, 15:avg_at_val, 16:avg_at_pct, 17:avg_at_rating]
 *
 * Max DTS / Min GPU-M filters are applied per vehicle BEFORE aggregation.
 * A vehicle is included in the sales-weighted metrics only when it meets both filters.
 * Stock is always summed regardless (we own it regardless of sales quality).
 */
function aggregateRawData() {
    const maxDIS  = getMaxDIS();
    const maxDTS  = getMaxDTS();
    const minGPUM = getMinGPUM();
    const todayMs = Date.now();

    const groups = new Map();
    for (const r of rawData) {
        const key = r[0] + '|' + r[1] + '|' + r[2];
        if (!groups.has(key)) {
            groups.set(key, {
                cap_id: r[0], label: r[1], fuel: r[2], price_band: r[3],
                stock: 0, sales90: 0, sales7: 0,
                dis_sum: 0, dis_n: 0,
                dts_sum: 0, dts_n: 0,
                gpu_m_sum: 0, gpu_m_n: 0,
                gpu_s_sum: 0, gpu_s_n: 0,
                gpu_ms_sum: 0, gpu_ms_n: 0,
                sell_sum: 0, sell_n: 0,
                at_val_sum: 0, at_val_n: 0,
                at_pct_sum: 0, at_pct_n: 0,
                at_rating_sum: 0, at_rating_n: 0,
            });
        }
        const g = groups.get(key);
        const stock   = Number(r[4]) || 0;
        const sales90 = Number(r[5]) || 0;
        const sales7  = Number(r[6]) || 0;
        const dis     = r[7] != null ? Number(r[7]) : null;
        const dts     = r[8] != null ? Number(r[8]) : null;
        const gpu_m   = r[22] != null ? Number(r[22]) : null;
        const gpu_s   = r[10] != null ? Number(r[10]) : null;
        const gpu_ms  = r[11] != null ? Number(r[11]) : null;
        const sell    = r[13] != null ? Number(r[13]) : null;
        const at_val  = r[14] != null ? Number(r[14]) : null;
        const at_pct  = r[15] != null ? Number(r[15]) : null;
        const at_rate = r[16] != null ? Number(r[16]) : null;
        const ackDate    = r[17] ? new Date(r[17]) : null;
        const daysSinceAck = ackDate ? Math.floor((todayMs - ackDate.getTime()) / 86400000) : null;

        g.stock += stock;

        const passDIS  = maxDIS  === null || daysSinceAck === null || daysSinceAck <= maxDIS;
        const passDTS  = maxDTS  === null || dts  === null || dts  <= maxDTS;
        const passGPUM = minGPUM === null || gpu_m === null || gpu_m >= minGPUM;
        if (!passDIS || !passDTS || !passGPUM) continue;

        g.sales90 += sales90;
        g.sales7  += sales7;

        if (dis     != null) { g.dis_sum      += dis;    g.dis_n++;      }
        if (dts     != null) { g.dts_sum      += dts;    g.dts_n++;      }
        if (gpu_m   != null) { g.gpu_m_sum  += gpu_m;  g.gpu_m_n++;  }
        if (gpu_s   != null) { g.gpu_s_sum  += gpu_s;  g.gpu_s_n++;  }
        if (gpu_ms  != null) { g.gpu_ms_sum += gpu_ms; g.gpu_ms_n++; }
        if (sell    != null && sales90 === 1) { g.sell_sum += sell; g.sell_n++; }
        if (at_val  != null && sales90 === 1) { g.at_val_sum  += at_val;  g.at_val_n++;  }
        if (at_pct  != null && sales90 === 1) { g.at_pct_sum  += at_pct;  g.at_pct_n++;  }
        if (at_rate != null && sales90 === 1) { g.at_rating_sum += at_rate; g.at_rating_n++; }
    }

    const avg = (sum, n) => n > 0 ? sum / n : null;
    tableData = [];
    for (const g of groups.values()) {
        tableData.push([
            g.cap_id,                                   // 0
            g.label,                                    // 1
            g.fuel,                                     // 2
            g.stock,                                    // 3
            g.sales90,                                  // 4
            null,                                       // 5 pct — set in renderTable
            0,                                          // 6 today's buying — set by SP
            g.sales7,                                   // 7
            avg(g.dis_sum,       g.dis_n),              // 8  avg DIS
            avg(g.dts_sum,       g.dts_n),              // 9  avg DTS
            avg(g.gpu_m_sum,     g.gpu_m_n),            // 10 avg GPU-M
            avg(g.gpu_s_sum,  g.gpu_s_n),               // 11 avg GPU-S
            avg(g.gpu_ms_sum, g.gpu_ms_n),              // 12 avg GPU-MS
            null,                                       // 13 purchase_price (not displayed)
            avg(g.sell_sum,      g.sell_n),             // 14 avg sell price
            avg(g.at_val_sum,    g.at_val_n),           // 15 avg AT val
            avg(g.at_pct_sum,    g.at_pct_n) != null ? avg(g.at_pct_sum, g.at_pct_n) * 100 : null, // 16 avg AT pct %
            avg(g.at_rating_sum, g.at_rating_n),        // 17 avg AT rating
            // 18, 19, 20, 21 set in renderTable (ideal, required, -, remaining)
            // 22: price_band
            g.price_band,                               // 22 price_band
        ]);
    }
}

const BAND_RANK = { '<10k':0,'10-12.5k':1,'12.5-15k':2,'15-20k':3,'20-25k':4,'25-30k':5,'30k+':6,'interest_free':7 };
function bandRank(label) {
    return BAND_RANK[String(label ?? '').split(' / ').pop().trim()] ?? 99;
}

function getNumberToBuy() {
    const val = parseInt(document.getElementById('numberToBuy')?.value ?? '0', 10);
    return isNaN(val) ? 0 : Math.max(0, val);
}

/* ── Colour scale helpers ────────────────────────────────── */
const _RED = [248, 105, 107];
const _YEL = [255, 235, 132];
const _GRN = [ 99, 190, 123];
function _excelScale(ratio) {
    const r = Math.min(1, Math.max(0, ratio));
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    const [c1, c2, t] = r < 0.5 ? [_RED, _YEL, r * 2] : [_YEL, _GRN, (r - 0.5) * 2];
    return `rgb(${lerp(c1[0],c2[0],t)},${lerp(c1[1],c2[1],t)},${lerp(c1[2],c2[2],t)})`;
}
function _gradScale(ratio) { return _excelScale(1 - ratio); }

function greenScale(v, max) {
    if (v <= 0 || max <= 0) return '';
    const MIN_R = 0.25;
    const r = MIN_R + (1 - MIN_R) * Math.min(1, v / max);
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    return `rgb(${lerp(255,_GRN[0],r)},${lerp(255,_GRN[1],r)},${lerp(255,_GRN[2],r)})`;
}

function redGreenScale(v, min, max) {
    const MIN_R = 0.25;
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    if (v < 0 && min < 0) {
        const r = MIN_R + (1 - MIN_R) * Math.min(1, v / min);
        return `rgb(${lerp(255,_RED[0],r)},${lerp(255,_RED[1],r)},${lerp(255,_RED[2],r)})`;
    }
    if (v > 0 && max > 0) {
        const r = MIN_R + (1 - MIN_R) * Math.min(1, v / max);
        return `rgb(${lerp(255,_GRN[0],r)},${lerp(255,_GRN[1],r)},${lerp(255,_GRN[2],r)})`;
    }
    return '';
}

function dtsScale(v) {
    if (v == null || v === '') return '';
    return _gradScale(v / (getMaxDTS() ?? 45));
}

function fwdScale(v, min, max) {
    if (v == null || v === '' || min === max) return '';
    return _excelScale((v - min) / (max - min));
}

function revScale(v, min, max) {
    if (v == null || min === max) return '';
    return _excelScale(1 - (v - min) / (max - min));
}

const _fitCanvas = document.createElement('canvas');

/* ── Format helpers ──────────────────────────────────────── */
function fmt(v, dp=2)  { return (v == null || v === '') ? '' : Number(v).toFixed(dp); }
function fmtInt(v)     { return (v == null || v === '') ? '' : Math.round(Number(v)).toLocaleString(); }
function fmtGBP(v)     { return (v == null || v === '') ? '' : '£' + Math.round(Number(v)).toLocaleString(); }
function fmtPct(v)     { return (v == null || v === '') ? '' : Number(v).toFixed(1) + '%'; }

/* ── Totals / averages rows ──────────────────────────────── */

function isInterestFree(r) {
    const band = r[22] ?? String(r[1] ?? '').split(' / ').pop().trim();
    return band === 'interest_free';
}

function renderTotalsRow(visibleRows) {
    if (!visibleRows || !visibleRows.length) {
        ['totals-row-rate','totals-row-if'].forEach(id => {
            const tr = document.getElementById(id);
            if (tr) Array.from(tr.cells).forEach(td => { td.textContent = ''; td.style.background = ''; });
        });
        return;
    }
    const DATA_COLS = [
        [4,  'sum', fmtInt],
        [7,  'sum', fmtInt],
        [3,  'sum', fmtInt],
        [18, 'sum', v => fmt(v, 1)],
        [19, 'sum', v => fmt(v, 1)],
        [6,  'sum', fmtInt],
        null,
        [8,  'avg', v => fmt(v, 1)],
        [9,  'avg', v => fmt(v, 1)],
        [14, 'avg', fmtGBP],
        [15, 'avg', fmtGBP],
        [16, 'avg', fmtPct],
        [17, 'avg', fmtInt],
        [10, 'avg', fmtGBP],
        [11, 'avg', fmtGBP],
        [12, 'avg', fmtGBP],
    ];
    const TODAY_CELL_IDX     = 5;
    const REMAINING_CELL_IDX = 6;

    function fillRow(trEl, rows, label) {
        if (!trEl) return;
        const cells = Array.from(trEl.cells);
        cells[0].textContent = '';
        if (cells[1]) { cells[1].style.background = ''; cells[1].textContent = label; }
        const reqSum   = rows.reduce((s, r) => s + (Number(r[19]) || 0), 0);
        const spReady  = rows.some(r => r[6] !== null);
        const todaySum = spReady ? rows.reduce((s, r) => s + (Number(r[6]) || 0), 0) : null;
        DATA_COLS.forEach((def, i) => {
            const td = cells[i + 2];
            if (!td) return;
            td.style.background = '';
            if (i === REMAINING_CELL_IDX) {
                td.textContent = spReady ? fmtInt(Math.round(reqSum) - todaySum) : '';
                return;
            }
            if (i === TODAY_CELL_IDX) {
                td.style.background = spReady ? '#fef08a' : '';
            }
            if (!def) { td.textContent = ''; return; }
            const [di, agg, formatter] = def;
            const vals = rows.map(r => r[di]).filter(v => v != null && v !== '' && !isNaN(Number(v))).map(Number);
            if (!vals.length) { td.textContent = ''; return; }
            const total = vals.reduce((a, b) => a + b, 0);
            td.textContent = formatter(agg === 'sum' ? total : total / vals.length);
        });
    }

    const rateRows = visibleRows.filter(r => !isInterestFree(r));
    const ifRows   = visibleRows.filter(r =>  isInterestFree(r));
    fillRow(document.getElementById('totals-row-rate'),  rateRows,    'Rate');
    fillRow(document.getElementById('totals-row-if'),    ifRows,      '0%');
    fillRow(document.getElementById('totals-row-total'), visibleRows, 'Total');
    requestAnimationFrame(() => {
        const totalTr = document.getElementById('totals-row-total');
        const ifTr    = document.getElementById('totals-row-if');
        const rateTr  = document.getElementById('totals-row-rate');
        if (totalTr && ifTr && rateTr) {
            const hTotal = totalTr.getBoundingClientRect().height;
            const hIf    = ifTr.getBoundingClientRect().height;
            Array.from(ifTr.cells).forEach(td   => { td.style.bottom = hTotal + 'px'; });
            Array.from(rateTr.cells).forEach(td => { td.style.bottom = (hTotal + hIf) + 'px'; });
        }
    });
}

/* ── Render table ────────────────────────────────────────── */
function renderTable() {
    const body = document.getElementById('table-body');
    body.innerHTML = '';

    const numberToBuy = getNumberToBuy();
    const totalCurrentStock = tableData.reduce((s, r) => s + (Number(r[3]) || 0), 0);
    const idealTotal = totalCurrentStock + numberToBuy;

    const lbl = document.getElementById('idealTotalLabel');

    const total90dSales = tableData.reduce((s, r) => s + (Number(r[4]) || 0), 0);

    let maxIdeal = -Infinity;
    let maxVar = -Infinity, minVar = Infinity;
    let maxRem = -Infinity, minRem = Infinity;
    let minGM  = Infinity,  maxGM  = -Infinity;
    let minGMS = Infinity,  maxGMS = -Infinity;
    let minATR = Infinity,  maxATR = -Infinity;
    let max7d     = 0;
    let maxSales  = 0;

    const track = (v, mn, mx) =>
        v != null && v !== '' && !isNaN(Number(v)) ? [Math.min(mn, Number(v)), Math.max(mx, Number(v))] : [mn, mx];
    rawData.forEach(r => {
        [minGM,  maxGM]  = track(r[22], minGM,  maxGM);
        [minGMS, maxGMS] = track(r[11], minGMS, maxGMS);
        [minATR, maxATR] = track(r[16], minATR, maxATR);
    });
    tableData.forEach(r => {
        if ((r[7] || 0) > max7d)    max7d    = Number(r[7]);
        if ((r[4] || 0) > maxSales) maxSales = Number(r[4]);
    });

    tableData.forEach(row => {
        const sales = Number(row[4]) || 0;
        const stock = Number(row[3]) || 0;
        row[18] = sales > 0 && total90dSales > 0
            ? idealTotal * sales / total90dSales
            : 0;
        row[19] = row[18] - stock;
        if (row[18] > maxIdeal) maxIdeal = row[18];
        if (row[19] > maxVar)   maxVar   = row[19];
        if (row[19] < minVar)   minVar   = row[19];
    });

    const idealSum = tableData.reduce((s, r) => s + (r[18] || 0), 0);
    if (lbl) lbl.textContent = `Total current stock: ${totalCurrentStock.toLocaleString()} → Ideal total: ${(totalCurrentStock + numberToBuy).toLocaleString()}`;

    tableData.forEach(r => {
        r[21] = Math.round(r[19] || 0) - (r[6] || 0);
        if (r[21] > maxRem) maxRem = r[21];
        if (r[21] < minRem) minRem = r[21];
    });

    const fragment = document.createDocumentFragment();
    tableData.forEach(row => {
        const tr = document.createElement('tr');
        const add = (text) => {
            const td = document.createElement('td');
            td.textContent = text;
            tr.appendChild(td);
            return td;
        };
        add(row[0] ?? '');
        add(row[1] ?? '');
        {
            const td = document.createElement('td');
            td.style.cssText = 'padding:0.18em 0.3em;text-align:center;vertical-align:middle';
            const sp = document.createElement('span');
            sp.textContent = 'View';
            sp.className = 'view-btn';
            td.appendChild(sp);
            tr.appendChild(td);
        }
        add(normFuel(row[2]));
        {
            const td = add(fmtInt(row[4]));
            const v = Number(row[4]) || 0;
            if (maxSales > 0) {
                const t = v / maxSales;
                const r = Math.round(255 - t * (255 - 0));
                const g = Math.round(255 - t * (255 - 188));
                const b = Math.round(255 - t * (255 - 18));
                td.style.background = `rgb(${r},${g},${b})`;
            }
        }
        { const td = add(row[7] != null ? fmtInt(row[7]) : ''); if ((row[7] || 0) === 0) td.style.background = '#d8d8d8'; }
        { const td = add(fmtInt(row[3])); if ((row[3] || 0) === 0) td.style.background = '#d8d8d8'; }
        add(row[18] != null ? fmt(row[18], 1) : '');
        add(row[19] != null ? fmt(row[19], 1) : '');
        const tdToday = add(row[6] != null ? fmtInt(row[6]) : '');
        if ((row[6] || 0) > 0) tdToday.style.background = '#fef08a';
        add(fmtInt(row[21]));
        add(row[8]  != null ? fmt(row[8],  1) : '');
        add(row[9]  != null ? fmt(row[9],  1) : '');
        add(row[14] != null ? fmtGBP(row[14]) : '');
        add(row[15] != null ? fmtGBP(row[15]) : '');
        add(row[16] != null ? fmtPct(row[16]) : '');
        add(row[17] != null ? fmtInt(row[17]) : '');
        add(row[10] != null ? fmtGBP(row[10]) : '');
        add(row[11] != null ? fmtGBP(row[11]) : '');
        add(row[12] != null ? fmtGBP(row[12]) : '');
        fragment.appendChild(tr);
    });

    Array.from(fragment.children).forEach((tr, i) => {
        const r = tableData[i];
        if ((r[7] || 0) > 0) tr.cells[5].style.background = greenScale(r[7], max7d);
        tr.cells[10].style.background = redGreenScale(r[21], minRem, maxRem);
        if (r[9] != null) tr.cells[12].style.background = dtsScale(r[9]);
        if (r[10] != null) tr.cells[17].style.background = fwdScale(Number(r[10]), minGM,  maxGM);
        if (r[12] != null) tr.cells[19].style.background = fwdScale(Number(r[12]), minGMS, maxGMS);
        if (r[17] != null) tr.cells[16].style.background = fwdScale(Number(r[17]), minATR, maxATR);
        if (isInterestFree(r)) { tr.cells[0].style.background = '#bfdbfe'; tr.cells[1].style.background = '#bfdbfe'; }
    });

    body.appendChild(fragment);

    const reqSum = tableData.reduce((s, r) => s + (r[19] > 0 ? r[19] : 0), 0);
    document.getElementById('required-sum-inline').textContent = Math.round(reqSum).toLocaleString();

    updateSalesColumnLabel();
    applyFilter();
}

/* ── Sort ────────────────────────────────────────────────── */
function sortTable(col, type) {
    document.querySelectorAll('#data-table thead tr:last-child th').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        const icon = th.querySelector('.sort-icon i');
        if (icon) icon.className = 'fas fa-sort';
    });
    if (sortCol === col) sortDir *= -1; else { sortCol = col; sortDir = type === 'num' ? -1 : 1; }

    tableData.sort((a, b) => {
        let av = a[col], bv = b[col];
        const aNul = av == null || av === '';
        const bNul = bv == null || bv === '';
        if (aNul && bNul) return 0;
        if (aNul) return 1;
        if (bNul) return -1;
        if (col === 1) {
            const baseA = String(av).split(' / ').slice(0, -1).join(' / ').toLowerCase();
            const baseB = String(bv).split(' / ').slice(0, -1).join(' / ').toLowerCase();
            if (baseA !== baseB) return baseA < baseB ? -sortDir : sortDir;
            const ra = bandRank(av), rb = bandRank(bv);
            if (ra !== rb) return (ra - rb) * sortDir;
            av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
        } else if (type === 'num') {
            av = Number(av); bv = Number(bv);
        } else {
            av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
        }
        if (av !== bv) return av < bv ? -sortDir : sortDir;
        if (col === 21) {
            const adts = a[9] != null ? Number(a[9]) : Infinity;
            const bdts = b[9] != null ? Number(b[9]) : Infinity;
            return adts - bdts;
        }
        return 0;
    });

    const th = document.querySelector(`#data-table thead tr:last-child th[data-col="${col}"]`);
    if (th) {
        th.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
        const icon = th.querySelector('.sort-icon i');
        if (icon) icon.className = sortDir === 1 ? 'fas fa-sort-up' : 'fas fa-sort-down';
    }
    renderTable();
}

document.querySelectorAll('#data-table thead tr:last-child th').forEach(th => {
    th.addEventListener('click', e => {
        if (e.target.closest('.resize-handle') || e.target.closest('button')) return;
        sortTable(+th.dataset.col, th.dataset.type);
    });
    th.addEventListener('dblclick', e => {
        if (!e.target.closest('.resize-handle') && !e.target.closest('button')) autoFitCol(+th.dataset.col, th);
    });
});

/* ── Column resize ───────────────────────────────────────── */
const ghost = document.getElementById('resize-ghost');

function getThColIndex(th) {
    return Array.from(th.parentElement.children).indexOf(th);
}

document.querySelectorAll('.resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', e => {
        e.preventDefault();
        e.stopPropagation();
        const th = handle.closest('th');
        const colIdx = getThColIndex(th);
        const startX = e.pageX;
        const startW = th.offsetWidth;
        const thRect = th.getBoundingClientRect();

        ghost.style.left = thRect.right + 'px';
        ghost.style.display = 'block';
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        let lastX = startX;
        const onMove = ev => {
            lastX = ev.pageX;
            const newRight = thRect.right + (ev.pageX - startX);
            ghost.style.left = Math.max(thRect.left + 40, newRight) + 'px';
        };
        const onUp = () => {
            ghost.style.display = 'none';
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            const newW = Math.max(40, startW + lastX - startX);
            colWidths[colIdx] = newW;
            applyColWidths();
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
    handle.addEventListener('dblclick', e => {
        e.stopPropagation();
        const th = handle.closest('th');
        const colIdx = getThColIndex(th);
        colWidths[colIdx] = null;
        autoFitCol(+th.dataset.col, th);
    });
});

function autoFitCol(col, th) {
    const ctx = _fitCanvas.getContext('2d');
    ctx.font = getComputedStyle(document.getElementById('data-table')).fontSize + ' Inter, sans-serif';
    let maxW = ctx.measureText(th.childNodes[0].textContent.trim()).width + 32;
    document.querySelectorAll('#data-table tbody tr').forEach(tr => {
        const td = tr.cells[col];
        if (td) maxW = Math.max(maxW, ctx.measureText(td.textContent).width + 16);
    });
    const colIdx = getThColIndex(th);
    colWidths[colIdx] = Math.ceil(maxW);
    applyColWidths();
}

function applySpPurchases(p) {
    spPurchases = p;
    const exact = {};
    let spTotal = 0;
    for (const [k, v] of Object.entries(p)) {
        exact[k] = v;
        spTotal += v;
    }
    const badge = document.getElementById('sp-total-badge');
    if (badge) badge.textContent = spTotal + ' purchases';
    tableData.forEach(r => {
        const band = r[22] ?? String(r[1] ?? '').split(' / ').pop().trim();
        const key  = r[0] + '|' + band;
        r[6] = exact[key] || 0;
    });
}

/* ── SharePoint patch ────────────────────────────────────── */
function patchSharePoint() {
    setStatus('sp-status', SP_LABEL, 'running');

    fetch(window.BASE_URL + '/buying-requirements/sp-purchases')
        .then(r => r.json())
        .then(resp => {
            if (resp.status !== 'ok') return;
            applySpPurchases(resp.purchases);
            renderTable();
            setStatus('sp-status', SP_LABEL, 'done');
        })
        .catch(() => setStatus('sp-status', SP_LABEL, 'error'));
}

/* ── Fetch data ──────────────────────────────────────────── */
function fetchData() {
    const btn = document.getElementById('fetchBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Fetching...';
    setStatus('sf-status', SF_LABEL, 'running');
    setStatus('sp-status', SP_LABEL, 'idle');

    const sfSource = new EventSource(window.BASE_URL + '/buying-requirements/stream-snowflake');
    sfSource.onmessage = e => {
        const data = JSON.parse(e.data);
        if (data.type === 'done' || data.type === 'error') {
            setStatus('sf-status', SF_LABEL, data.type);
            sfSource.close();
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync-alt me-2"></i>Refresh Data';
        }
    };
    sfSource.onerror = () => {
        setStatus('sf-status', SF_LABEL, 'error');
        sfSource.close();
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-sync-alt me-2"></i>Refresh Data';
    };

    loadFromServer(true, true);
}

function applyRawDataToTable(fetchSP) {
    aggregateRawData();
    document.getElementById('sf-total-stock').textContent =
        rawData.reduce((s, r) => s + (Number(r[4]) || 0), 0).toLocaleString();
    document.getElementById('sf-total-sales').textContent =
        rawData.reduce((s, r) => s + (Number(r[5]) || 0), 0).toLocaleString();
    document.getElementById('data-section').style.display = 'block';
    sortCol = null; sortDir = 1;
    document.querySelectorAll('#data-table thead tr:last-child th').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        const icon = th.querySelector('.sort-icon i');
        if (icon) icon.className = 'fas fa-sort';
    });
    applyColWidths();
    renderTable();
    sortCol = null; sortDir = 1;
    sortTable(21, 'num');
    document.getElementById('table-wrap').classList.remove('d-none');
    if (fetchSP) patchSharePoint();
    else loadSPFromCache();
}

function refreshTable() {
    if (!rawData.length) return;
    const btn = document.getElementById('refreshTableBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading...'; }
    requestAnimationFrame(() => setTimeout(() => {
        aggregateRawData();
        if (Object.keys(spPurchases).length) applySpPurchases(spPurchases);
        renderTable();
        sortCol = 21; sortDir = 1;
        sortTable(21, 'num');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Refresh Table'; }
    }, 0));
}

function loadFromServer(fetchSP = false, fromFetchData = false) {
    const btn = document.getElementById('refreshTableBtn');
    if (!fromFetchData) {
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading...'; }
        document.getElementById('fetch-loading').style.display = 'block';
    }

    document.getElementById('table-wrap').classList.add('d-none');
    document.getElementById('table-error').classList.add('d-none');

    const sfUrl = window.BASE_URL + '/buying-requirements/sf-data' + (fromFetchData ? '?force=true' : '');
    fetch(sfUrl)
        .then(r => r.json())
        .then(resp => {
            document.getElementById('fetch-loading').style.display = 'none';
            if (resp.status !== 'ok') {
                document.getElementById('data-section').style.display = 'block';
                document.getElementById('table-error').textContent = resp.message;
                document.getElementById('table-error').classList.remove('d-none');
                return;
            }
            rawData = resp.rows;
            setStatus('sf-status', SF_LABEL, 'done');
            applyRawDataToTable(fetchSP);
        })
        .catch(err => {
            document.getElementById('fetch-loading').style.display = 'none';
            document.getElementById('table-error').textContent = 'Failed to load data: ' + err;
            document.getElementById('table-error').classList.remove('d-none');
        })
        .finally(() => {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Refresh Table'; }
        });
}

function loadSPFromCache() {
    fetch(window.BASE_URL + '/buying-requirements/sp-purchases-cached')
        .then(r => r.json())
        .then(resp => {
            if (resp.status !== 'ok') return;
            applySpPurchases(resp.purchases);
            renderTable();
        })
        .catch(() => {});
}

function exportXLSX() {
    const headers = Array.from(document.querySelectorAll('#data-table thead tr:last-child th'))
        .map(th => th.childNodes[0].textContent.trim());

    const visibleRows = Array.from(document.querySelectorAll('#table-body tr'))
        .filter(tr => tr.style.display !== 'none');

    const rows = visibleRows.map(tr =>
        Array.from(tr.cells).map(td => {
            const text = td.textContent.trim();
            const num = Number(text.replace(/[£,%]/g, '').replace(/,/g, ''));
            return text !== '' && !isNaN(num) ? num : text;
        })
    );

    const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Buying Requirements');

    const wbArray = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });

    (async () => {
        const zip = await JSZip.loadAsync(wbArray);
        const styleXml =
            `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
            `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
            `<fonts count="1"><font><sz val="11"/><name val="Aptos"/></font></fonts>` +
            `<fills count="2">` +
              `<fill><patternFill patternType="none"/></fill>` +
              `<fill><patternFill patternType="gray125"/></fill>` +
            `</fills>` +
            `<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>` +
            `<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>` +
            `<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>` +
            `<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>` +
            `</styleSheet>`;
        zip.file('xl/styles.xml', styleXml);
        const blob = await zip.generateAsync({ type: 'blob', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'buying_requirements.xlsx';
        a.click();
        URL.revokeObjectURL(a.href);
    })();
}

/* ── Vehicle popup ───────────────────────────────────────── */
let _modalVehicles = [];
let _modalSortCol = 'deriv';
let _modalSortAsc = true;

function _modalStatus(r) {
    const s = String(r[21] ?? '').toUpperCase();
    if (s === 'ORDERED') return 'Ordered';
    if (s === 'SOLD')    return 'Delivered';
    return 'Stock';
}

function _getModalSortValue(r, col) {
    if (col === 'deriv') return (r[1].split(' / ').slice(0, -1).join(' / ') || '').toLowerCase();
    if (col === 'vrm') return (r[18] || '').toLowerCase();
    if (col === 'supplier') return (r[19] || '').toLowerCase();
    if (col === 'status') return _modalStatus(r);
    return '';
}

function _sortModalVehicles() {
    _modalVehicles.sort((a, b) => {
        const av = _getModalSortValue(a, _modalSortCol);
        const bv = _getModalSortValue(b, _modalSortCol);
        let cmp = 0;
        if (av < bv) cmp = -1;
        if (av > bv) cmp = 1;
        if (cmp === 0 && _modalSortCol !== 'deriv') {
            const ad = (a[1].split(' / ').slice(0, -1).join(' / ') || '').toLowerCase();
            const bd = (b[1].split(' / ').slice(0, -1).join(' / ') || '').toLowerCase();
            if (ad < bd) cmp = -1;
            if (ad > bd) cmp = 1;
        }
        if (cmp === 0 && _modalSortCol !== 'status') {
            cmp = Number(b[4]) - Number(a[4]);
        }
        return _modalSortAsc ? cmp : -cmp;
    });
}

function _updateModalSortIndicators() {
    document.querySelectorAll('#vehicle-modal-table thead th[data-sort]').forEach(th => {
        const col = th.getAttribute('data-sort');
        const label = th.getAttribute('data-label');
        if (col === _modalSortCol) {
            th.textContent = label + ' ' + (_modalSortAsc ? '\u25b2' : '\u25bc');
        } else {
            th.textContent = label;
        }
    });
}

function _renderModalRows() {
    const tbody = document.getElementById('vehicle-modal-body');
    tbody.innerHTML = '';
    _modalVehicles.forEach(r => {
        const tr = document.createElement('tr');
        const status = _modalStatus(r);
        const deriv = r[1].split(' / ').slice(0, -1).join(' / ');
        const addTd = (text, align) => {
            const td = document.createElement('td');
            td.textContent = text;
            if (align) td.style.textAlign = align;
            tr.appendChild(td);
            return td;
        };
        addTd(deriv);
        addTd(r[18] || '—');
        const tdApex = document.createElement('td');
        if (r[20]) {
            const a = document.createElement('a');
            a.href = r[20];
            a.target = '_blank';
            a.textContent = 'Apex';
            a.className = 'apex-link';
            tdApex.appendChild(a);
        }
        tr.appendChild(tdApex);
        const tdPortal = document.createElement('td');
        if (r[26]) {
            const a = document.createElement('a');
            a.href = r[26];
            a.target = '_blank';
            a.textContent = 'Portal';
            a.className = 'apex-link';
            tdPortal.appendChild(a);
        }
        tr.appendChild(tdPortal);
        addTd(r[23] || '—');
        addTd(status);
        addTd(r[27] ? r[27].slice(0, 10).split('-').reverse().join('/') : '');
        addTd(r[19] || '—');
        addTd(r[24] ? r[24].slice(0, 10).split('-').reverse().join('/') : '—');
        addTd(r[25] != null ? Number(r[25]).toLocaleString() : '—', 'right');
        addTd(r[7]  != null ? fmtInt(r[7])  : '—', 'right');
        addTd(r[8]  != null ? fmtInt(r[8])  : '—', 'right');
        addTd(r[13] != null ? fmtGBP(r[13]) : '—', 'right');
        addTd(r[14] != null ? fmtGBP(r[14]) : '—', 'right');
        addTd(r[15] != null ? fmtPct(r[15] * 100) : '—', 'right');
        addTd(r[16] != null ? fmtInt(r[16]) : '—', 'right');
        addTd(r[22] != null ? fmtGBP(r[22]) : '—', 'right');
        if (status === 'Delivered') tr.classList.add('vehicle-row-sold');
        tbody.appendChild(tr);
    });
}

function openVehicleModal(capId, label) {
    // rawData rows: [0:cap_id, 1:label, 2:fuel, 3:price_band, 4:stock, 5:sales90,
    //                6:sales7, 7:dis, 8:dts, 9:gpu_m(sales), ..., 17:ack_date,
    //                18:vrm, 19:supplier_name, 20:vehicle_url, 21:vehicle_status,
    //                22:gpu_m_resolved, 23:vehicle_location, 24:date_first_registered, 25:mileage,
    //                26:autotrader_portal_link, 27:invoice_posted_date]
    const maxDIS = getMaxDIS();
    const maxDTS = getMaxDTS();
    const minGPUM = getMinGPUM();
    const todayMs = Date.now();
    _modalVehicles = rawData.filter(r => {
        if (r[0] !== capId || r[1] !== label) return false;
        const isStock = Number(r[4]) === 1;
        if (isStock) return true;
        const ackDate = r[17] ? new Date(r[17]) : null;
        const daysSinceAck = ackDate ? Math.floor((todayMs - ackDate.getTime()) / 86400000) : null;
        const dts = r[8] != null ? Number(r[8]) : null;
        const gpu_m = r[22] != null ? Number(r[22]) : null;
        const passDIS = maxDIS === null || daysSinceAck === null || daysSinceAck <= maxDIS;
        const passDTS = maxDTS === null || dts === null || dts <= maxDTS;
        const passGPUM = minGPUM === null || gpu_m === null || gpu_m >= minGPUM;
        return passDIS && passDTS && passGPUM;
    });
    _modalSortCol = 'deriv';
    _modalSortAsc = true;
    _sortModalVehicles();
    _updateModalSortIndicators();
    _renderModalRows();
    document.getElementById('vehicle-modal-title').textContent = label.split(' / ').slice(0, -1).join(' / ') + ' — CAP ID: ' + capId;
    document.getElementById('vehicle-modal-backdrop').style.display = '';
    const modal = document.getElementById('vehicle-modal');
    modal.style.fontSize = currentFontPx + 'px';
    modal.style.display = '';
}

function closeVehicleModal() {
    document.getElementById('vehicle-modal-backdrop').style.display = 'none';
    document.getElementById('vehicle-modal').style.display = 'none';
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeVehicleModal();
});

document.getElementById('vehicle-modal-table').addEventListener('click', e => {
    const th = e.target.closest('th[data-sort]');
    if (!th) return;
    const col = th.getAttribute('data-sort');
    if (col === _modalSortCol) {
        _modalSortAsc = !_modalSortAsc;
    } else {
        _modalSortCol = col;
        _modalSortAsc = true;
    }
    _sortModalVehicles();
    _updateModalSortIndicators();
    _renderModalRows();
});

document.getElementById('table-body').addEventListener('click', e => {
    const btn = e.target.closest('.view-btn');
    if (!btn) return;
    const tr = btn.closest('tr');
    const idx = Array.from(document.getElementById('table-body').children).indexOf(tr);
    const row = tableData[idx];
    if (row) openVehicleModal(row[0], row[1]);
});

document.addEventListener('DOMContentLoaded', () => loadFromServer(true));