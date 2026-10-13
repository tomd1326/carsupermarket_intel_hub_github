/* ── Formatters ──────────────────────────────────────────────────────────── */
function fmtGBP(v) {
    if (v == null) return '—';
    // Sign before the symbol: -£1,234, never £-1,234.
    const n = Number(v);
    return (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function vehicleStatus(raw) {
    const s = String(raw ?? '').toUpperCase();
    if (s === 'ORDERED') return 'Ordered';
    if (s === 'SOLD')    return 'Delivered';
    return 'Stock';
}
function statusRank(status) {
    if (status === 'Stock')     return 0;
    if (status === 'Ordered')   return 1;
    if (status === 'Delivered') return 2;
    return 3;
}
function fmtDate(v) {
    return v ? String(v).slice(0, 10).split('-').reverse().join('/') : '—';
}

// Formats a column can use, set per column in Settings.
const COLUMN_FORMATS = [
    ['text', 'Text'], ['number', 'Number'], ['gbp', '£'], ['pct', '%'],
    ['date', 'Date'], ['link', 'Link button'], ['status', 'Stock / Ordered / Delivered'],
];
const NUMBER_FORMATS = ['number', 'gbp', 'pct'];
const DEFAULT_WIDTH = 90;

/* ── State ───────────────────────────────────────────────────────────────── */
let allRows       = [];
let colIdx        = {};    // column name -> position in a data row
let viewColumns   = [];    // Settings columns: [{column, label, format, decimals, width}]
let sortCol       = null;  // a column name
let sortAsc       = true;
let currentFontPx = 12;
// Chosen value per button group: 'status', plus 'col:<COLUMN>' for each
// Settings filter. 'All' means not filtering.
const activeFilters = { status: 'All' };
const missingFilters = { cap: false, at: false };   // No CAP Data / No AT Data toggles
let customFilters = [];    // Settings filters: [{column, label, description}]
const FILTER_DROPDOWN_OVER = 15;   // more values than this -> a dropdown, not buttons
const BLANK_KEY = '__BLANK__';

// A row's value for a column name, or null where the data has no such column.
function val(r, name) {
    const i = colIdx[name];
    return i === undefined ? null : r[i];
}
function derivText(r) {
    return ((val(r, 'MAKE_MODEL') || '') + ' ' + (val(r, 'CAP_DERIVATIVE') || '')).toLowerCase();
}

/* ── Snowflake status ────────────────────────────────────────────────────── */
function setStatus(state) {
    const el = document.getElementById('sf-status');
    if (!el) return;
    const label = '<i class="fas fa-snowflake me-1 text-info"></i>Snowflake';
    const map = {
        idle:    `${label} <span class="text-muted">—</span>`,
        running: `${label} <span class="spinner-border spinner-border-sm ms-1" style="width:.7rem;height:.7rem"></span>`,
        done:    `${label} <i class="fas fa-check-circle text-success ms-1"></i>`,
        error:   `${label} <i class="fas fa-times-circle text-danger ms-1"></i>`,
    };
    el.innerHTML = map[state] ?? map.idle;
}

/* ── Fetch ───────────────────────────────────────────────────────────────── */
async function fetchData(force = false) {
    const btn = document.getElementById('fetchBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-sync-alt spin me-1"></i>Refresh Data'; }
    document.body.classList.add('page-is-loading');
    document.getElementById('data-section').style.display  = 'none';
    document.getElementById('table-wrap').classList.add('d-none');
    document.getElementById('table-error').classList.add('d-none');
    setStatus('running');
    try {
        const url  = BASE_URL + '/stock-list/data' + (force ? '?force=true' : '');
        const res  = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Unknown error');
        allRows = data.rows;
        colIdx  = Object.fromEntries(data.columns.map((c, i) => [c, i]));
        // Columns saved in Settings but not in the table yet are left off.
        viewColumns = data.view.filter(c => c.column in colIdx);
        if (!viewColumns.some(c => c.column === sortCol)) {
            sortCol = viewColumns.some(c => c.column === 'MAKE_MODEL_DERIVATIVE')
                ? 'MAKE_MODEL_DERIVATIVE' : (viewColumns[0] || {}).column || null;
            sortAsc = true;
        }
        customFilters = data.filters || [];
        buildHeader();
        buildCustomFilters();
        setStatus('done');
        const sfEl = document.getElementById('sf-status');
        sfEl.title = data.cached_at ? 'Cached at ' + new Date(data.cached_at * 1000).toLocaleTimeString('en-GB') : '';
        if (data.missing && data.missing.length) {
            sfEl.title += (sfEl.title ? '\n' : '') + 'Not in the table, so not shown: ' + data.missing.join(', ');
        }
        // Apex updated at — latest VEHICLE_UPDATED_AT across all rows
        let maxApex = null;
        allRows.forEach(r => {
            const v = val(r, 'VEHICLE_UPDATED_AT');
            if (v && (!maxApex || v > maxApex)) maxApex = v;
        });
        if (maxApex) {
            const d = new Date(String(maxApex).replace(' ', 'T'));
            document.getElementById('apex-updated-val').textContent =
                d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
            document.getElementById('apex-updated').style.display = '';
        }
        document.body.classList.remove('page-is-loading');
        document.getElementById('data-section').style.display  = '';
        document.getElementById('table-wrap').classList.remove('d-none');
        applyFilters();
        fitTableWrap();
    } catch (e) {
        setStatus('error');
        document.body.classList.remove('page-is-loading');
        const errEl = document.getElementById('table-error');
        errEl.textContent = 'Failed to load data: ' + e.message;
        errEl.classList.remove('d-none');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Refresh Data'; }
    }
}

/* ── Summary bar ─────────────────────────────────────────────────────────── */
function updateSummary(filtered) {
    // Delivered vehicles are left out by the server, so only these two remain.
    let stock = 0, ordered = 0;
    filtered.forEach(r => {
        const s = vehicleStatus(val(r, 'VEHICLE_STATUS'));
        if (s === 'Stock')        stock++;
        else if (s === 'Ordered') ordered++;
    });
    document.getElementById('sum-stock').textContent   = stock.toLocaleString('en-GB');
    document.getElementById('sum-ordered').textContent = ordered.toLocaleString('en-GB');
    const anyFilter = Object.values(activeFilters).some(v => v !== 'All') ||
                      missingFilters.cap || missingFilters.at ||
                      (document.getElementById('searchInput')?.value ?? '') !== '';
    document.getElementById('sum-filtered-note').style.display = anyFilter ? '' : 'none';
}

/* ── Filters ─────────────────────────────────────────────────────────────── */
function applyFilters() {
    const search  = (document.getElementById('searchInput')?.value ?? '').toLowerCase();

    const filtered = allRows.filter(r => {
        const status = vehicleStatus(val(r, 'VEHICLE_STATUS'));
        if (activeFilters.status !== 'All' && status !== activeFilters.status) return false;
        for (const f of customFilters) {
            const want = activeFilters['col:' + f.column];
            if (want && want !== 'All' && filterKey(val(r, f.column)) !== want) return false;
        }
        if (missingFilters.cap && val(r, 'CAP_LIVE_CLEAN_LATEST') != null)              return false;
        if (missingFilters.at  && val(r, 'AUTOTRADER_LATEST_RETAIL_VALUATION') != null) return false;
        if (search) {
            const vrm = (val(r, 'VRM') || '').toLowerCase();
            if (!derivText(r).includes(search) && !vrm.includes(search)) return false;
        }
        return true;
    });
    updateSummary(filtered);
    renderTable(filtered);
}

/* ── Settings filters (one button group per column) ─────────────────────── */
// The value a row is matched on: the cell as text, blank for empty.
function filterKey(v) {
    return v == null || v === '' ? BLANK_KEY : String(v);
}
function filterValueText(key) {
    if (key === BLANK_KEY) return '(blank)';
    if (key === 'interest_free') return '0%';
    return key;
}

function buildCustomFilters() {
    const holder = document.getElementById('custom-filters');
    holder.innerHTML = '';
    const esc = ColumnEditor.esc;
    customFilters.forEach(f => {
        const group = 'col:' + f.column;
        // Every value found in the loaded data, numbers in number order.
        const keys = [...new Set(allRows.map(r => filterKey(val(r, f.column))))];
        const numeric = keys.every(k => k === BLANK_KEY || !isNaN(Number(k)));
        keys.sort((a, b) => {
            if (a === BLANK_KEY) return 1;
            if (b === BLANK_KEY) return -1;
            return numeric ? Number(a) - Number(b) : a.localeCompare(b);
        });
        if (!keys.includes(activeFilters[group])) activeFilters[group] = 'All';
        const chosen = activeFilters[group];

        const title = esc(f.label || f.column) + (f.description
            ? ` <span class="info-mark" data-info="${esc(f.description)}">i</span>` : '');
        let controls;
        if (keys.length > FILTER_DROPDOWN_OVER) {
            controls = `<select class="form-select form-select-sm filter-select" data-group="${esc(group)}">
                <option value="All">All</option>
                ${keys.map(k => `<option value="${esc(k)}" ${k === chosen ? 'selected' : ''}>${esc(filterValueText(k))}</option>`).join('')}
            </select>`;
        } else {
            controls = ['All', ...keys].map(k => `<button class="btn btn-sm ${k === chosen ? 'btn-dark' : 'btn-outline-secondary'} filter-btn"
                data-group="${esc(group)}" data-value="${esc(k)}">${esc(k === 'All' ? 'All' : filterValueText(k))}</button>`).join('');
        }
        const div = document.createElement('div');
        div.className = 'filter-group titled';
        div.innerHTML = `<span class="filter-title">${title}</span><div class="filter-buttons">${controls}</div>`;
        holder.appendChild(div);
    });
}

document.addEventListener('change', e => {
    const sel = e.target.closest('.filter-select');
    if (!sel) return;
    activeFilters[sel.dataset.group] = sel.value;
    applyFilters();
});

function clearAllFilters() {
    Object.keys(activeFilters).forEach(k => { activeFilters[k] = 'All'; });
    document.querySelectorAll('.filter-select').forEach(sel => { sel.value = 'All'; });
    missingFilters.cap = missingFilters.at = false;
    document.querySelectorAll('.missing-btn').forEach(btn => {
        btn.classList.remove('btn-dark');
        btn.classList.add('btn-outline-secondary');
    });
    document.getElementById('searchInput').value = '';
    document.querySelectorAll('.filter-btn').forEach(btn => {
        const isAll = btn.dataset.value === 'All';
        btn.classList.toggle('btn-dark', isAll);
        btn.classList.toggle('btn-outline-secondary', !isAll);
    });
    applyFilters();
}

document.addEventListener('click', e => {
    const btn = e.target.closest('.missing-btn');
    if (!btn) return;
    const key = btn.dataset.missing;
    missingFilters[key] = !missingFilters[key];
    btn.classList.toggle('btn-dark', missingFilters[key]);
    btn.classList.toggle('btn-outline-secondary', !missingFilters[key]);
    applyFilters();
});

document.addEventListener('click', e => {
    const btn = e.target.closest('.filter-btn');
    if (!btn) return;
    const group = btn.dataset.group;
    const value = btn.dataset.value;
    if (!group || !value) return;
    activeFilters[group] = value;
    document.querySelectorAll(`.filter-btn[data-group="${CSS.escape(group)}"]`).forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === value);
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== value);
    });
    applyFilters();
});

