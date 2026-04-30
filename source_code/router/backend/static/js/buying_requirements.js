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
    if (elId === 'sp-status') {
        if (state === 'running') {
            el.style.cssText = 'background:#fde047;color:#000;font-weight:700;padding:2px 8px;border-radius:4px;';
        } else {
            el.style.cssText = '';
        }
    }
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
const BASE_FONT_PX  = 12;
const BASE_FIXED_PX = 65;
const BASE_DESC_PX  = 541;
const BASE_OVERRIDE = { 2: 60, 3: 46, 8: 78, 9: 65.5, 10: 79.5 }; // View, Fuel narrow, Required wider, Remaining wider
const FONT_STEP     = 0.5; // px per click
const FONT_MIN      = 8;
const FONT_MAX      = 16;

let currentFontPx = BASE_FONT_PX; // 11.5px default

function scaledFixed()    { return Math.round(BASE_FIXED_PX * currentFontPx / BASE_FONT_PX); }
function scaledDesc()     { return Math.round(BASE_DESC_PX  * currentFontPx / BASE_FONT_PX); }
function scaledOverride(i){ return Math.round((BASE_OVERRIDE[i] ?? BASE_FIXED_PX) * currentFontPx / BASE_FONT_PX); }

function changeFontSize(delta) {
    currentFontPx = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round((currentFontPx + delta * FONT_STEP) * 10) / 10));
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

const CAP_ONLY_COL_INDICES = new Set([0, 2, 8, 9]); // CAP ID, View Vehicles, Required (cap only), Today's Buying

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
        if (currentView === 'model' && CAP_ONLY_COL_INDICES.has(i)) return;
        const w = colWidths[i] !== null ? colWidths[i] : (i === 1 ? scaledDesc() : scaledOverride(i));
        totalW += w;
        const col = document.createElement('col');
        col.style.width = w + 'px';
        cg.appendChild(col);
    });
    table.style.width = totalW + 'px';
}

window.addEventListener('resize', applyColWidths);

function _setApexUpdatedAt(rows) {
    let max = null;
    rows.forEach(r => { if (r[28] && (!max || r[28] > max)) max = r[28]; });
    const el = document.getElementById('apex-updated-at');
    const val = document.getElementById('apex-updated-at-val');
    if (!el || !val || !max) return;
    const d = new Date(max);
    const dd = String(d.getDate()).padStart(2, '0');
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const yyyy = d.getFullYear();
    const hh = String(d.getHours()).padStart(2, '0');
    const min = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    val.textContent = `${dd}/${mm}/${yyyy} ${hh}:${min}:${ss}`;
    el.style.display = '';
}

/* ── Filters ─────────────────────────────────────────────── */
let filterBand = new Set(['All']), filterFuel = new Set(['All']), filterCapId = null, filterMinReq = null, filterModel = null;

function resetTableFilters() {
    // Reset band
    filterBand.clear(); filterBand.add('All');
    document.querySelectorAll('.filter-btn[data-group="band"]').forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === 'All');
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== 'All');
    });
    // Reset fuel
    filterFuel.clear(); filterFuel.add('All');
    document.querySelectorAll('.filter-btn[data-group="fuel"]').forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === 'All');
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== 'All');
    });
    // Reset CAP ID
    filterCapId = null;
    const capInp = document.getElementById('capIdSearch');
    if (capInp) capInp.value = '';
    const capClear = document.getElementById('capIdClearBtn');
    if (capClear) capClear.style.display = 'none';
    // Reset Model
    filterModel = null;
    const modelInp = document.getElementById('modelSearch');
    if (modelInp) modelInp.value = '';
    const modelClear = document.getElementById('modelClearBtn');
    if (modelClear) modelClear.style.display = 'none';
    // Reset Min Req
    filterMinReq = null;
    const reqInp = document.getElementById('minReqInput');
    if (reqInp) reqInp.value = '';
    const reqClear = document.getElementById('minReqClearBtn');
    if (reqClear) reqClear.style.display = 'none';

    if (tableData.length) applyFilter();
}

function applyMinReqFilter() {
    const val = parseInt(document.getElementById('minReqInput').value, 10);
    filterMinReq = isNaN(val) ? null : val;
    const clearBtn = document.getElementById('minReqClearBtn');
    if (clearBtn) clearBtn.style.display = filterMinReq !== null ? '' : 'none';
    if (tableData.length) applyFilter();
}

function clearMinReqFilter() {
    filterMinReq = null;
    const inp = document.getElementById('minReqInput');
    if (inp) inp.value = '';
    const clearBtn = document.getElementById('minReqClearBtn');
    if (clearBtn) clearBtn.style.display = 'none';
    if (tableData.length) applyFilter();
}

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

function applyModelFilter() {
    const val = document.getElementById('modelSearch').value.trim().toLowerCase();
    filterModel = val.length > 0 ? val : null;
const clearBtn = document.getElementById('modelClearBtn');
    if (clearBtn) clearBtn.style.display = filterModel !== null ? '' : 'none';
    if (tableData.length) applyFilter();
}

function clearModelFilter() {
    filterModel = null;
    const inp = document.getElementById('modelSearch');
    if (inp) inp.value = '';
    const clearBtn = document.getElementById('modelClearBtn');
    if (clearBtn) clearBtn.style.display = 'none';
    if (tableData.length) applyFilter();
}

document.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const id = e.target.id;
    if (id === 'capIdSearch') applyCapIdFilter();
    else if (id === 'modelSearch') applyModelFilter();
});

document.addEventListener('DOMContentLoaded', () => {
    const lbl = document.getElementById('fontSizeLabel');
    if (lbl) lbl.textContent = currentFontPx + 'px';
});

const ALL_BANDS = new Set(['<10k','10-12.5k','12.5-15k','15-20k','20-25k','25-30k','30k+','interest_free']);
const ALL_FUELS = new Set(['Diesel','Electric','Hybrid','Petrol']);

