/* DSE (Daily Spend & Enquiries) page: one row per metric, a column per day of
   the month, then the month and comparison columns. Rows and appearance are
   set in the Settings popup (admin only) and stored in kpi_layout. */
const API = BASE_URL + '/dse';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DATE_TYPES = ['DATE', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ', 'TIMESTAMP'];
const NUMBER_TYPES = ['NUMBER', 'FLOAT', 'DECIMAL', 'NUMERIC', 'INT', 'INTEGER', 'BIGINT', 'DOUBLE', 'REAL'];
const SETTING_NUMBERS = ['default_days', 'font_size', 'row_height', 'label_width', 'day_width', 'summary_width'];
const SETTING_COLOURS = ['weekend_bg', 'section_bg', 'section_text', 'summary_bg'];

let pageData = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 2026-10-23 -> "23 Oct" (with the year as "23 Oct 26" when asked)
function dateLabel(iso, withYear) {
    const [y, m, d] = iso.split('-').map(Number);
    return d + ' ' + MONTHS[m - 1] + (withYear ? ' ' + String(y).slice(2) : '');
}

function resetRange() {
    document.getElementById('startInput').value = '';
    document.getElementById('endInput').value = '';
    fetchData(false);
}

function setStatus(state) {
    const label = '<i class="fas fa-snowflake me-1 text-info"></i>Snowflake';
    const map = {
        running: `${label} <span class="spinner-border spinner-border-sm ms-1" style="width:.7rem;height:.7rem"></span>`,
        done:    `${label} <i class="fas fa-check-circle text-success ms-1"></i>`,
        error:   `${label} <i class="fas fa-times-circle text-danger ms-1"></i>`,
    };
    document.getElementById('sf-status').innerHTML = map[state] || label;
}

/* ── Formatting ──────────────────────────────────────────────────────────── */
function fmtValue(v, row) {
    if (v === null || v === undefined) return '';
    const d = row.decimals || 0;
    const n = row.fmt === 'pct100' ? Number(v) * 100 : Number(v);
    const text = Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: d, maximumFractionDigits: d });
    const sign = n < 0 ? '-' : '';
    if (row.fmt === 'gbp') return sign + '£' + text;
    if (row.fmt === 'pct' || row.fmt === 'pct100') return sign + text + '%';
    return sign + text;
}