/* ── Sort ────────────────────────────────────────────────────────────────── */
function getSortValue(r, c) {
    const v = val(r, c.column);
    if (c.format === 'status') return statusRank(vehicleStatus(v));
    if (NUMBER_FORMATS.includes(c.format)) return Number(v) || 0;
    return String(v ?? '').toLowerCase();
}

function sortRows(rows) {
    const c = viewColumns.find(col => col.column === sortCol);
    if (!c) return rows;
    return [...rows].sort((a, b) => {
        const av = getSortValue(a, c);
        const bv = getSortValue(b, c);
        let cmp = av < bv ? -1 : av > bv ? 1 : 0;
        // secondary: always alphabetical on make / model / derivative
        if (cmp === 0) {
            const ad = derivText(a), bd = derivText(b);
            cmp = ad < bd ? -1 : ad > bd ? 1 : 0;
        }
        return sortAsc ? cmp : -cmp;
    });
}

function updateSortIndicators() {
    document.querySelectorAll('#stock-table thead th[data-sort]').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        const arrow = th.querySelector('.sort-arrow');
        if (!arrow) return;
        if (th.dataset.sort === sortCol) {
            th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
            arrow.textContent = sortAsc ? ' ▲' : ' ▼';
            arrow.style.opacity = '1';
        } else {
            arrow.textContent = '';
            arrow.style.opacity = '0.35';
        }
    });
}