const FUEL_NORM = { p:'Petrol', petrol:'Petrol', d:'Diesel', diesel:'Diesel', e:'Electric', electric:'Electric', ev:'Electric', h:'Hybrid', hybrid:'Hybrid', phev:'Hybrid', mhev:'Hybrid' };
function normFuel(v) { return FUEL_NORM[String(v ?? '').trim().toLowerCase()] || 'Petrol'; }
const FUEL_ABBR = { Petrol:'P', Diesel:'D', Electric:'E', Hybrid:'H' };
function fuelAbbr(v) { return FUEL_ABBR[normFuel(v)] || 'P'; }

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
        const req = row[19] != null ? Math.round(Number(row[19])) : null;
        const passModel = filterModel === null || String(row[1] ?? '').toLowerCase().includes(filterModel);
        const visible = passModel && (filterCapId !== null
            ? Number(row[0]) === filterCapId
            : (filterBand.has('All') || filterBand.has(band)) &&
              (filterFuel.has('All') || filterFuel.has(fuel)) &&
              (filterMinReq === null || (req !== null && req >= filterMinReq)));
        tr.style.display = visible ? '' : 'none';
        if (visible) visibleRows.push(row);
    });
    const lbl = document.getElementById('filtered-sales-label');
    if (lbl) lbl.textContent = `Filtered ${getMaxDIS() ?? 90}d Sales: ${filteredSales.toLocaleString()} / ${totalSales.toLocaleString()}`;
    const capLbl = document.getElementById('filtered-cap-count');
    if (capLbl) {
        capLbl.textContent = `Filtered ${currentView === 'model' ? 'Models' : 'CAP IDs'}: ${visibleRows.length}`;
        capLbl.classList.remove('flash');
        void capLbl.offsetWidth; // force reflow so re-adding the class restarts the animation
        capLbl.classList.add('flash');
    }
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
    const flbl = document.getElementById('filtered-sales-label');
    if (flbl && flbl.textContent) {
        flbl.textContent = flbl.textContent.replace(/Filtered \S+ Sales:/, `Filtered ${label}:`);
    }
}
document.getElementById('maxDIS')?.addEventListener('change', updateSalesColumnLabel);
function getMaxDTS()  { const v = parseFloat(document.getElementById('maxDTS')?.value);  return isNaN(v) ? null : v; }
function getMinGPUM() { const v = parseFloat(document.getElementById('minGPUM')?.value); return isNaN(v) ? null : v; }

/* ── View toggle ─────────────────────────────────────────── */
let currentView = 'cap'; // 'cap' | 'model'
let pendingView = 'cap'; // selection not yet applied


const THEAD_COLS = [
    { capOnly: true,  html: '<span class="th-label">CAP ID</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="0" data-type="num"' },
    { capOnly: false, html: '<span class="th-label" id="th-label-span">Make / Model / Derivative / Price Band</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="1" data-type="str" id="th-label" class="col-left"' },
    { capOnly: true,  html: '<span class="th-label">View Vehicles</span>', attrs: 'class="col-view"' },
    { capOnly: false, html: '<span class="th-label">Fuel</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="2" data-type="str" class="col-center"' },
    { capOnly: false, html: '<span class="th-label">90d Sales</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="4" data-type="num" id="th-total-sales"' },
    { capOnly: false, html: '<span class="th-label">7d Sales</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="7" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Stock</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="3" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Ideal Stock</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="18" data-type="num"' },
    { capOnly: true, html: '<span class="th-label">Required</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="19" data-type="num"' },
    { capOnly: true,  html: '<span class="th-label">Today\'s Buying</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="6" data-type="num" style="background:#fef08a"' },
    { capOnly: false, html: '<span class="th-label">Remaining</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="21" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg Days in Stock</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="8" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg Days to Sell</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="9" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg Sell Price</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="14" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg AT PTM</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="15" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg AT PTM %</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="16" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg AT Rating</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="17" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg GPU-M</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="10" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg GPU-S</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="11" data-type="num"' },
    { capOnly: false, html: '<span class="th-label">Avg GPU-MS</span><span class="sort-icon"><i class="fas fa-sort"></i></span><div class="resize-handle"></div>', attrs: 'data-col="12" data-type="num"' },
];

function rebuildTheadTfoot() {
    const isCap = currentView === 'cap';
    const cols = THEAD_COLS.filter(c => isCap || !c.capOnly);
    const nCols = cols.length;

    // thead
    const thead = document.querySelector('#data-table thead tr');
    if (thead) {
        thead.innerHTML = cols.map(c => `<th ${c.attrs}>${c.html}</th>`).join('');
        // re-attach sort listeners
        thead.querySelectorAll('th').forEach(th => {
            th.addEventListener('click', e => {
                if (e.target.closest('.resize-handle') || e.target.closest('button')) return;
                sortTable(+th.dataset.col, th.dataset.type);
            });
            th.addEventListener('dblclick', e => {
                if (!e.target.closest('.resize-handle') && !e.target.closest('button')) autoFitCol(+th.dataset.col, th);
            });
        });
        // re-attach resize handles
        thead.querySelectorAll('.resize-handle').forEach(handle => attachResizeHandle(handle));
    }

    // tfoot — rebuild each row with correct number of cells
    // cap:   blank(colspan=3) | label-cell(fuel col) | data cells × (nCols-4)
    // model: blank(colspan=1) | label-cell(fuel col) | data cells × (nCols-2)
    ['totals-row-rate','totals-row-if','totals-row-total'].forEach(id => {
        const tr = document.getElementById(id);
        if (!tr) return;
        const blankColspan = isCap ? 3 : 1;
        let html = `<td class="tfoot-blank" colspan="${blankColspan}"></td><td></td>`;
        const dataCells = nCols - blankColspan - 1;
        for (let i = 0; i < dataCells; i++) html += '<td></td>';
        tr.innerHTML = html;
    });
}

function setView(view) {
    pendingView = view;
    document.getElementById('view-btn-cap').classList.toggle('btn-dark', view === 'cap');
    document.getElementById('view-btn-cap').classList.toggle('btn-outline-secondary', view !== 'cap');
    document.getElementById('view-btn-model').classList.toggle('btn-dark', view === 'model');
    document.getElementById('view-btn-model').classList.toggle('btn-outline-secondary', view !== 'model');
}