function fmtCompare(v) {
    if (v === null || v === undefined) return '';
    return (v > 0 ? '+' : '') + v.toLocaleString('en-GB', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + '%';
}

/* ── Fetch ───────────────────────────────────────────────────────────────── */
async function fetchData(force = false) {
    const btn = document.getElementById('fetchBtn');
    btn.disabled = true;
    document.body.classList.add('page-is-loading');
    document.getElementById('table-error').classList.add('d-none');
    setStatus('running');
    try {
        const start = document.getElementById('startInput').value;
        const end = document.getElementById('endInput').value;
        const params = new URLSearchParams();
        if (start) params.set('start', start);
        if (end) params.set('end', end);
        if (force) params.set('force', 'true');
        const res = await fetch(API + '/data?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Unknown error');
        pageData = data;
        document.getElementById('startInput').value = data.start;
        document.getElementById('endInput').value = data.end;
        document.getElementById('sf-status').title = data.cached_at
            ? 'Read at ' + new Date(data.cached_at * 1000).toLocaleTimeString('en-GB') : '';
        render();
        setStatus('done');
    } catch (e) {
        setStatus('error');
        const errEl = document.getElementById('table-error');
        errEl.textContent = 'Failed to load data: ' + e.message;
        errEl.classList.remove('d-none');
    } finally {
        document.body.classList.remove('page-is-loading');
        btn.disabled = false;
    }
}

/* ── Table ───────────────────────────────────────────────────────────────── */
function applyAppearance(table, s) {
    table.style.setProperty('--dse-font', s.font_size + 'px');
    table.style.setProperty('--dse-row-h', s.row_height + 'px');
    table.style.setProperty('--dse-label-w', s.label_width + 'px');
    table.style.setProperty('--dse-day-w', s.day_width + 'px');
    table.style.setProperty('--dse-sum-w', s.summary_width + 'px');
    table.style.setProperty('--dse-weekend', s.weekend_bg);
    table.style.setProperty('--dse-section-bg', s.section_bg);
    table.style.setProperty('--dse-section-text', s.section_text);
    table.style.setProperty('--dse-summary-bg', s.summary_bg);
}

function summaryColumns(d) {
    return [
        ['mtd', 'Range Total'],
        ['mtd_avg', 'Range Daily Avg'],
        ['prev_mtd', 'Previous Period Total'],
        ['prev_mtd_avg', 'Previous Period Daily Avg'],
        ['ly_mtd', 'Same Days Last Year Total'],
        ['ly_compare', 'Vs Last Year'],
    ];
}

function render() {
    const d = pageData;
    const table = document.getElementById('dse-table');
    applyAppearance(table, d.settings);
    const empty = !d.rows.length;
    document.getElementById('empty-note').classList.toggle('d-none', !empty);
    document.getElementById('table-wrap').classList.toggle('d-none', empty);
    if (empty) return;

    const weekend = d.days.map(iso => [0, 6].includes(new Date(iso + 'T00:00:00').getDay()));
    const sums = summaryColumns(d);
    let head = '<tr><th class="dse-label">' + esc(dateLabel(d.start, true) + ' - ' + dateLabel(d.end, true)) + '</th>';
    d.days.forEach((iso, i) => {
        const day = new Date(iso + 'T00:00:00');
        head += `<th class="dse-day${weekend[i] ? ' dse-weekend' : ''}" title="${esc(dateLabel(iso, true))}">${esc(dateLabel(iso, false))}`
              + `<span class="dse-weekday">${WEEKDAYS[day.getDay()]}</span></th>`;
    });
    sums.forEach(([, label], i) => { head += `<th class="dse-sum${i === 0 ? ' dse-sum-first' : ''}">${esc(label)}</th>`; });
    document.getElementById('dse-head').innerHTML = head + '</tr>';

    const values = Object.fromEntries(d.values.map(v => [v.id, v]));
    const colCount = 1 + d.days.length + sums.length;
    let body = '';
    let lastSection = null;
    d.rows.forEach(row => {
        const section = row.section || '';
        if (section && section !== lastSection) {
            body += `<tr class="dse-section"><td class="dse-label">${esc(section)}</td>`
                  + `<td colspan="${colCount - 1}"></td></tr>`;
        }
        lastSection = section;
        const v = values[row.id] || {};
        body += `<tr><td class="dse-label" title="${esc(sourceText(row))}">${esc(row.label)}</td>`;
        if (v.error || !v.days) {
            body += `<td class="dse-row-error" colspan="${colCount - 1}">${esc(v.error || 'No data')}</td></tr>`;
            return;
        }
        const shade = row.shade && row.shade !== 'none' ? shadeScale(v.days, x => x) : null;
        v.days.forEach((x, i) => {
            const bg = shade ? shade(x, row.shade) : '';
            body += `<td class="dse-day${weekend[i] ? ' dse-weekend' : ''}${x < 0 ? ' dse-neg' : ''}"`
                  + `${bg ? ` style="background:${bg}"` : ''}>${esc(fmtValue(x, row))}</td>`;
        });
        sums.forEach(([key], i) => {
            const x = v[key];
            const text = key === 'ly_compare' ? fmtCompare(x) : fmtValue(x, row);
            body += `<td class="dse-sum${i === 0 ? ' dse-sum-first' : ''}${x < 0 ? ' dse-neg' : ''}">${esc(text)}</td>`;
        });
        body += '</tr>';
    });
    document.getElementById('dse-body').innerHTML = body;
}

function sourceText(row) {
    if (row.shape === 'long') {
        return `${row.table}: ${row.split_column} = '${row.metric_name}', ${row.value_column} by ${row.date_column}`;
    }
    return `${row.table}.${row.metric_column} by ${row.date_column}`;
}

/* ── Settings (admin only) ───────────────────────────────────────────────── */
let draftRows = [];
let editingIndex = null;   // null = adding
let tablesList = null;
const columnsCache = {};

function settingsError(msg) {
    const el = document.getElementById('settings-error');
    el.textContent = msg || '';
    el.classList.toggle('d-none', !msg);
}

function openSettings() {
    if (!pageData) return;
    draftRows = pageData.rows.map(r => ({ ...r }));
    settingsError('');
    closeRowEditor();
    renderRowList();
    const s = pageData.settings;
    document.getElementById('st-compare_mode').value = s.compare_mode;
    SETTING_NUMBERS.concat(SETTING_COLOURS).forEach(k => { document.getElementById('st-' + k).value = s[k]; });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('settingsModal')).show();
}

