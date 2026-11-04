/* All Cars Bought: every car submitted on this site, on its own full page.
 * Search, filters, date range and sort all run in the browser. The CSV holds
 * exactly the cars and columns the page has narrowed down to. */

let allRows = [];
let allSort = {column: 'PURCHASE_DATE', dir: -1};
let allFilters = {};
let allLimit = 0;
let allFilterTimer = null;

// What the search box can look in. The first is the default.
const SEARCH_BY = {
    vrm: {label: 'VRM', value: r => r.vrm},
    supplier: {label: 'Supplier', value: r => r.supplier_name},
    cap: {label: 'CAP ID', value: r => r.cap_id},
    all: {label: 'All Columns'},
};

const customKey = c => c.column.startsWith('CUSTOM:') ? c.column.slice(7).toLowerCase() : null;
const customField = c => customFields.find(f => f.key === customKey(c));
const metricKey = c => c.column.startsWith('METRIC:') ? c.column.slice(7).toLowerCase() : null;
const metricOf = c => (buyerCfg.metrics || []).find(m => m.id === metricKey(c));
const isFigure = c => ['gbp', 'number', 'pct'].includes(c.format);
let metricValues = {};     // {metric id: {vehicle id: {value, rows, matched, level}}}
let metricErrors = {};     // {metric id: message}

// Columns chosen in Settings, less any whose custom field or metric has gone or is off.
const allColumns = () => (buyerCfg.table_columns || []).filter(c => {
    if (customKey(c)) return !!customField(c);
    if (metricKey(c)) { const m = metricOf(c); return !!(m && m.enabled); }
    return true;
});

function allValue(r, c) {
    const key = customKey(c);
    if (key) return (r.custom || {})[key];
    const metric = metricKey(c);
    if (metric) { const hit = (metricValues[metric] || {})[r.vehicle_id]; return hit ? hit.value : null; }
    return r[c.column.toLowerCase()];
}

function allText(r, c) {
    const v = allValue(r, c);
    if (v === null || v === undefined || v === '') return '';
    if (c.format === 'date') return ukDate(String(v));
    const n = num(v);
    if (n !== null && c.format === 'gbp') return '£' + n.toLocaleString('en-GB', {minimumFractionDigits: c.decimals ?? 2, maximumFractionDigits: c.decimals ?? 2});
    if (n !== null && (c.format === 'number' || c.format === 'pct')) {
        return n.toLocaleString('en-GB', {minimumFractionDigits: c.decimals ?? 0, maximumFractionDigits: c.decimals ?? 0}) + (c.format === 'pct' ? '%' : '');
    }
    return String(v);
}

// What a figure is based on, for hovering over it.
function metricTitle(r, c) {
    const m = metricOf(c);
    const hit = (metricValues[m.id] || {})[r.vehicle_id];
    const about = m.description ? m.description + '\n' : '';
    return about + (hit ? `From ${hit.rows} rows, matched on ${hit.matched}` : `No figure: fewer than ${m.min_rows} rows matched`);
}

/* One request per metric that is on the table. Nothing is asked for when no
   metric is switched on. */
function loadMetricValues() {
    const wanted = allColumns().filter(metricKey).map(metricOf);
    metricErrors = {};
    wanted.forEach(m => {
        fetch(`${BASE}/cars-bought/metric-values/${encodeURIComponent(m.id)}`).then(r => r.json()).then(res => {
            if (res.status === 'ok') { metricValues[m.id] = res.values || {}; delete metricErrors[m.id]; }
            else metricErrors[m.id] = res.message;
            refreshAllBody();
        }).catch(err => { metricErrors[m.id] = `${m.label}: ${err.message}`; refreshAllBody(); });
    });
}

// Figures can arrive while a buyer is typing in the table, and redrawing would lose the box.
function refreshAllBody() {
    const typing = document.activeElement;
    if (typing && $('allBody').contains(typing)) { setTimeout(refreshAllBody, 1500); return; }
    renderAllBody();
}

function allCompare(a, b, c) {
    const x = allValue(a, c), y = allValue(b, c);
    const blankX = x === null || x === undefined || x === '', blankY = y === null || y === undefined || y === '';
    if (blankX || blankY) return blankX === blankY ? 0 : (blankX ? 1 : -1) * allSort.dir;   // blanks always last
    if (isFigure(c)) return (num(x) ?? 0) - (num(y) ?? 0);
    return String(x).localeCompare(String(y));
}