/* ── Header (built from the Settings columns) ────────────────────────────── */
function buildHeader() {
    const colgroup = document.getElementById('stock-cols');
    const headRow  = document.getElementById('stock-head');
    colgroup.innerHTML = '';
    headRow.innerHTML  = '';
    viewColumns.forEach(c => {
        const col = document.createElement('col');
        col.style.width = (c.width || DEFAULT_WIDTH) + 'px';
        colgroup.appendChild(col);

        const th = document.createElement('th');
        th.title = c.column;
        if (c.format !== 'link') {
            th.dataset.sort = c.column;
            th.addEventListener('click', e => {
                if (e.target.classList.contains('resize-handle')) return;
                if (sortCol === c.column) sortAsc = !sortAsc;
                else { sortCol = c.column; sortAsc = true; }
                updateSortIndicators();
                applyFilters();
            });
        }
        th.appendChild(document.createTextNode(c.label || c.column));
        if (c.format !== 'link') {
            const arrow = document.createElement('span');
            arrow.className = 'sort-arrow';
            th.appendChild(arrow);
        }
        headRow.appendChild(th);
    });
    initResize();
    updateSortIndicators();
    syncTableWidth();
}

/* ── Column resize ───────────────────────────────────────────────────────── */
// The table scrolls inside .table-wrap both ways. Its horizontal scrollbar sits
// at the bottom of that box, so the box is sized to end at the bottom of the
// window — otherwise the bar is below the fold and only reachable after
// scrolling the whole page down.
const TABLE_BOTTOM_GAP = 32;   // white space left between the table and the window bottom, px
function fitTableWrap() {
    const wrap = document.getElementById('table-wrap');
    if (wrap.classList.contains('d-none')) return;
    const top = wrap.getBoundingClientRect().top + window.scrollY;
    wrap.style.maxHeight = Math.max(200, window.innerHeight - top - TABLE_BOTTOM_GAP) + 'px';
}
window.addEventListener('resize', fitTableWrap);

