/* Apex Missing Finance page. Built from stock_list.js: the columns, filter
   buttons, hard filters and cell colour rules come from the Settings popup. */
const API = BASE_URL + '/reports/apex-missing-finance';

/* ── Formatters ──────────────────────────────────────────────────────────── */
function fmtGBP(v, dp) {
    if (v == null) return '—';
    const n = Number(v);
    return (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function fmtDate(v) {
    return v ? String(v).slice(0, 10).split('-').reverse().join('/') : '—';
}
// STATUS_PAID -> Paid, STATUS_ACKNOWLEDGED -> Acknowledged
function statusText(v) {
    if (v == null || v === '') return '—';
    return String(v).replace(/^STATUS_/, '').toLowerCase().replace(/(^|_)(\w)/g, (m, s, c) => (s ? ' ' : '') + c.toUpperCase());
}

const COLUMN_FORMATS = [
    ['text', 'Text'], ['number', 'Number'], ['gbp', '£'], ['pct', '%'],
    ['date', 'Date'], ['link', 'Link button'], ['status', 'Status (STATUS_PAID -> Paid)'],
];
const NUMBER_FORMATS = ['number', 'gbp', 'pct'];
const DEFAULT_WIDTH = 90;
const RULE_OPERATORS = [
    ['equals', 'Equals'], ['not_equals', 'Does not equal'], ['contains', 'Contains'],
    ['is_blank', 'Is blank'], ['is_not_blank', 'Is not blank'],
    ['less_than', 'Less than'], ['greater_than', 'Greater than'],
];
const NO_VALUE_OPS = ['is_blank', 'is_not_blank'];

/* ── State ───────────────────────────────────────────────────────────────── */
let allRows = [];
let colIdx = {};
let viewColumns = [];
let customFilters = [];
let cellRules = [];
let sortCol = null;
let sortAsc = true;
let shownRows = [];
let appearance = { font_size: 12, auto_width: true };
let autoWidths = {};
const activeFilters = {};
const FILTER_DROPDOWN_OVER = 15;
const BLANK_KEY = '__BLANK__';

function val(r, name) {
    const i = colIdx[name];
    return i === undefined ? null : r[i];
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

/* ── Fetch ───────────────────────────────────────────────────────────────── */
async function fetchData(force = false) {
    const btn = document.getElementById('fetchBtn');
    btn.disabled = true;
    document.body.classList.add('page-is-loading');
    document.getElementById('data-section').style.display = 'none';
    document.getElementById('table-wrap').classList.add('d-none');
    document.getElementById('table-error').classList.add('d-none');
    setStatus('running');
    try {
        const res = await fetch(API + '/data' + (force ? '?force=true' : ''));
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Unknown error');
        allRows = data.rows;
        colIdx = Object.fromEntries(data.columns.map((c, i) => [c, i]));
        viewColumns = data.view.filter(c => c.column in colIdx);
        if (!viewColumns.some(c => c.column === sortCol)) {
            sortCol = viewColumns.some(c => c.column === 'ORDER_ACKNOWLEDGED_AT') ? 'ORDER_ACKNOWLEDGED_AT' : (viewColumns[0] || {}).column || null;
            sortAsc = false;
        }
        customFilters = data.filters || [];
        cellRules = data.cell_rules || [];
        appearance = data.appearance || appearance;
        await applyAppearance();
        buildHeader();
        buildCustomFilters();
        setStatus('done');
        const sfEl = document.getElementById('sf-status');
        sfEl.title = data.cached_at ? 'Cached at ' + new Date(data.cached_at * 1000).toLocaleTimeString('en-GB') : '';
        if (data.missing && data.missing.length) sfEl.title += '\nNot in the table, so not shown: ' + data.missing.join(', ');
        let maxApex = null;
        allRows.forEach(r => {
            const v = val(r, 'UPDATED_AT');
            if (v && (!maxApex || v > maxApex)) maxApex = v;
        });
        if (maxApex) {
            const d = new Date(String(maxApex).replace(' ', 'T'));
            document.getElementById('apex-updated-val').textContent =
                d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB');
            document.getElementById('apex-updated').style.display = '';
        }
        document.body.classList.remove('page-is-loading');
        document.getElementById('data-section').style.display = '';
        document.getElementById('table-wrap').classList.remove('d-none');
        applyFilters();
        fitTableWrap();
        return true;
    } catch (e) {
        setStatus('error');
        document.body.classList.remove('page-is-loading');
        const errEl = document.getElementById('table-error');
        errEl.textContent = 'Failed to load data: ' + e.message;
        errEl.classList.remove('d-none');
        awsLoginIfExpired(errEl, e.message);
        // Live Update reads this to mark its reload step failed.
        return { ok: false, message: 'Failed to load data: ' + e.message };
    } finally {
        btn.disabled = false;
    }
}

/* ── Live Update (admin only) ────────────────────────────────────────────── */
const LIVE_UPDATE_RECIPE = 'apex_missing_finance';

// The start form and the run's steps/logs are in separate boxes, so the last
// run stays readable from the Log button after the popup closes itself.
const LU_OPTS = { reload: true, onDone: () => fetchData(true), closeModal: 'liveUpdateModal', logButton: 'lu-log-btn' };

function liveUpdateBox() {
    return document.getElementById('live-update-box');
}

function liveUpdateModal() {
    return bootstrap.Modal.getOrCreateInstance(document.getElementById('liveUpdateModal'));
}

function followLiveUpdate(runId) {
    liveUpdateFollow(BASE_URL, runId, liveUpdateBox(), LU_OPTS);
}

function openLiveUpdateLog() {
    document.getElementById('live-update-start').innerHTML = '';
    liveUpdateModal().show();
}

async function openLiveUpdate() {
    liveUpdateModal().show();
    const box = liveUpdateBox();
    const start = document.getElementById('live-update-start');
    if (box.dataset.luRun) return;   // a run is already showing
    try {
        const cfg = await apiGetJson(BASE_URL + '/api/live-update/config?recipe=' + LIVE_UPDATE_RECIPE);
        if (cfg.open_run_id) { start.innerHTML = ''; followLiveUpdate(cfg.open_run_id); return; }
        const recipe = (cfg.recipes || []).find(r => r.recipe_key === LIVE_UPDATE_RECIPE);
        if (!recipe) throw new Error('the Apex Missing Finance recipe is missing - add it in Settings > Live Update');
        box.innerHTML = '';
        start.innerHTML = `<p class="mb-2">Pulls <strong>${recipe.endpoints.join('</strong> and <strong>')}</strong> from Apex for
            the last <input type="number" id="lu-lookback" min="1" max="720" value="${recipe.lookback_hours}" style="width:60px"> hours,
            rebuilds the tables up to this report in PROD, checks each step, then reloads the table here.
            The popup closes when it has finished cleanly; Log shows it again.</p>
            <button class="btn btn-primary btn-sm" id="lu-go"><i class="fas fa-bolt me-1"></i>Start Live Update</button>`;
        document.getElementById('lu-go').onclick = async () => {
            const lookback = document.getElementById('lu-lookback').value;
            start.innerHTML = '';
            try {
                await liveUpdateStart(BASE_URL, LIVE_UPDATE_RECIPE, box, { ...LU_OPTS, lookback });
            } catch (e) {
                box.insertAdjacentHTML('beforeend', `<div class="text-danger mt-2">Could not start: ${luEsc(e.message)}</div>`);
            }
        };
    } catch (e) {
        start.innerHTML = `<div class="text-danger">Could not load Live Update: ${luEsc(e.message)}</div>`;
    }
}

/* ── Filters ─────────────────────────────────────────────────────────────── */
function filterKey(v) {
    return v == null || v === '' ? BLANK_KEY : String(v);
}
function filterValueText(f, key) {
    if (key === BLANK_KEY) return '(blank)';
    const c = viewColumns.find(col => col.column === f.column);
    return c && c.format === 'status' ? statusText(key) : key;
}

function applyFilters() {
    const search = (document.getElementById('searchInput').value || '').toLowerCase();
    const filtered = allRows.filter(r => {
        for (const f of customFilters) {
            const want = activeFilters['col:' + f.column];
            if (want && want !== 'All' && !want.includes(filterKey(val(r, f.column)))) return false;
        }
        if (search && !viewColumns.some(c => String(val(r, c.column) ?? '').toLowerCase().includes(search))) return false;
        return true;
    });
    renderTable(filtered);
}

function buildCustomFilters() {
    const holder = document.getElementById('custom-filters');
    holder.innerHTML = '';
    const esc = ColumnEditor.esc;
    customFilters.forEach(f => {
        const group = 'col:' + f.column;
        const keys = [...new Set(allRows.map(r => filterKey(val(r, f.column))))];
        const numeric = keys.every(k => k === BLANK_KEY || !isNaN(Number(k)));
        keys.sort((a, b) => {
            if (a === BLANK_KEY) return 1;
            if (b === BLANK_KEY) return -1;
            return numeric ? Number(a) - Number(b) : a.localeCompare(b);
        });
        const selected = activeFilters[group];
        if (selected === undefined || (selected !== 'All' && selected.some(k => !keys.includes(k)))) activeFilters[group] = 'All';
        const chosen = activeFilters[group];
        const title = esc(f.label || f.column) + (f.description
            ? ` <span class="info-mark" data-info="${esc(f.description)}">i</span>` : '');
        let controls;
        if (keys.length > FILTER_DROPDOWN_OVER) {
            controls = `<select class="form-select form-select-sm filter-select" data-group="${esc(group)}">
                <option value="All">All</option>
                ${keys.map(k => `<option value="${esc(k)}" ${chosen !== 'All' && chosen.includes(k) ? 'selected' : ''}>${esc(filterValueText(f, k))}</option>`).join('')}
            </select>`;
        } else {
            controls = ['All', ...keys].map(k => `<button class="btn btn-sm ${(k === 'All' && chosen === 'All') || (chosen !== 'All' && chosen.includes(k)) ? 'btn-dark' : 'btn-outline-secondary'} filter-btn"
                data-group="${esc(group)}" data-value="${esc(k)}">${esc(k === 'All' ? 'All' : filterValueText(f, k))}</button>`).join('');
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
    activeFilters[sel.dataset.group] = sel.value === 'All' ? 'All' : [sel.value];
    applyFilters();
});

document.addEventListener('click', e => {
    const btn = e.target.closest('.filter-btn');
    if (!btn) return;
    const group = btn.dataset.group, value = btn.dataset.value;
    if (value === 'All') {
        activeFilters[group] = 'All';
    } else if (e.ctrlKey || e.metaKey) {
        const selected = activeFilters[group] === 'All' ? [] : [...activeFilters[group]];
        const index = selected.indexOf(value);
        if (index < 0) selected.push(value); else selected.splice(index, 1);
        activeFilters[group] = selected.length ? selected : 'All';
    } else {
        activeFilters[group] = [value];
    }
    const chosen = activeFilters[group];
    document.querySelectorAll(`.filter-btn[data-group="${CSS.escape(group)}"]`).forEach(b => {
        const on = chosen === 'All' ? b.dataset.value === 'All' : chosen.includes(b.dataset.value);
        b.classList.toggle('btn-dark', on);
        b.classList.toggle('btn-outline-secondary', !on);
    });
    applyFilters();
});

function clearAllFilters() {
    Object.keys(activeFilters).forEach(k => { activeFilters[k] = 'All'; });
    document.getElementById('searchInput').value = '';
    buildCustomFilters();
    applyFilters();
}

/* ── Sort ────────────────────────────────────────────────────────────────── */
function sortRows(rows) {
    const c = viewColumns.find(col => col.column === sortCol);
    if (!c) return rows;
    const key = r => {
        const v = val(r, c.column);
        if (NUMBER_FORMATS.includes(c.format)) return v == null ? -Infinity : Number(v);
        return String(v ?? '').toLowerCase();
    };
    return [...rows].sort((a, b) => {
        const av = key(a), bv = key(b);
        const cmp = av < bv ? -1 : av > bv ? 1 : 0;
        return sortAsc ? cmp : -cmp;
    });
}

/* ── Font size and column widths ─────────────────────────────────────────── */
// The text a cell shows, as fillCell writes it, for measuring.
function cellText(c, v) {
    const dp = Number.isInteger(c.decimals) ? c.decimals : 0;
    switch (c.format) {
        case 'gbp':    return fmtGBP(v, dp);
        case 'number': return v == null ? '—' : Number(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
        case 'pct':    return v == null ? '—' : (Number(v) * 100).toFixed(dp) + '%';
        case 'date':   return fmtDate(v);
        case 'status': return statusText(v);
        case 'link':   return c.label || 'Open';
        default:       return v == null || v === '' ? '—' : String(v);
    }
}

let measureCtx = null;
function textWidth(text, px, weight) {
    if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
    const family = getComputedStyle(document.getElementById('amf-table')).fontFamily;
    measureCtx.font = `${weight} ${px}px ${family}`;
    return measureCtx.measureText(text).width;
}

// Narrowest width that fits the heading on the two lines the header row has.
function headerWidth(text, px) {
    const words = text.split(/\s+/).filter(Boolean);
    const full = textWidth(text, px, 600);
    let w = Math.max(...words.map(t => textWidth(t, px, 600)), full / 2);
    const space = textWidth(' ', px, 600);
    for (let tries = 0; tries < 20; tries++) {
        let lines = 1, line = 0;
        words.forEach(t => {
            const tw = textWidth(t, px, 600);
            if (line && line + space + tw > w) { lines++; line = tw; } else line += (line ? space : 0) + tw;
        });
        if (lines <= 2) return w;
        w += px;
    }
    return full;
}

// Each column as wide as its longest value or heading at the chosen size,
// plus the cell padding (6px each side), border and the sort arrow.
function computeAutoWidths() {
    const px = appearance.font_size;
    const arrow = textWidth(' ▼', px * 0.65, 400);
    autoWidths = {};
    viewColumns.forEach(c => {
        let cells;
        if (c.format === 'link') {
            cells = textWidth(cellText(c, null), px * 0.9, 400) + 14;   // button padding and border
        } else {
            const seen = new Set();
            cells = 0;
            allRows.forEach(r => {
                const t = cellText(c, val(r, c.column));
                if (seen.has(t)) return;
                seen.add(t);
                cells = Math.max(cells, textWidth(t, px, 400));
            });
        }
        const head = headerWidth(c.label || c.column, px) + (c.format === 'link' ? 0 : arrow);
        autoWidths[c.column] = Math.min(Math.ceil(Math.max(cells, head)) + 15, 600);
    });
}

async function applyAppearance() {
    const px = appearance.font_size;
    document.getElementById('amf-table').style.fontSize = px + 'px';
    if (!appearance.auto_width) return;
    // Measured in the page font, so wait for both weights to load first.
    if (document.fonts) {
        await Promise.all([document.fonts.load(`400 ${px}px 'DM Sans'`), document.fonts.load(`600 ${px}px 'DM Sans'`)])
            .catch(() => {});
    }
    computeAutoWidths();
}

function columnWidth(c) {
    return appearance.auto_width && autoWidths[c.column] ? autoWidths[c.column] : (c.width || DEFAULT_WIDTH);
}

function buildHeader() {
    const colgroup = document.getElementById('amf-cols');
    const headRow = document.getElementById('amf-head');
    colgroup.innerHTML = '';
    headRow.innerHTML = '';
    let total = 0;
    viewColumns.forEach(c => {
        const col = document.createElement('col');
        col.style.width = columnWidth(c) + 'px';
        total += columnWidth(c);
        colgroup.appendChild(col);
        const th = document.createElement('th');
        th.title = c.column;
        if (c.format === 'link') th.className = 'col-center';
        else if (['gbp', 'number', 'pct'].includes(c.format)) th.className = 'col-right';
        th.appendChild(document.createTextNode(c.label || c.column));
        if (c.format !== 'link') {
            const arrow = document.createElement('span');
            arrow.className = 'sort-arrow';
            if (c.column === sortCol) { arrow.textContent = sortAsc ? ' ▲' : ' ▼'; arrow.style.opacity = '1'; }
            th.appendChild(arrow);
            th.addEventListener('click', () => {
                if (sortCol === c.column) sortAsc = !sortAsc;
                else { sortCol = c.column; sortAsc = true; }
                buildHeader();
                applyFilters();
            });
        }
        headRow.appendChild(th);
    });
    document.getElementById('amf-table').style.width = total + 'px';
}

const TABLE_BOTTOM_GAP = 32;
function fitTableWrap() {
    const wrap = document.getElementById('table-wrap');
    if (wrap.classList.contains('d-none')) return;
    const top = wrap.getBoundingClientRect().top + window.scrollY;
    wrap.style.maxHeight = Math.max(200, window.innerHeight - top - TABLE_BOTTOM_GAP) + 'px';
}
window.addEventListener('resize', fitTableWrap);

/* ── Cell colours ────────────────────────────────────────────────────────── */
function ruleMatches(rule, v) {
    const blank = v == null || v === '';
    switch (rule.operator) {
        case 'is_blank':     return blank;
        case 'is_not_blank': return !blank;
        case 'contains':     return !blank && String(v).toLowerCase().includes(rule.value.toLowerCase());
        case 'less_than':    return !blank && Number(v) < Number(rule.value);
        case 'greater_than': return !blank && Number(v) > Number(rule.value);
        case 'not_equals':   return !sameValue(v, rule.value);
        default:             return sameValue(v, rule.value);
    }
}
// Numbers compare as numbers, so a rule value of 0 matches 0.0.
function sameValue(v, ruleValue) {
    if (v == null) return false;
    if (typeof v === 'number' && ruleValue.trim() !== '' && !isNaN(Number(ruleValue))) return v === Number(ruleValue);
    return String(v) === ruleValue;
}
function cellColour(column, v) {
    const rule = cellRules.find(r => r.column === column && ruleMatches(r, v));
    return rule ? rule.colour : null;
}

/* ── Render ──────────────────────────────────────────────────────────────── */
function fillCell(td, c, v) {
    const dp = Number.isInteger(c.decimals) ? c.decimals : 0;
    switch (c.format) {
        case 'gbp':
            td.className = 'col-right';
            td.textContent = fmtGBP(v, dp);
            return;
        case 'number':
            td.className = 'col-right';
            td.textContent = v == null ? '—' : Number(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
            return;
        case 'pct':
            td.className = 'col-right';
            td.textContent = v == null ? '—' : (Number(v) * 100).toFixed(dp) + '%';
            return;
        case 'date':
            td.textContent = fmtDate(v);
            return;
        case 'status':
            td.textContent = statusText(v);
            return;
        case 'link':
            td.className = 'col-center';
            if (v && /^https?:\/\//i.test(String(v))) {
                const a = document.createElement('a');
                a.href = v; a.target = '_blank'; a.rel = 'noopener';
                a.textContent = c.label || 'Open'; a.className = 'apex-link';
                td.appendChild(a);
            }
            return;
        default:
            td.textContent = v == null || v === '' ? '—' : String(v);
    }
}

function renderTable(rows) {
    shownRows = sortRows(rows);
    const tbody = document.getElementById('amf-tbody');
    tbody.innerHTML = '';
    shownRows.forEach(r => {
        const tr = document.createElement('tr');
        viewColumns.forEach(c => {
            const td = document.createElement('td');
            const v = val(r, c.column);
            fillCell(td, c, v);
            const colour = cellColour(c.column, v);
            if (colour) td.style.background = colour;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    document.getElementById('row-count').textContent = shownRows.length.toLocaleString('en-GB') + ' rows';
}

/* ── CSV export ──────────────────────────────────────────────────────────── */
function csvEscape(v) {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function csvValue(c, v) {
    if (c.format === 'status') return statusText(v);
    if (c.format === 'date' && v) return String(v).slice(0, 10);
    return v;
}
function downloadCsv() {
    const header = viewColumns.map(c => csvEscape(c.label || c.column)).join(',');
    const lines = shownRows.map(r => viewColumns.map(c => csvEscape(csvValue(c, val(r, c.column)))).join(','));
    const blob = new Blob(['﻿' + [header, ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `apex_missing_finance_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

/* ── Settings (admin only) ───────────────────────────────────────────────── */
let columnEditor = null;
let liveColumns = [];
let filterDraft = [];
let hardFilterDraft = [];
let cellRuleDraft = [];

function columnOptions(selected) {
    const esc = ColumnEditor.esc;
    const names = liveColumns.map(c => c.column);
    const cols = (selected && !names.includes(selected) ? [selected] : []).concat(names);
    return `<option value="">(choose a column)</option>` + cols.map(c =>
        `<option value="${esc(c)}" ${c === selected ? 'selected' : ''}>${esc(c)}${names.includes(c) ? '' : ' (not in the table)'}</option>`).join('');
}

function renderFilterDraft() {
    const esc = ColumnEditor.esc;
    const last = filterDraft.length - 1;
    document.getElementById('filterEditor').innerHTML = filterDraft.map((f, i) => `
        <div class="filter-edit-row">
            <select onchange="filterDraft[${i}].column = this.value">${columnOptions(f.column)}</select>
            <input type="text" class="f-label" maxlength="60" value="${esc(f.label || '')}" placeholder="Title (empty: column name)"
                   oninput="filterDraft[${i}].label = this.value">
            <input type="text" class="f-desc" maxlength="300" value="${esc(f.description || '')}" placeholder="Description (shown on the i)"
                   oninput="filterDraft[${i}].description = this.value">
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Move up" onclick="moveDraft(filterDraft, ${i}, -1, renderFilterDraft)" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Move down" onclick="moveDraft(filterDraft, ${i}, 1, renderFilterDraft)" ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
            <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" onclick="filterDraft.splice(${i}, 1); renderFilterDraft()"><i class="fas fa-times"></i></button>
        </div>`).join('') || '<div class="text-muted small">No filters added yet.</div>';
}
function addFilterDraft() { filterDraft.push({ column: '', label: '', description: '' }); renderFilterDraft(); }

function operatorOptions(ops, selected) {
    return ops.map(([op, label]) => `<option value="${op}" ${op === selected ? 'selected' : ''}>${label}</option>`).join('');
}

function renderHardFilterDraft() {
    const esc = ColumnEditor.esc;
    document.getElementById('hardFilterEditor').innerHTML = hardFilterDraft.map((f, i) => `
        <div class="filter-edit-row">
            <select onchange="hardFilterDraft[${i}].column = this.value">${columnOptions(f.column)}</select>
            <select onchange="hardFilterDraft[${i}].operator = this.value; renderHardFilterDraft()">${operatorOptions(RULE_OPERATORS, f.operator)}</select>
            <input type="text" class="f-desc" maxlength="200" value="${esc(f.value || '')}" placeholder="Value"
                   oninput="hardFilterDraft[${i}].value = this.value" ${NO_VALUE_OPS.includes(f.operator) ? 'disabled' : ''}>
            <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" onclick="hardFilterDraft.splice(${i}, 1); renderHardFilterDraft()"><i class="fas fa-times"></i></button>
        </div>`).join('') || '<div class="text-muted small">No hard filters added yet.</div>';
}
function addHardFilterDraft() { hardFilterDraft.push({ column: '', operator: 'equals', value: '' }); renderHardFilterDraft(); }

function renderCellRuleDraft() {
    const esc = ColumnEditor.esc;
    const last = cellRuleDraft.length - 1;
    document.getElementById('cellRuleEditor').innerHTML = cellRuleDraft.map((r, i) => `
        <div class="filter-edit-row">
            <select onchange="cellRuleDraft[${i}].column = this.value">${columnOptions(r.column)}</select>
            <select onchange="cellRuleDraft[${i}].operator = this.value; renderCellRuleDraft()">${operatorOptions(RULE_OPERATORS, r.operator)}</select>
            <input type="text" class="f-desc" maxlength="200" value="${esc(r.value || '')}" placeholder="Value"
                   oninput="cellRuleDraft[${i}].value = this.value" ${NO_VALUE_OPS.includes(r.operator) ? 'disabled' : ''}>
            <input type="color" value="${esc(r.colour)}" title="Cell colour" oninput="cellRuleDraft[${i}].colour = this.value">
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Move up" onclick="moveDraft(cellRuleDraft, ${i}, -1, renderCellRuleDraft)" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Move down" onclick="moveDraft(cellRuleDraft, ${i}, 1, renderCellRuleDraft)" ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
            <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" onclick="cellRuleDraft.splice(${i}, 1); renderCellRuleDraft()"><i class="fas fa-times"></i></button>
        </div>`).join('') || '<div class="text-muted small">No colour rules added yet.</div>';
}
function addCellRuleDraft() { cellRuleDraft.push({ column: '', operator: 'equals', value: '', colour: '#fff3b0' }); renderCellRuleDraft(); }

function moveDraft(list, i, step, render) {
    const j = i + step;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    render();
}

function settingsError(message) {
    const el = document.getElementById('settings-error');
    el.textContent = message || '';
    el.classList.toggle('d-none', !message);
}

async function openSettings() {
    settingsError('');
    columnEditor = null;
    document.getElementById('settings-loading').classList.remove('d-none');
    document.getElementById('settings-content').classList.add('d-none');
    document.getElementById('settingsSaveBtn').disabled = true;
    document.getElementById('colEditor').innerHTML = '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('settingsModal')).show();
    try {
        const resp = await fetch(API + '/columns').then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Could not read the columns');
        // ColumnEditor measures widths while rendering, so show its container first.
        document.getElementById('settings-content').classList.remove('d-none');
        columnEditor = new ColumnEditor(document.getElementById('colEditor'), {
            live: resp.columns,
            shown: resp.view.map(c => ({ ...c, width: c.width || DEFAULT_WIDTH })),
            formats: COLUMN_FORMATS,
            numberFormats: NUMBER_FORMATS,
            newColumn: (column, type) => ({
                column, label: '', decimals: 0, width: DEFAULT_WIDTH,
                format: ['NUMBER', 'FLOAT'].includes(type) ? 'number'
                      : ['DATE', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ'].includes(type) ? 'date' : 'text',
            }),
        });
        liveColumns = resp.columns;
        const ap = resp.appearance || appearance;
        document.getElementById('amfFontSize').value = ap.font_size;
        document.getElementById('amfAutoWidth').checked = ap.auto_width;
        filterDraft = (resp.filters || []).map(f => ({ ...f }));
        hardFilterDraft = (resp.hard_filters || []).map(f => ({ ...f }));
        cellRuleDraft = (resp.cell_rules || []).map(r => ({ ...r }));
        renderFilterDraft();
        renderHardFilterDraft();
        renderCellRuleDraft();
        document.getElementById('settingsSaveBtn').disabled = false;
    } catch (e) {
        document.getElementById('settings-content').classList.add('d-none');
        settingsError(e.message);
    } finally {
        document.getElementById('settings-loading').classList.add('d-none');
    }
}

async function saveSettings() {
    if (!columnEditor) return;
    const btn = document.getElementById('settingsSaveBtn');
    btn.disabled = true;
    settingsError('');
    try {
        const body = {
            columns: columnEditor.columns(),
            filters: filterDraft.map(f => ({ column: f.column, label: (f.label || '').trim(), description: (f.description || '').trim() })),
            hard_filters: hardFilterDraft.map(f => ({ column: f.column, operator: f.operator,
                value: NO_VALUE_OPS.includes(f.operator) ? '' : (f.value || '').trim() })),
            cell_rules: cellRuleDraft.map(r => ({ column: r.column, operator: r.operator,
                value: NO_VALUE_OPS.includes(r.operator) ? '' : (r.value || '').trim(), colour: r.colour })),
            appearance: { font_size: parseInt(document.getElementById('amfFontSize').value, 10) || 12,
                          auto_width: document.getElementById('amfAutoWidth').checked },
        };
        const resp = await fetch(API + '/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
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