// Spaces are ignored, so "AB12 CDE" finds AB12CDE.
const searchWords = s => String(s ?? '').replace(/\s+/g, '').toLowerCase();

function matchesSearch(r, cols) {
    const search = searchWords($('allSearch').value);
    if (!search) return true;
    const by = SEARCH_BY[$('allSearchBy').value];
    if (by.value) return searchWords(by.value(r)).includes(search);
    return cols.some(c => searchWords(allText(r, c)).includes(search));
}

function matchesDates(r) {
    const day = String(r.purchase_date || '').slice(0, 10);
    const from = $('allFrom').value, to = $('allTo').value;
    return (!from || day >= from) && (!to || day <= to);
}

function allFiltered() {
    const cols = allColumns();
    const active = cols.filter(c => (allFilters[c.column] || '').trim());
    const out = allRows.filter(r =>
        active.every(c => allText(r, c).toLowerCase().includes(allFilters[c.column].trim().toLowerCase()))
        && matchesDates(r) && matchesSearch(r, cols));
    const sortCol = cols.find(c => c.column === allSort.column);
    if (sortCol) out.sort((a, b) => allCompare(a, b, sortCol) * allSort.dir);
    return out;
}

/* Every search, date and filter set now, in words. Empty when none are. */
function activeFilters() {
    const out = [];
    const search = $('allSearch').value.trim();
    if (search) out.push(`${SEARCH_BY[$('allSearchBy').value].label} Search: ${search}`);
    if ($('allFrom').value) out.push(`From ${ukDate($('allFrom').value)}`);
    if ($('allTo').value) out.push(`To ${ukDate($('allTo').value)}`);
    allColumns().forEach(c => {
        const text = (allFilters[c.column] || '').trim();
        if (text) out.push(`${c.label || c.column}: ${text}`);
    });
    return out;
}

function renderAllHead() {
    const cols = allColumns();
    const arrow = c => c.column !== allSort.column ? '' : ` <i class="fas fa-sort-${allSort.dir > 0 ? 'up' : 'down'}"></i>`;
    $('allHead').innerHTML = '<tr>' + cols.map(c => {
        const f = customField(c);
        return `<th data-sort="${esc(c.column)}">${esc(c.label || c.column)}${f && f.required ? ' *' : ''}${arrow(c)}</th>`;
    }).join('') + '<th></th></tr><tr>' +
        cols.map(c => `<th><input class="form-control" list="allFilterValues" autocomplete="off" data-filter="${esc(c.column)}" value="${esc(allFilters[c.column] || '')}" placeholder="Filter"></th>`).join('') + '<th></th></tr>';
}

function allCell(r, c) {
    const f = customField(c);
    // Not for this car's supplier: greyed out, showing anything saved before the field was limited.
    if (f && !fieldApplies(f, r.supplier_name)) return `<td class="cb-not-used${isSumField(f) ? ' text-end' : ''}">${esc(allText(r, c))}</td>`;
    if (f) {
        const v = allValue(r, c) ?? '';
        const missing = f.required && v === '' ? 'cb-missing' : '';
        const box = f.type === 'yesno'
            ? `<select class="form-select" data-vid="${r.vehicle_id}" data-ckey="${esc(f.key)}">${yesNoOptions(v)}</select>`
            : `<input class="form-control${isSumField(f) ? ' text-end' : ''}" data-vid="${r.vehicle_id}" data-ckey="${esc(f.key)}" value="${esc(v)}">`;
        return `<td class="${missing}${f.key === 'notes' ? ' notes-wide' : ''}">${box}</td>`;
    }
    if (c.column === 'BUYER' || c.column === 'SUPPLIER_NAME') return missingCell(allText(r, c));
    const title = metricKey(c) ? ` title="${esc(metricTitle(r, c))}"` : '';
    return `<td class="${isFigure(c) ? 'text-end' : ''}"${title}>${esc(allText(r, c))}</td>`;
}