function applyView() {
    if (!rawData.length) return;
    const btn = document.getElementById('applyViewBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading...'; }
    requestAnimationFrame(() => setTimeout(() => {
    currentView = pendingView;
    rebuildTheadTfoot();
    colWidths = new Array(COL_UNITS.length).fill(null);
    applyColWidths();
    if (currentView === 'cap') aggregateRawData(); else aggregateModelData();
    applySpPurchases(spPurchases);
    renderTable();
    if (currentView === 'model') {
        sortCol = 21; sortDir = 1; // sortTable toggles to -1 → descending
        sortTable(21, 'num');
    } else {
        sortCol = null; sortDir = 1;
        sortTable(21, 'num');
    }
    if (btn) { btn.disabled = false; btn.innerHTML = 'Apply'; }
    }, 0));
}

/* ── Aggregate raw per-vehicle rows into per-derivative rows ─
 *
 * Raw row layout (from Snowflake):
 *   [0:cap_id, 1:label, 2:fuel, 3:price_band, 4:stock, 5:sales90, 6:sales7,
 *    7:dis, 8:dts, 9:gpu_m, 10:gpu_s, 11:gpu_ms,
 *    12:purchase_price, 13:sell_price, 14:at_val, 15:at_pct, 16:at_rating,
 *    17:ack_date, 18:vrm, 19:supplier_name, 20:vehicle_url, 21:vehicle_status,
 *    22:gpu_m_resolved, 29:make_model, 30:cap_live_clean]
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
        const key = r[0] + '|' + r[2] + '|' + r[3];
        const labelNoBand = r[1].split(' / ').slice(0, -1).join(' / ');
        if (!groups.has(key)) {
            groups.set(key, {
                cap_id: r[0], label: labelNoBand, fuel: r[2], price_band: r[3],
                stock: 0, sales90: 0, sales7: 0,
                dis_sum: 0, dis_n: 0,
                dts_sum: 0, dts_n: 0,
                gpu_m_sum: 0, gpu_m_n: 0,
                gpu_s_sum: 0, gpu_s_n: 0,
                gpu_ms_sum: 0, gpu_ms_n: 0,
                sell_sum: 0, sell_n: 0,
                at_val_sum: 0, at_val_n: 0,
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

        const sold     = sales90 === 1;
        const soldRate = sold && r[3] !== 'interest_free';
        if (dis     != null && sold)     { g.dis_sum        += dis;    g.dis_n++;        }
        if (dts     != null && sold)     { g.dts_sum        += dts;    g.dts_n++;        }
        if (gpu_m   != null && sold)     { g.gpu_m_sum      += gpu_m;  g.gpu_m_n++;      }
        if (gpu_s   != null && sold)     { g.gpu_s_sum      += gpu_s;  g.gpu_s_n++;      }
        if (gpu_ms  != null && sold)     { g.gpu_ms_sum     += gpu_ms; g.gpu_ms_n++;     }
        if (sell    != null && sell !== 0 && soldRate) { g.sell_sum += sell; g.sell_n++; }
        if (at_val  != null && soldRate) { g.at_val_sum     += at_val; g.at_val_n++;     }
        if (at_rate != null && sold)     { g.at_rating_sum  += at_rate; g.at_rating_n++; }
    }

    const avg = (sum, n) => n > 0 ? sum / n : null;
    tableData = [];
    for (const g of groups.values()) {
        if (g.stock === 0 && g.sales90 === 0) continue;
        const avgSell  = avg(g.sell_sum,   g.sell_n);
        const avgAtVal = avg(g.at_val_sum, g.at_val_n);
        tableData.push([
            g.cap_id,                                   // 0
            g.label + ' / ' + g.price_band,            // 1 (table: includes price band)
            g.fuel,                                     // 2
            g.stock,                                    // 3
            g.sales90,                                  // 4
            null,                                       // 5 pct — set in renderTable
            null,                                       // 6 today's buying — set by SP
            g.sales7,                                   // 7
            avg(g.dis_sum,       g.dis_n),              // 8  avg DIS
            avg(g.dts_sum,       g.dts_n),              // 9  avg DTS
            avg(g.gpu_m_sum,     g.gpu_m_n),            // 10 avg GPU-M
            avg(g.gpu_s_sum,  g.gpu_s_n),               // 11 avg GPU-S
            avg(g.gpu_ms_sum, g.gpu_ms_n),              // 12 avg GPU-MS
            null,                                       // 13 purchase_price (not displayed)
            avgSell,                                    // 14 avg sell price
            avgAtVal,                                   // 15 avg AT val
            (avgSell != null && avgAtVal != null && avgAtVal !== 0) ? ((avgSell) / avgAtVal) * 100 : null, // 16 AT pct %
            avg(g.at_rating_sum, g.at_rating_n),        // 17 avg AT rating
            // 18, 19, 20, 21 set in renderTable (ideal, required, -, remaining)
            // 22: price_band
            g.price_band,                               // 22 price_band
        ]);
    }
}

function aggregateModelData() {
    const maxDIS  = getMaxDIS();
    const maxDTS  = getMaxDTS();
    const minGPUM = getMinGPUM();
    const todayMs = Date.now();

    const groups = new Map();
    for (const r of rawData) {
        const makeModel  = r[29] || '';
        const fuel       = normFuel(r[2]);
        const price_band = r[3] || '';
        const key = makeModel + '|' + fuel + '|' + price_band;
        if (!groups.has(key)) {
            const label = makeModel + ' / ' + normFuel(r[2]) + ' / ' + price_band;
            groups.set(key, {
                cap_id: null, label, fuel: r[2], price_band,
                stock: 0, sales90: 0, sales7: 0,
                dis_sum: 0, dis_n: 0,
                dts_sum: 0, dts_n: 0,
                gpu_m_sum: 0, gpu_m_n: 0,
                gpu_s_sum: 0, gpu_s_n: 0,
                gpu_ms_sum: 0, gpu_ms_n: 0,
                sell_sum: 0, sell_n: 0,
                at_val_sum: 0, at_val_n: 0,
                at_rating_sum: 0, at_rating_n: 0,
            });
        }
        const g = groups.get(key);
        const stock   = Number(r[4]) || 0;
        const sales90 = Number(r[5]) || 0;
        const sales7  = Number(r[6]) || 0;
        const dis     = r[7]  != null ? Number(r[7])  : null;
        const dts     = r[8]  != null ? Number(r[8])  : null;
        const gpu_m   = r[22] != null ? Number(r[22]) : null;
        const gpu_s   = r[10] != null ? Number(r[10]) : null;
        const gpu_ms  = r[11] != null ? Number(r[11]) : null;
        const sell    = r[13] != null ? Number(r[13]) : null;
        const at_val  = r[14] != null ? Number(r[14]) : null;
        const at_rate = r[16] != null ? Number(r[16]) : null;
        const ackDate      = r[17] ? new Date(r[17]) : null;
        const daysSinceAck = ackDate ? Math.floor((todayMs - ackDate.getTime()) / 86400000) : null;

        g.stock += stock;

        const passDIS  = maxDIS  === null || daysSinceAck === null || daysSinceAck <= maxDIS;
        const passDTS  = maxDTS  === null || dts  === null || dts  <= maxDTS;
        const passGPUM = minGPUM === null || gpu_m === null || gpu_m >= minGPUM;
        if (!passDIS || !passDTS || !passGPUM) continue;

        g.sales90 += sales90;
        g.sales7  += sales7;

        if (dis    != null && sales90 === 1) { g.dis_sum     += dis;    g.dis_n++;     }
        if (dts    != null && sales90 === 1) { g.dts_sum     += dts;    g.dts_n++;     }
        if (gpu_m  != null && sales90 === 1) { g.gpu_m_sum  += gpu_m;  g.gpu_m_n++;  }
        if (gpu_s  != null && sales90 === 1) { g.gpu_s_sum  += gpu_s;  g.gpu_s_n++;  }
        if (gpu_ms != null && sales90 === 1) { g.gpu_ms_sum += gpu_ms; g.gpu_ms_n++; }
        if (sell   != null && sell !== 0 && sales90 === 1) { g.sell_sum += sell; g.sell_n++; }
        if (at_val != null && sales90 === 1) { g.at_val_sum  += at_val;  g.at_val_n++;  }
        if (at_rate!= null && sales90 === 1) { g.at_rating_sum += at_rate; g.at_rating_n++; }
    }

    const avg = (sum, n) => n > 0 ? sum / n : null;
    tableData = [];
    for (const g of groups.values()) {
        if (g.stock === 0 && g.sales90 === 0) continue;
        const avgSell  = avg(g.sell_sum,   g.sell_n);
        const avgAtVal = avg(g.at_val_sum, g.at_val_n);
        tableData.push([
            g.cap_id,
            g.label,
            g.fuel,
            g.stock,
            g.sales90,
            null,
            null,
            g.sales7,
            avg(g.dis_sum,       g.dis_n),
            avg(g.dts_sum,       g.dts_n),
            avg(g.gpu_m_sum,     g.gpu_m_n),
            avg(g.gpu_s_sum,     g.gpu_s_n),
            avg(g.gpu_ms_sum,    g.gpu_ms_n),
            null,
            avgSell,
            avgAtVal,
            (avgSell != null && avgAtVal != null && avgAtVal !== 0) ? ((avgSell) / avgAtVal) * 100 : null,
            avg(g.at_rating_sum, g.at_rating_n),
            // 18, 19, 20, 21 set in renderTable
            g.price_band,  // 22
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
const _YEL = [255, 243, 180];
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
    // cap view:   blank(3) | fuel(label) | sales | 7d | stock | ideal | req | today | remaining | avgDIS | avgDTS | sell | ATPTM | ATPTM% | ATRating | GPUM | GPUS | GPUMS
    // model view: blank(1) | fuel(label) | sales | 7d | stock | ideal | req |                    avgDIS | avgDTS | sell | ATPTM | ATPTM% | ATRating | GPUM | GPUS | GPUMS
    const isCap = currentView === 'cap';
    const CAP_DATA_COLS = [
        [4,  'sum', fmtInt],
        [7,  'sum', fmtInt],
        [3,  'sum', fmtInt],
        [18, 'sum', v => fmt(v, 1)],
        [19, 'sum', v => fmt(v, 1)],
        [6,  'sum', fmtInt],   // Today's Buying (cap only)
        null,                  // Remaining (cap only, computed)
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
    const MODEL_DATA_COLS = [
        [4,  'sum', fmtInt],
        [7,  'sum', fmtInt],
        [3,  'sum', fmtInt],
        [18, 'sum', v => fmt(v, 1)],
        [21, 'sum', fmtInt],
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
    const DATA_COLS = isCap ? CAP_DATA_COLS : MODEL_DATA_COLS;
    const TODAY_CELL_IDX     = 5;  // only used in cap view
    const REMAINING_CELL_IDX = 6;  // only used in cap view

    function fillRow(trEl, rows, label) {
        if (!trEl) return;
        const cells = Array.from(trEl.cells);
        cells[0].textContent = '';
        if (cells[1]) { cells[1].style.background = ''; cells[1].textContent = label; }
        const reqSum   = rows.reduce((s, r) => s + (Number(r[19]) || 0), 0);
        const spReady  = isCap && rows.some(r => r[6] !== null);
        const todaySum = spReady ? rows.reduce((s, r) => s + (Number(r[6]) || 0), 0) : null;
        const MODEL_REM_IDX = 4; // index in MODEL_DATA_COLS for Remaining
        DATA_COLS.forEach((def, i) => {
            const td = cells[i + 2];
            if (!td) return;
            td.style.background = '';
            if (isCap && i === REMAINING_CELL_IDX) {
                const remSum = rows.reduce((s, r) => s + (Number(r[21]) || 0), 0);
                td.textContent = spReady ? fmt(remSum, 1) : '';
                return;
            }
            if (!isCap && i === MODEL_REM_IDX) {
                td.textContent = fmt(reqSum, 1);
                return;
            }
            if (isCap && i === TODAY_CELL_IDX) {
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
    if (lbl) lbl.textContent = `Ideal total: ${(totalCurrentStock + numberToBuy).toLocaleString()}`;

    tableData.forEach(r => {
        r[21] = (r[19] || 0) - (r[6] || 0);
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
        if (currentView === 'cap') add(row[0] ?? '');
        { const td = add((row[1] ?? '').replace('interest_free', '0%')); td.classList.add('col-left'); }
        if (currentView === 'cap') {
            const td = document.createElement('td');
            td.style.cssText = 'padding:0.18em 0.3em;text-align:center;vertical-align:middle';
            const sp = document.createElement('span');
            sp.textContent = 'View';
            sp.className = 'view-btn';
            td.appendChild(sp);
            tr.appendChild(td);
        }
        add(fuelAbbr(row[2]));
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
        if (currentView === 'cap') add(row[19] != null ? fmt(row[19], 1) : '');
        if (currentView === 'cap') {
            const tdToday = add(row[6] != null ? fmtInt(row[6]) : '');
            if ((row[6] || 0) > 0) tdToday.style.background = '#fef08a';
        }
        add(row[21] != null ? fmt(row[21], 1) : '');
        add(row[8]  != null ? fmt(row[8],  1) : '');
        add(row[9]  != null ? fmt(row[9],  1) : '');
        add(row[14] != null ? fmtGBP(row[14]) : '');
        add(row[15] != null ? fmtGBP(row[15]) : '');
        add(row[16] != null ? fmtPct(row[16]) : '');
        add(row[17] != null ? fmtInt(row[17]) : '');
        { const td = add(row[10] != null ? fmtGBP(row[10]) : ''); if (row[10] != null && row[10] < 0) td.style.color = '#dc2626'; }
        add(row[11] != null ? fmtGBP(row[11]) : '');
        { const td = add(row[12] != null ? fmtGBP(row[12]) : ''); if (row[12] != null && row[12] < 0) td.style.color = '#dc2626'; }
        fragment.appendChild(tr);
    });

    // DOM cell indices depend on view
    // cap:   CAP_ID(0) label(1) View(2) fuel(3) sales(4) 7d(5) stock(6) ideal(7) req(8) today(9) rem(10) DIS(11) DTS(12) sell(13) ATPTM(14) ATPTM%(15) ATR(16) GPUM(17) GPUS(18) GPUMS(19)
    // model: label(0) fuel(1) sales(2) 7d(3) stock(4) ideal(5) req(6) DIS(7) DTS(8) sell(9) ATPTM(10) ATPTM%(11) ATR(12) GPUM(13) GPUS(14) GPUMS(15)
    const isCap = currentView === 'cap';
    // cap:   CAP_ID(0) label(1) View(2) fuel(3) sales(4) 7d(5) stock(6) ideal(7) req(8) today(9) rem(10) DIS(11) DTS(12) sell(13) ATPTM(14) ATPTM%(15) ATR(16) GPUM(17) GPUS(18) GPUMS(19)
    // model: label(0) fuel(1) sales(2) 7d(3) stock(4) ideal(5) rem(6) DIS(7) DTS(8) sell(9) ATPTM(10) ATPTM%(11) ATR(12) GPUM(13) GPUS(14) GPUMS(15)
    const CI = {
        sales7:    isCap ? 5  : 3,
        required:  isCap ? 8  : null,
        remaining: isCap ? 10 : 6,
        dts:       isCap ? 12 : 8,
        gpuM:      isCap ? 17 : 13,
        gpuMS:     isCap ? 19 : 15,
        atRating:  isCap ? 16 : 12,
        ifSkip:    isCap ? new Set([4,5,9,10,12,16,17,19]) : new Set([2,3,6,8,12,13,15]),
    };
    Array.from(fragment.children).forEach((tr, i) => {
        const r = tableData[i];
        if ((r[7] || 0) > 0 && tr.cells[CI.sales7])   tr.cells[CI.sales7].style.background   = greenScale(r[7], max7d);
        if (r[19] != null && tr.cells[CI.required])    tr.cells[CI.required].style.background  = redGreenScale(r[19], minVar, maxVar);
        if (CI.remaining !== null && tr.cells[CI.remaining]) tr.cells[CI.remaining].style.background = redGreenScale(r[21], minRem, maxRem);
        if (r[9]  != null && tr.cells[CI.dts])         tr.cells[CI.dts].style.background       = dtsScale(r[9]);
        if (r[10] != null && tr.cells[CI.gpuM])        tr.cells[CI.gpuM].style.background      = fwdScale(Number(r[10]), minGM,  maxGM);
        if (r[12] != null && tr.cells[CI.gpuMS])       tr.cells[CI.gpuMS].style.background     = fwdScale(Number(r[12]), minGMS, maxGMS);
        if (r[17] != null && tr.cells[CI.atRating])    tr.cells[CI.atRating].style.background  = fwdScale(Number(r[17]), minATR, maxATR);
        if (isInterestFree(r)) {
            Array.from(tr.cells).forEach((td, ci) => { if (!CI.ifSkip.has(ci)) td.style.background = '#bfdbfe'; });
        }
    });

    body.appendChild(fragment);

    const reqSum = tableData.reduce((s, r) => s + (r[19] > 0 ? r[19] : 0), 0);
    document.getElementById('required-sum-inline').textContent = fmt(reqSum, 1);

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
        if (_resizeDragged) { _resizeDragged = false; return; }
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

let _resizeDragged = false;

function attachResizeHandle(handle) {
    handle.addEventListener('mousedown', e => {
        e.preventDefault();
        e.stopPropagation();
        _resizeDragged = false;
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
            if (Math.abs(ev.pageX - startX) > 3) _resizeDragged = true;
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
}

document.querySelectorAll('.resize-handle').forEach(handle => attachResizeHandle(handle));

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
    let spTotal = 0;
    for (const v of Object.values(p)) spTotal += v;
    const badge = document.getElementById('sp-total-badge');
    if (badge) badge.textContent = spTotal + ' purchases';
    if (currentView === 'model') return;
    tableData.forEach(r => {
        const band = r[22] ?? String(r[1] ?? '').split(' / ').pop().trim();
        const key  = r[0] + '|' + band;
        r[6] = p[key] || 0;
    });
}

const _SP_MAX_AGE_SECS = 30 * 60;

function _setSfCachedAt(cachedAt) {
    const el = document.getElementById('sf-status');
    if (!el || !cachedAt) return;
    const d = new Date(cachedAt * 1000);
    const hhmm = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
    el.innerHTML = `${SF_LABEL} <span class="text-muted" style="font-size:0.85em">updated ${hhmm}</span>`;
}

function _setSpUpdatedAt(fetchedAt) {
    const el = document.getElementById('sp-status');
    if (!el) return;
    if (fetchedAt) {
        const d = new Date(fetchedAt * 1000);
        const hhmm = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
        el.innerHTML = `${SP_LABEL} <span class="text-muted" style="font-size:0.85em">updated ${hhmm}</span>`;
    }
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
            _setSpUpdatedAt(resp.fetched_at);
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
    if (currentView === 'model') aggregateModelData(); else aggregateRawData();
    document.getElementById('sf-total-stock').textContent =
        rawData.reduce((s, r) => s + (Number(r[4]) || 0), 0).toLocaleString();
    document.getElementById('sf-total-sales').textContent =
        rawData.reduce((s, r) => s + (Number(r[5]) || 0), 0).toLocaleString();
    document.getElementById('data-section').style.display = 'block';
    sortCol = null; sortDir = 1;
    rebuildTheadTfoot();
    applyColWidths();
    renderTable();
    sortCol = null; sortDir = 1;
    sortTable(21, 'num');
    document.getElementById('table-wrap').classList.remove('d-none');
    if (fetchSP) patchSharePoint();
    else loadSPFromCache();
}

function resetApplyFilters() {
    const sd = window.SETTINGS_DEFAULTS || {};
    document.getElementById('numberToBuy').value = sd.numberToBuy ?? 200;
    document.getElementById('maxDIS').value      = sd.maxDIS      ?? 90;
    document.getElementById('maxDTS').value      = sd.maxDTS      ?? 45;
    document.getElementById('minGPUM').value     = sd.minGPUM     ?? 0;
    refreshTable();
}

function refreshTable() {
    if (!rawData.length) return;
    const btn = document.getElementById('refreshTableBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Loading...'; }
    // Reset all table filters before re-aggregating
    filterBand.clear(); filterBand.add('All');
    document.querySelectorAll('.filter-btn[data-group="band"]').forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === 'All');
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== 'All');
    });
    filterFuel.clear(); filterFuel.add('All');
    document.querySelectorAll('.filter-btn[data-group="fuel"]').forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === 'All');
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== 'All');
    });
    filterCapId = null;
    const capInp = document.getElementById('capIdSearch');
    if (capInp) capInp.value = '';
    const capClear = document.getElementById('capIdClearBtn');
    if (capClear) capClear.style.display = 'none';
    filterModel = null;
    const modelInp = document.getElementById('modelSearch');
    if (modelInp) modelInp.value = '';
    const modelClear = document.getElementById('modelClearBtn');
    if (modelClear) modelClear.style.display = 'none';
    filterMinReq = null;
    const reqInp = document.getElementById('minReqInput');
    if (reqInp) reqInp.value = '';
    const reqClear = document.getElementById('minReqClearBtn');
    if (reqClear) reqClear.style.display = 'none';
    requestAnimationFrame(() => setTimeout(() => {
        if (currentView === 'model') aggregateModelData(); else aggregateRawData();
        applySpPurchases(spPurchases);
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
            _setSfCachedAt(resp.cached_at);
            _setApexUpdatedAt(rawData);
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
            const ageSeconds = resp.fetched_at ? (Date.now() / 1000 - resp.fetched_at) : Infinity;
            if (ageSeconds > _SP_MAX_AGE_SECS || !resp.fetched_at) {
                patchSharePoint();
                return;
            }
            applySpPurchases(resp.purchases);
            renderTable();
            _setSpUpdatedAt(resp.fetched_at);
        })
        .catch(() => {});
}

function exportXLSX() {
    const fmt1 = v => v != null ? Math.round(v * 10) / 10 : null;
    const fmtN = v => v != null ? Math.round(v) : null;
    const avg  = (sum, n) => n > 0 ? sum / n : null;

    // Aggregate from tableData — same source as the table, so averages match exactly
    // tableData rows: [0:cap_id, 1:label, 2:fuel, 3:stock, 4:sales90, 7:sales7,
    //                  8:avgDIS, 9:avgDTS, 10:avgGPUM, 11:avgGPUS, 12:avgGPUMS,
    //                  14:avgSell, 15:avgATVal, 16:avgATPct, 17:avgATRating, 18:ideal, 19:required,
    //                  22:price_band]
    const groups = new Map();
    for (const r of tableData) {
        const cap_id = r[0];
        const isIF   = r[22] === 'interest_free';
        if (!groups.has(cap_id)) {
            groups.set(cap_id, {
                label: r[1].split(' / ').slice(0, -1).join(' / '),
                fuel: normFuel(r[2]),
                stock: 0, sales90: 0, sales7: 0,
                ideal: 0, required: 0,
                dis_sum: 0, dis_n: 0,
                dts_sum: 0, dts_n: 0,
                gpu_m_sum: 0, gpu_m_n: 0,
                gpu_s_sum: 0, gpu_s_n: 0,
                gpu_ms_sum: 0, gpu_ms_n: 0,
                sell_sum: 0, sell_n: 0,
                at_val_sum: 0, at_val_n: 0,
                at_rating_sum: 0, at_rating_n: 0,
            });
        }
        const g = groups.get(cap_id);
        g.stock    += Number(r[3])  || 0;
        g.sales90  += Number(r[4])  || 0;
        g.sales7   += Number(r[7])  || 0;
        g.ideal    += Number(r[18]) || 0;
        g.required += Number(r[19]) || 0;
        if (r[8]  != null) { g.dis_sum      += Number(r[8]);  g.dis_n++;      }
        if (r[9]  != null) { g.dts_sum      += Number(r[9]);  g.dts_n++;      }
        if (r[10] != null) { g.gpu_m_sum    += Number(r[10]); g.gpu_m_n++;    }
        if (r[11] != null) { g.gpu_s_sum    += Number(r[11]); g.gpu_s_n++;    }
        if (r[12] != null) { g.gpu_ms_sum   += Number(r[12]); g.gpu_ms_n++;   }
        if (r[17] != null) { g.at_rating_sum+= Number(r[17]); g.at_rating_n++;}
        if (!isIF) {
            if (r[14] != null) { g.sell_sum  += Number(r[14]); g.sell_n++;  }
            if (r[15] != null) { g.at_val_sum+= Number(r[15]); g.at_val_n++;}
        }
    }

    const headers = ['CAP ID', 'Required', 'Make Model Derivative', 'Fuel',
        '90d Sales', '7d Sales', 'Stock', 'Ideal Stock', 'Required',
        'Avg DIS', 'Avg DTS',
        'Avg Sell Price (Rate)', 'Avg AT Val (Rate)', 'Avg AT PTM % (Rate)', 'Avg AT Rating',
        'Avg GPU-M', 'Avg GPU-S', 'Avg GPU-MS'];

    const rows = [...groups.entries()].sort((a, b) => a[1].label.localeCompare(b[1].label)).map(([cap_id, g]) => {
        const avgSell  = avg(g.sell_sum,  g.sell_n);
        const avgAtVal = avg(g.at_val_sum, g.at_val_n);
        const atPct    = avgSell != null && avgAtVal != null && avgAtVal !== 0
            ? Math.round(((avgSell) / avgAtVal) * 1000) / 10
            : null;
        return [
            cap_id,
            fmt1(g.required),
            g.label,
            g.fuel,
            g.sales90,
            g.sales7,
            g.stock,
            fmt1(g.ideal),
            fmt1(g.required),
            fmt1(avg(g.dis_sum,       g.dis_n)),
            fmt1(avg(g.dts_sum,       g.dts_n)),
            fmtN(avgSell),
            fmtN(avgAtVal),
            atPct,
            fmt1(avg(g.at_rating_sum, g.at_rating_n)),
            fmtN(avg(g.gpu_m_sum,     g.gpu_m_n)),
            fmtN(avg(g.gpu_s_sum,     g.gpu_s_n)),
            fmtN(avg(g.gpu_ms_sum,    g.gpu_ms_n)),
        ];
    });

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
let _modalSortCol = 'default';
let _modalSortAsc = true;

function _statusRank(r) {
    const s = _modalStatus(r);
    if (s === 'Stock')     return 0;
    if (s === 'Ordered')   return 1;
    if (s === 'Delivered') return 2;
    return 3;
}

function _modalStatus(r) {
    const s = String(r[21] ?? '').toUpperCase();
    if (s === 'ORDERED') return 'Ordered';
    if (s === 'SOLD')    return 'Delivered';
    return 'Stock';
}

function _getModalSortValue(r, col) {
    if (col === 'deriv')     return (r[1].split(' / ').slice(0, -1).join(' / ') || '').toLowerCase();
    if (col === 'price_band') return (r[3] || '').toLowerCase();
    if (col === 'vrm')       return (r[18] || '').toLowerCase();
    if (col === 'supplier')  return (r[19] || '').toLowerCase();
    if (col === 'status')    return _statusRank(r);
    if (col === 'location')  return (r[23] || '').toLowerCase();
    if (col === 'delivered') return r[27] || '';
    if (col === 'dfr')       return r[24] || '';
    if (col === 'mileage')   return Number(r[25]) || 0;
    if (col === 'dis')       return Number(r[7]) || 0;
    if (col === 'dts')       return Number(r[8]) || 0;
    if (col === 'purchase_price') return Number(r[12]) || 0;
    if (col === 'cap_clean')  return Number(r[30]) || 0;
    if (col === 'cap_pct')    return (r[12] && r[30]) ? r[12] / r[30] : 0;
    if (col === 'sell_price') return Number(r[13]) || 0;
    if (col === 'at_ptm')      return Number(r[14]) || 0;
    if (col === 'at_ptm_pct')  return Number(r[15]) || 0;
    if (col === 'at_rating')   return Number(r[16]) || 0;
    if (col === 'gpu_m')       return Number(r[22]) || 0;
    return '';
}

function _sortModalVehicles() {
    _modalVehicles.sort((a, b) => {
        if (_modalSortCol === 'default') {
            const rankCmp = _statusRank(a) - _statusRank(b);
            if (rankCmp !== 0) return rankCmp;
            return Number(b[7]) - Number(a[7]); // DIS high to low
        }
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

    // accumulators for averages row (mileage onwards)
    let mileSum = 0, mileN = 0;
    let disSum  = 0, disN  = 0;
    let dtsSum  = 0, dtsN  = 0;
    let ppSum   = 0, ppN   = 0;
    let capSum  = 0, capN  = 0;
    let sellSum = 0, sellN = 0;
    let ptmSum  = 0, ptmN  = 0;
    let ratingSum = 0, ratingN = 0;
    let gpumSum = 0, gpumN  = 0;

    const visibleVehicles = _modalBand === 'All' ? _modalVehicles : _modalVehicles.filter(r => r[3] === _modalBand);
    visibleVehicles.forEach(r => {
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
        addTd(r[3] === 'interest_free' ? '0%' : (r[3] || '—'));
        addTd(r[18] || '—');
        addTd(status);
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
        addTd(r[27] ? r[27].slice(0, 10).split('-').reverse().join('/') : '');
        addTd(r[19] || '—');
        addTd(r[24] ? r[24].slice(0, 10).split('-').reverse().join('/') : '—');
        addTd(r[25] != null ? Number(r[25]).toLocaleString() : '—', 'right');
        addTd(r[7]  != null ? fmtInt(r[7])  : '—', 'right');
        addTd(r[8]  != null ? fmtInt(r[8])  : '—', 'right');
        addTd(r[12] != null && r[12] !== 0 ? fmtGBP(r[12]) : '—', 'right');
        addTd(r[30] != null && r[30] !== 0 ? fmtGBP(r[30]) : '—', 'right');
        const capPct = (r[12] && r[30]) ? ((r[12] / r[30]) * 100).toFixed(1) + '%' : '—';
        addTd(capPct, 'right');
        addTd(r[13] != null && r[13] !== 0 ? fmtGBP(r[13]) : '—', 'right');
        addTd(r[14] != null ? fmtGBP(r[14]) : '—', 'right');
        addTd(r[13] != null && r[13] !== 0 && r[14] != null && r[14] !== 0 ? fmtPct((r[13] / r[14]) * 100) : '—', 'right');
        addTd(r[16] != null ? fmtInt(r[16]) : '—', 'right');
        { const td = addTd(r[22] != null ? fmtGBP(r[22]) : '—', 'right'); if (r[22] != null && r[22] < 0) td.style.color = '#dc2626'; }
        if (status === 'Delivered') tr.classList.add('vehicle-row-sold');
        else if (status === 'Ordered') tr.classList.add('vehicle-row-ordered');
        else if (r[3] === 'interest_free') tr.classList.add('vehicle-row-if');
        tbody.appendChild(tr);

        // accumulate averages
        if (r[25] != null)                        { mileSum += Number(r[25]); mileN++; }
        if (r[7]  != null)                        { disSum  += Number(r[7]);  disN++;  }
        if (r[8]  != null)                        { dtsSum  += Number(r[8]);  dtsN++;  }
        if (r[12] != null && r[12] !== 0)         { ppSum   += Number(r[12]); ppN++;   }
        if (r[30] != null && r[30] !== 0)         { capSum  += Number(r[30]); capN++;  }
        if (r[13] != null && r[13] !== 0)         { sellSum += Number(r[13]); sellN++; }
        if (r[14] != null)                        { ptmSum  += Number(r[14]); ptmN++;  }
        if (r[16] != null)                        { ratingSum += Number(r[16]); ratingN++; }
        if (r[22] != null)                        { gpumSum += Number(r[22]); gpumN++; }
    });

    // build averages tfoot row
    const avgRow = document.getElementById('vehicle-modal-avg-row');
    avgRow.innerHTML = '';
    const addAvgTd = (text, align, blank) => {
        const td = document.createElement('td');
        td.textContent = blank ? '' : text;
        if (blank) td.classList.add('tfoot-blank');
        if (align) td.style.textAlign = align;
        avgRow.appendChild(td);
    };
    const avgMile = mileN ? Math.round(mileSum / mileN) : null;
    const avgDIS  = disN  ? disSum  / disN  : null;
    const avgDTS  = dtsN  ? dtsSum  / dtsN  : null;
    const avgPP   = ppN   ? ppSum   / ppN   : null;
    const avgCap  = capN  ? capSum  / capN  : null;
    const avgSell = sellN ? sellSum / sellN : null;
    const avgPTM  = ptmN  ? ptmSum  / ptmN  : null;
    const avgRating = ratingN ? ratingSum / ratingN : null;
    const avgGPUM = gpumN ? gpumSum / gpumN : null;
    const avgCapPct  = (avgPP  && avgCap)  ? ((avgPP  / avgCap)  * 100) : null;
    const avgPTMPct  = (avgSell && avgPTM) ? (((avgSell) / avgPTM) * 100) : null;

    addAvgTd('', null, true);   // deriv
    addAvgTd('', null, true);   // price band
    addAvgTd('', null, true);   // vrm
    addAvgTd('', null, true);   // status
    addAvgTd('', null, true);   // apex
    addAvgTd('', null, true);   // portal
    addAvgTd('', null, true);   // location
    addAvgTd('', null, true);   // delivered
    addAvgTd('', null, true);   // supplier
    addAvgTd('Avg', 'left');    // dfr — label cell
    addAvgTd(avgMile != null ? avgMile.toLocaleString() : '—', 'right');
    addAvgTd(avgDIS  != null ? fmtInt(avgDIS)  : '—', 'right');
    addAvgTd(avgDTS  != null ? fmtInt(avgDTS)  : '—', 'right');
    addAvgTd(avgPP   != null ? fmtGBP(avgPP)   : '—', 'right');
    addAvgTd(avgCap  != null ? fmtGBP(avgCap)  : '—', 'right');
    addAvgTd(avgCapPct  != null ? avgCapPct.toFixed(1)  + '%' : '—', 'right');
    addAvgTd(avgSell != null ? fmtGBP(avgSell) : '—', 'right');
    addAvgTd(avgPTM  != null ? fmtGBP(avgPTM)  : '—', 'right');
    addAvgTd(avgPTMPct  != null ? fmtPct(avgPTMPct)  : '—', 'right');
    addAvgTd(avgRating != null ? fmtInt(avgRating) : '—', 'right');
    { const td = addAvgTd(avgGPUM != null ? fmtGBP(avgGPUM) : '—', 'right'); if (avgGPUM != null && avgGPUM < 0) td.style.color = '#dc2626'; }

}

let _modalBand = 'All';

function _buildModalFilterBar(bands) {
    const bar = document.getElementById('vehicle-modal-filter-bar');
    bar.innerHTML = '';
    const allBandOrder = ['<10k','10-12.5k','12.5-15k','15-20k','20-25k','25-30k','30k+','interest_free'];
    const sorted = ['All', ...allBandOrder.filter(b => bands.has(b))];
    sorted.forEach(val => {
        const btn = document.createElement('button');
        btn.className = 'btn btn-sm ' + (val === _modalBand ? 'btn-dark' : 'btn-outline-secondary') + ' modal-band-btn';
        btn.textContent = val === 'interest_free' ? '0%' : val;
        btn.onclick = () => {
            _modalBand = val;
            bar.querySelectorAll('.modal-band-btn').forEach(b => {
                b.classList.toggle('btn-dark', b.textContent === (val === 'interest_free' ? '0%' : val) && val === _modalBand);
                b.classList.toggle('btn-outline-secondary', !(b.textContent === (val === 'interest_free' ? '0%' : val) && val === _modalBand));
            });
            // re-sync button states cleanly
            bar.querySelectorAll('.modal-band-btn').forEach((b, i) => {
                const bVal = sorted[i];
                b.classList.toggle('btn-dark', bVal === _modalBand);
                b.classList.toggle('btn-outline-secondary', bVal !== _modalBand);
            });
            _sortModalVehicles();
            _renderModalRows();
        };
        bar.appendChild(btn);
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
        if (r[0] !== capId) return false;
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
    const bands = new Set(_modalVehicles.map(r => r[3]).filter(Boolean));
    _modalBand = 'All';
    _buildModalFilterBar(bands);
    _modalSortCol = 'default';
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

document.addEventListener('DOMContentLoaded', () => loadFromServer(false));