function renderRowList() {
    const holder = document.getElementById('rowList');
    if (!draftRows.length) {
        holder.innerHTML = '<div class="text-muted small">No rows yet.</div>';
        return;
    }
    holder.innerHTML = draftRows.map((r, i) => `
        <div class="dse-row-item">
            <span class="ri-label">${esc(r.label)}</span>
            <span class="ri-section">${esc(r.section || '')}</span>
            <span class="ri-source">${esc(sourceText(r))}</span>
            <button class="btn btn-outline-secondary" onclick="moveRow(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Move Up"><i class="fas fa-arrow-up"></i></button>
            <button class="btn btn-outline-secondary" onclick="moveRow(${i}, 1)" ${i === draftRows.length - 1 ? 'disabled' : ''} title="Move Down"><i class="fas fa-arrow-down"></i></button>
            <button class="btn btn-outline-primary" onclick="editRow(${i})">Edit</button>
            <button class="btn btn-outline-danger" onclick="deleteRow(${i})">Delete</button>
        </div>`).join('');
    const sections = [...new Set(draftRows.map(r => r.section).filter(Boolean))];
    document.getElementById('re-sections').innerHTML = sections.map(s => `<option value="${esc(s)}">`).join('');
}

function moveRow(i, step) {
    const j = i + step;
    if (j < 0 || j >= draftRows.length) return;
    [draftRows[i], draftRows[j]] = [draftRows[j], draftRows[i]];
    renderRowList();
}

function deleteRow(i) {
    if (!confirm(`Delete the row "${draftRows[i].label}"?`)) return;
    draftRows.splice(i, 1);
    if (editingIndex === i) closeRowEditor();
    renderRowList();
}

function rowEditorError(msg) {
    const el = document.getElementById('row-editor-error');
    el.textContent = msg || '';
    el.classList.toggle('d-none', !msg);
}

function closeRowEditor() {
    editingIndex = null;
    document.getElementById('rowEditor').classList.add('d-none');
    document.getElementById('addRowBtn').classList.remove('d-none');
}

function fillSelect(id, items, selected, blank) {
    const sel = document.getElementById(id);
    const opts = (blank ? [`<option value="">${esc(blank)}</option>`] : [])
        .concat(items.map(v => `<option value="${esc(v)}"${v === selected ? ' selected' : ''}>${esc(v)}</option>`));
    sel.innerHTML = opts.join('');
    if (selected && !items.includes(selected)) sel.value = '';
}

async function apiGet(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'Unknown error');
    return data;
}

async function loadTables() {
    if (tablesList) return tablesList;
    tablesList = (await apiGet(API + '/tables')).tables;
    return tablesList;
}

async function loadColumns(table) {
    if (!table) return [];
    if (!columnsCache[table]) {
        columnsCache[table] = (await apiGet(API + '/columns?table=' + encodeURIComponent(table))).columns;
    }
    return columnsCache[table];
}

function showShapeFields() {
    const long = document.getElementById('re-shape').value === 'long';
    document.querySelectorAll('#rowEditor .re-wide').forEach(el => el.classList.toggle('d-none', long));
    document.querySelectorAll('#rowEditor .re-long').forEach(el => el.classList.toggle('d-none', !long));
}

// Fills the column dropdowns from the chosen table, keeping `row`'s choices.
async function fillColumnFields(row) {
    const table = document.getElementById('re-table').value;
    rowEditorError('');
    let cols = [];
    try {
        cols = await loadColumns(table);
    } catch (e) {
        rowEditorError('Could not read the columns: ' + e.message);
    }
    const dates = cols.filter(c => DATE_TYPES.includes(c.type)).map(c => c.column);
    const numbers = cols.filter(c => NUMBER_TYPES.includes(c.type)).map(c => c.column);
    const all = cols.map(c => c.column);
    fillSelect('re-date', dates, row.date_column || (dates.length === 1 ? dates[0] : ''), 'Choose...');
    fillSelect('re-metric', numbers, row.metric_column, 'Choose...');
    fillSelect('re-split', all, row.split_column, 'Choose...');
    fillSelect('re-value', numbers, row.value_column, 'Choose...');
    await fillNames(row.metric_name);
}