function renderFilterNote(matching) {
    const filters = activeFilters();
    const count = `${matching.toLocaleString('en-GB')} Of ${allRows.length.toLocaleString('en-GB')}`;
    $('allFilterNote').style.display = filters.length ? '' : 'none';
    $('allFilterNote').textContent = filters.length ? `Filtered, Showing ${count} Cars. ${filters.join(', ')}` : '';
    $('allExportBtn').className = `btn btn-sm fw-bold text-nowrap ${filters.length ? 'btn-warning' : 'btn-success'}`;
    $('allExportBtn').innerHTML = '<i class="fas fa-download me-1"></i>'
        + (filters.length ? `Export CSV (Filtered: ${count})` : `Export CSV (All ${allRows.length.toLocaleString('en-GB')})`);
    $('allExportBtn').title = filters.length ? 'Only the cars that match the filters, with the columns on screen' : 'Every car, with the columns on screen';
}

function renderAllBody() {
    const cols = allColumns();
    const rowsNow = allFiltered();
    const shown = rowsNow.slice(0, allLimit);
    const total = r2(rowsNow.reduce((s, r) => s + (num(r.purchase_total) || 0), 0));
    $('allBody').innerHTML = shown.length ? shown.map(r => `<tr>${cols.map(c => allCell(r, c)).join('')}
        <td class="text-nowrap"><button class="btn btn-sm btn-outline-secondary py-0" data-openbatch="${r.batch_id}" title="Open the batch"><i class="fas fa-list"></i></button>
        ${r.has_file ? `<a class="btn btn-sm btn-outline-secondary py-0" href="${BASE}/cars-bought/batches/${r.batch_id}/view" target="_blank" rel="noopener" title="Open the file"><i class="fas fa-file-invoice"></i></a>` : ''}</td></tr>`).join('')
        : `<tr><td colspan="${cols.length + 1}" class="text-muted">${allRows.length ? 'No vehicles match' : 'No purchases yet'}</td></tr>`;
    $('allCount').textContent = `${rowsNow.length} of ${allRows.length} vehicles, ${gbp(total)}`;
    renderFilterNote(rowsNow.length);
    const problems = Object.values(metricErrors);
    $('allMetricErrors').textContent = problems.join(' ');
    $('allMetricErrors').style.display = problems.length ? '' : 'none';
    $('allMoreBtn').style.display = rowsNow.length > shown.length ? '' : 'none';
}

function loadAllVehicles() {
    return fetch(BASE + '/cars-bought/vehicles').then(r => r.json()).then(res => {
        allRows = res.vehicles || [];
        allLimit = allLimit || buyerCfg.table_rows || 500;
        if (buyerCfg.table_columns) { renderAllHead(); renderAllBody(); }
    }).catch(() => { $('allBody').innerHTML = '<tr><td class="text-danger">Could not load the vehicles</td></tr>'; });
}

/* ── CSV of what the page has narrowed down to ─────────── */

// A cell starting with one of these is run as a formula by Excel.
const csvCell = v => {
    const text = String(v ?? '');
    return `"${(/^[=+\-@]/.test(text) ? "'" : '') + text.replace(/"/g, '""')}"`;
};