function syncTableWidth() {
    const table = document.getElementById('stock-table');
    const cols  = table.querySelectorAll('colgroup col');
    let total = 0;
    cols.forEach(c => { total += parseInt(c.style.width) || 0; });
    table.style.width = total + 'px';
}

function initResize() {
    const ghost   = document.getElementById('resize-ghost');
    const table   = document.getElementById('stock-table');
    const cols    = Array.from(table.querySelectorAll('colgroup col'));

    document.querySelectorAll('#stock-table thead th').forEach((th, colIdx) => {
        const handle = document.createElement('div');
        handle.className = 'resize-handle';
        th.appendChild(handle);

        let startX, startW, rafId;

        handle.addEventListener('mousedown', e => {
            e.stopPropagation();
            e.preventDefault();
            startX = e.clientX;
            startW = th.offsetWidth;
            ghost.style.left    = e.clientX + 'px';
            ghost.style.display = 'block';
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';

            let lastX = startX;
            let dragged = false;

            function onMove(e) {
                dragged = true;
                lastX = e.clientX;
                cancelAnimationFrame(rafId);
                rafId = requestAnimationFrame(() => {
                    ghost.style.left = lastX + 'px';
                });
            }
            function cleanup(commitX) {
                cancelAnimationFrame(rafId);
                ghost.style.display = 'none';
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup',   onUp);
                document.removeEventListener('click',     suppressClick, true);
                window.removeEventListener('blur',        onBlur);
                if (commitX !== null && dragged) {
                    const newW = Math.max(30, startW + (commitX - startX));
                    if (cols[colIdx]) cols[colIdx].style.width = newW + 'px';
                    syncTableWidth();
                }
            }
            // Capture-phase click suppressor — fires before the th sort handler
            function suppressClick(e) { e.stopPropagation(); }
            function onUp(e)  { cleanup(e.clientX); }
            function onBlur() { cleanup(null); }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',   onUp);
            document.addEventListener('click',     suppressClick, true);
            window.addEventListener('blur',        onBlur);
        });
    });
}