async function fillNames(selected) {
    const table = document.getElementById('re-table').value;
    const split = document.getElementById('re-split').value;
    if (document.getElementById('re-shape').value !== 'long' || !table || !split) {
        fillSelect('re-name', selected ? [selected] : [], selected, 'Choose...');
        return;
    }
    try {
        const names = (await apiGet(API + '/names?table=' + encodeURIComponent(table)
                                    + '&split_column=' + encodeURIComponent(split))).names;
        fillSelect('re-name', names, selected, 'Choose...');
    } catch (e) {
        rowEditorError('Could not read the names: ' + e.message);
    }
}

async function editRow(i) {
    editingIndex = i;
    const row = i === null ? { shape: 'wide', agg: 'sum', fmt: 'num', decimals: 0, shade: 'none' } : draftRows[i];
    document.getElementById('rowEditorTitle').textContent = i === null ? 'Add Row' : 'Edit Row';
    document.getElementById('rowEditor').classList.remove('d-none');
    document.getElementById('addRowBtn').classList.add('d-none');
    rowEditorError('');
    document.getElementById('re-label').value = row.label || '';
    document.getElementById('re-section').value = row.section || (i === null && draftRows.length ? draftRows[draftRows.length - 1].section || '' : '');
    document.getElementById('re-shape').value = row.shape;
    document.getElementById('re-agg').value = row.agg;
    document.getElementById('re-fmt').value = row.fmt;
    document.getElementById('re-decimals').value = row.decimals;
    document.getElementById('re-shade').value = row.shade || 'none';
    showShapeFields();
    try {
        fillSelect('re-table', await loadTables(), row.table, 'Choose...');
    } catch (e) {
        rowEditorError('Could not read the tables: ' + e.message);
        return;
    }
    await fillColumnFields(row);
    document.getElementById('rowEditor').scrollIntoView({ block: 'nearest' });
}

function applyRowEdit() {
    const val = id => document.getElementById(id).value;
    const shape = val('re-shape');
    const row = {
        id: editingIndex === null ? 'r_' + Math.random().toString(36).slice(2, 12) : draftRows[editingIndex].id,
        label: val('re-label').trim(),
        section: val('re-section').trim(),
        table: val('re-table'),
        shape,
        date_column: val('re-date'),
        agg: val('re-agg'),
        fmt: val('re-fmt'),
        decimals: parseInt(val('re-decimals'), 10) || 0,
        shade: val('re-shade'),
    };
    if (shape === 'wide') {
        row.metric_column = val('re-metric');
    } else {
        row.split_column = val('re-split');
        row.metric_name = val('re-name');
        row.value_column = val('re-value');
    }
    const missing = [['label', 'a label'], ['table', 'a table'], ['date_column', 'the date column']]
        .concat(shape === 'wide' ? [['metric_column', 'the column']]
                                 : [['split_column', 'the name column'], ['metric_name', 'the name'], ['value_column', 'the value column']])
        .find(([k]) => !row[k]);
    if (missing) { rowEditorError('Choose ' + missing[1]); return; }
    if (row.decimals < 0 || row.decimals > 4) { rowEditorError('Decimals must be 0 to 4'); return; }
    if (editingIndex === null) draftRows.push(row); else draftRows[editingIndex] = row;
    closeRowEditor();
    renderRowList();
}

async function saveSettings() {
    if (!document.getElementById('rowEditor').classList.contains('d-none')) {
        settingsError('Apply or cancel the row being edited first');
        return;
    }
    const settings = { compare_mode: document.getElementById('st-compare_mode').value };
    SETTING_NUMBERS.forEach(k => { settings[k] = parseInt(document.getElementById('st-' + k).value, 10); });
    SETTING_COLOURS.forEach(k => { settings[k] = document.getElementById('st-' + k).value; });
    const btn = document.getElementById('settingsSaveBtn');
    btn.disabled = true;
    settingsError('');
    try {
        const res = await fetch(API + '/layout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rows: draftRows, settings }),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Save failed');
        bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
        await fetchData(true);
    } catch (e) {
        settingsError(e.message);
    } finally {
        btn.disabled = false;
    }
}

/* ── Start ───────────────────────────────────────────────────────────────── */
['startInput', 'endInput'].forEach(id => document.getElementById(id).addEventListener('change', () => {
    const start = document.getElementById('startInput').value, end = document.getElementById('endInput').value;
    if (start && end) fetchData(false);
}));
if (window.IS_ADMIN) {
    document.getElementById('re-table').addEventListener('change', () => fillColumnFields({}));
    document.getElementById('re-shape').addEventListener('change', () => { showShapeFields(); fillNames(''); });
    document.getElementById('re-split').addEventListener('change', () => fillNames(''));
}
fetchData(false);