function exportCsv() {
    const cols = allColumns();
    // Figures as plain numbers, so they add up in Excel. Dates as on screen.
    const cell = (r, c) => isFigure(c) ? (allValue(r, c) ?? '') : csvCell(allText(r, c));
    const lines = [cols.map(c => csvCell(c.label || c.column)).join(',')]
        .concat(allFiltered().map(r => cols.map(c => cell(r, c)).join(',')));
    const day = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'}));
    link.download = `cars_bought_${day}${activeFilters().length ? '_filtered' : ''}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
}

/* ── Page events ───────────────────────────────────────── */

function refilter() {
    allLimit = buyerCfg.table_rows || 500;
    clearTimeout(allFilterTimer);
    allFilterTimer = setTimeout(renderAllBody, 150);
}

$('allHead').addEventListener('click', e => {
    const th = e.target.closest('[data-sort]');
    if (!th) return;
    allSort = {column: th.dataset.sort, dir: allSort.column === th.dataset.sort ? -allSort.dir : 1};
    renderAllHead();
    renderAllBody();
});
// Only the rows are rebuilt while typing, so the filter box keeps the cursor.
$('allHead').addEventListener('input', e => {
    if (!e.target.dataset.filter) return;
    allFilters[e.target.dataset.filter] = e.target.value;
    refilter();
});
// The filter box's dropdown: the values in that column.
$('allHead').addEventListener('focusin', e => {
    const c = allColumns().find(x => x.column === e.target.dataset.filter);
    if (!c) return;
    const values = [...new Set(allRows.map(r => allText(r, c)).filter(Boolean))].sort().slice(0, 500);
    $('allFilterValues').innerHTML = values.map(v => `<option value="${esc(v)}">`).join('');
});
$('allSearch').addEventListener('input', refilter);
$('allSearchBy').addEventListener('change', () => {
    $('allSearch').placeholder = `Search ${SEARCH_BY[$('allSearchBy').value].label}`;
    refilter();
});
['allFrom', 'allTo'].forEach(id => $(id).addEventListener('change', refilter));
$('allClearBtn').addEventListener('click', () => {
    allFilters = {};
    ['allSearch', 'allFrom', 'allTo'].forEach(id => { $(id).value = ''; });
    renderAllHead();
    renderAllBody();
});
$('allExportBtn').addEventListener('click', exportCsv);
$('allMoreBtn').addEventListener('click', () => {
    allLimit += buyerCfg.table_rows || 500;
    renderAllBody();
});
$('allBody').addEventListener('click', e => {
    const open = e.target.closest('[data-openbatch]');
    if (open) openBatch(open.dataset.openbatch);
});
// A buyer filling in a field: saved as soon as they leave the box.
$('allBody').addEventListener('change', e => {
    const el = e.target;
    if (!el.dataset.vid) return;
    const row = allRows.find(r => String(r.vehicle_id) === el.dataset.vid);
    const field = customFields.find(f => f.key === el.dataset.ckey);
    postJSON(`/cars-bought/vehicles/${el.dataset.vid}/custom`, {key: el.dataset.ckey, value: el.value}).then(res => {
        if (res.status !== 'ok') { alert(res.message); el.value = (row.custom || {})[el.dataset.ckey] ?? ''; return; }
        row.custom = {...(row.custom || {}), [el.dataset.ckey]: res.value};
        el.value = res.value ?? '';
        el.closest('td').classList.toggle('cb-missing', !!(field && field.required && el.value === ''));
    }).catch(err => { alert('Error: ' + err.message); el.value = (row.custom || {})[el.dataset.ckey] ?? ''; });
});

/* ── Invoices not yet submitted ────────────────────────── */

/* Vehicles count only files already read. Asks again while any is being read. */
function showInboxSummary(files) {
    const list = files || [];
    const cars = list.reduce((s, f) => s + (f.vehicles || 0), 0);
    $('inboxSummary').style.display = list.length ? '' : 'none';
    $('inboxSummary').textContent = `${list.length} Unprocessed Invoice${list.length === 1 ? '' : 's'} (${cars} Vehicle${cars === 1 ? '' : 's'})`;
    if (list.some(f => f.status === 'queued' || f.status === 'reading')) {
        setTimeout(() => fetch(BASE + '/cars-bought/inbox').then(r => r.json()).then(res => showInboxSummary(res.files)).catch(() => {}), 5000);
    }
}

/* ── Start ─────────────────────────────────────────────── */

fetch(BASE + '/cars-bought/config').then(r => r.json()).then(res => {
    customFields = res.custom_fields || [];
    buyerCfg = res;
    // What is known now shows first; the slower look in the SharePoint folder follows.
    fetch(BASE + '/cars-bought/inbox').then(r => r.json()).then(inbox => {
        if (!res.inbox_enabled) { showInboxSummary(inbox.files); return null; }
        $('inboxSummary').style.display = (inbox.files || []).length ? '' : 'none';
        $('inboxSummary').textContent = `${(inbox.files || []).length} Unprocessed Invoices`;
        return postJSON('/cars-bought/inbox/check', {}).then(checked => showInboxSummary(checked.files));
    }).catch(() => {});
    // Whichever of this and the vehicles arrives second draws the table.
    allLimit = allLimit || res.table_rows || 500;
    renderAllHead();
    renderAllBody();
    loadMetricValues();
});
loadAllVehicles();