/* ── Render ──────────────────────────────────────────────────────────────── */
function fillCell(td, c, v) {
    const dp = Number.isInteger(c.decimals) ? c.decimals : 0;
    switch (c.format) {
        case 'gbp':
            td.className = 'col-right';
            td.textContent = fmtGBP(v);
            if (v != null && v < 0) td.style.color = '#dc2626';
            return;
        case 'number':
            td.className = 'col-right';
            td.textContent = v == null ? '—'
                : Number(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
            return;
        case 'pct':
            td.className = 'col-right';
            td.textContent = v == null ? '—' : (Number(v) * 100).toFixed(dp) + '%';
            return;
        case 'date':
            td.className = 'col-left';
            td.textContent = fmtDate(v);
            return;
        case 'status':
            td.className = 'col-left';
            td.textContent = vehicleStatus(v);
            return;
        case 'link':
            td.className = 'col-center';
            if (v) {
                const a = document.createElement('a');
                a.href = v; a.target = '_blank';
                a.textContent = c.label || 'Open'; a.className = 'apex-link';
                td.appendChild(a);
            }
            return;
        default:
            td.className = 'col-left';
            td.textContent = v === 'interest_free' ? '0%' : (v == null || v === '' ? '—' : String(v));
    }
}

let shownRows = [];   // the rows in the table, filtered and sorted — what the CSV exports

function renderTable(rows) {
    const sorted = sortRows(rows);
    shownRows = sorted;
    const tbody  = document.getElementById('stock-tbody');
    tbody.innerHTML = '';

    sorted.forEach(r => {
        const status = vehicleStatus(val(r, 'VEHICLE_STATUS'));
        const tr     = document.createElement('tr');

        if (status === 'Delivered')       tr.classList.add('vehicle-row-sold');
        else if (status === 'Ordered')    tr.classList.add('vehicle-row-ordered');
        else if (val(r, 'PRICE_BAND') === 'interest_free') tr.classList.add('vehicle-row-if');

        viewColumns.forEach(c => {
            const td = document.createElement('td');
            fillCell(td, c, val(r, c.column));
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });

    document.getElementById('row-count').textContent =
        sorted.length.toLocaleString('en-GB') + ' vehicles';
}

/* ── CSV export ──────────────────────────────────────────────────────────── */
function csvEscape(v) {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// The rows and columns on screen, with the Settings headings. Raw values (not
// the formatted display) so numbers stay numeric; status and the 0% price band
// are written as shown on the page.
function csvValue(c, v) {
    if (c.format === 'status') return vehicleStatus(v);
    if (c.format === 'date' && v) return String(v).slice(0, 10);
    if (v === 'interest_free') return '0%';
    return v;
}

function downloadCsv() {
    const header = viewColumns.map(c => csvEscape(c.label || c.column)).join(',');
    const lines = shownRows.map(r => viewColumns.map(c => csvEscape(csvValue(c, val(r, c.column)))).join(','));
    const blob = new Blob(['﻿' + [header, ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `stock_list_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

/* ── Font size ───────────────────────────────────────────────────────────── */
function changeFontSize(delta) {
    currentFontPx = Math.min(16, Math.max(8, Math.round((currentFontPx + delta * 0.5) * 10) / 10));
    document.getElementById('stock-table').style.fontSize = currentFontPx + 'px';
    document.getElementById('fontSizeLabel').textContent  = currentFontPx + 'px';
}

/* ── Settings: columns and filters (admin only) ──────────────────────────── */
let columnEditor = null;
let filterDraft = [];      // [{column, label, description}] being edited
let filterLiveColumns = []; // every column of the table, for the filter dropdowns

function renderFilterDraft() {
    const esc = ColumnEditor.esc;
    const names = filterLiveColumns.map(c => c.column);
    const last = filterDraft.length - 1;
    document.getElementById('filterEditor').innerHTML = filterDraft.map((f, i) => {
        // A saved column no longer in the table still shows, marked.
        const cols = (f.column && !names.includes(f.column) ? [f.column] : []).concat(names);
        return `
            <div class="filter-edit-row">
                <select title="Column the filter's buttons come from" onchange="filterDraft[${i}].column = this.value">
                    <option value="">(choose a column)</option>
                    ${cols.map(c => `<option value="${esc(c)}" ${c === f.column ? 'selected' : ''}>${esc(c)}${names.includes(c) ? '' : ' (not in the table)'}</option>`).join('')}
                </select>
                <input type="text" class="f-label" maxlength="60" value="${esc(f.label || '')}" placeholder="Title (empty: column name)"
                       oninput="filterDraft[${i}].label = this.value">
                <input type="text" class="f-desc" maxlength="300" value="${esc(f.description || '')}" placeholder="Description (shown on the i)"
                       oninput="filterDraft[${i}].description = this.value">
                <button type="button" class="btn btn-sm btn-outline-secondary" title="Move up" onclick="moveFilterDraft(${i}, -1)"
                        ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                <button type="button" class="btn btn-sm btn-outline-secondary" title="Move down" onclick="moveFilterDraft(${i}, 1)"
                        ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" onclick="filterDraft.splice(${i}, 1); renderFilterDraft()">
                    <i class="fas fa-times"></i></button>
            </div>`;
    }).join('') || '<div class="text-muted small">No filters added yet.</div>';
}

function addFilterDraft() {
    filterDraft.push({ column: '', label: '', description: '' });
    renderFilterDraft();
}

function moveFilterDraft(i, step) {
    const j = i + step;
    if (j < 0 || j >= filterDraft.length) return;
    [filterDraft[i], filterDraft[j]] = [filterDraft[j], filterDraft[i]];
    renderFilterDraft();
}

function settingsError(message) {
    const el = document.getElementById('settings-error');
    el.textContent = message || '';
    el.classList.toggle('d-none', !message);
}

async function openSettings() {
    settingsError('');
    columnEditor = null;
    filterDraft = [];
    document.getElementById('colEditor').innerHTML = '<div class="text-muted small">Loading columns&hellip;</div>';
    document.getElementById('filterEditor').innerHTML = '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('settingsModal')).show();
    try {
        const resp = await fetch(BASE_URL + '/stock-list/columns').then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Could not read the columns');
        // Widths dragged on the page since loading are kept when saving.
        const widths = {};
        document.querySelectorAll('#stock-cols col').forEach((col, i) => {
            if (viewColumns[i]) widths[viewColumns[i].column] = parseInt(col.style.width) || DEFAULT_WIDTH;
        });
        columnEditor = new ColumnEditor(document.getElementById('colEditor'), {
            live: resp.columns,
            shown: resp.view.map(c => ({ ...c, width: widths[c.column] || c.width || DEFAULT_WIDTH })),
            formats: COLUMN_FORMATS,
            numberFormats: NUMBER_FORMATS,
            newColumn: (column, type) => ({
                column, label: '', decimals: 0, width: DEFAULT_WIDTH,
                format: ['NUMBER', 'FLOAT'].includes(type) ? 'number'
                      : ['DATE', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ'].includes(type) ? 'date' : 'text',
            }),
        });
        filterLiveColumns = resp.columns;
        filterDraft = (resp.filters || []).map(f => ({ ...f }));
        renderFilterDraft();
    } catch (e) {
        document.getElementById('colEditor').innerHTML = '';
        settingsError(e.message);
    }
}

async function saveSettings() {
    if (!columnEditor) return;
    const btn = document.getElementById('settingsSaveBtn');
    btn.disabled = true;
    settingsError('');
    try {
        // Filters first: a filter with no column chosen stops the save before
        // anything is written.
        const filters = filterDraft.map(f => ({
            column: f.column, label: (f.label || '').trim(), description: (f.description || '').trim() }));
        const fResp = await fetch(BASE_URL + '/stock-list/filters', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filters }),
        }).then(r => r.json());
        if (fResp.status !== 'ok') throw new Error(fResp.message || 'Filters save failed');
        const resp = await fetch(BASE_URL + '/stock-list/columns', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ columns: columnEditor.columns() }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
        bootstrap.Modal.getOrCreateInstance(document.getElementById('settingsModal')).hide();
        fetchData();
    } catch (e) {
        settingsError(e.message);
    } finally {
        btn.disabled = false;
    }
}

/* ── Boot ────────────────────────────────────────────────────────────────── */
document.getElementById('searchInput').addEventListener('keydown', e => { if (e.key === 'Enter') applyFilters(); });

fetchData();
