/* Trade Report: the View Vehicles popup and Settings > View Vehicles.
 *
 * Copied from the KPI page's vehicle list (kpi.js) so the two work alike: sort
 * by a heading, quick filters, a total / average row, CSV, and the Shown / Not
 * Shown column picker. The differences: the rows are already in the browser,
 * so nothing is fetched, and the columns come from the one trade_sales table.
 * KPI's grey-out rules are left out because trade sales have no cancelled deals.
 *
 * Loads after charts.js (escHtml) and before trade_report.js, which calls
 * TV.open(title, rows) from the View buttons.
 */
window.TV = (function () {
    'use strict';

    const BASE_URL = window.BASE_URL || '';
    const IS_ADMIN = !!window.TRADE_IS_ADMIN;

    // Every column the server sends: the heading and format it starts with.
    // inList = in the list until Settings has been saved.
    const COLUMNS = [
        { column: 'vrm', label: 'VRM', format: 'text', inList: true },
        { column: 'stock_id', label: 'Stock ID', format: 'text', inList: true },
        { column: 'sales_channel', label: 'Sales Channel', format: 'text' },
        { column: 'supplier', label: 'Supplier', format: 'text', inList: true },
        { column: 'supplier_category', label: 'Category', format: 'text', inList: true },
        { column: 'days_in_stock', label: 'DIS', format: 'number', inList: true },
        { column: 'branch_id', label: 'Site ID', format: 'text' },
        { column: 'branch_name', label: 'Site', format: 'text', inList: true },
        { column: 'branch_category', label: 'Site Category', format: 'text', inList: true },
        { column: 'auction', label: 'Customer', format: 'text', inList: true },
        { column: 'make', label: 'Make', format: 'text', inList: true },
        { column: 'model', label: 'Model', format: 'text', inList: true },
        { column: 'mileage', label: 'Mileage', format: 'number', inList: true },
        { column: 'financial_year', label: 'Financial Year', format: 'text' },
        { column: 'financial_period', label: 'Financial Period', format: 'text' },
        { column: 'month', label: 'Month', format: 'month', inList: true },
        { column: 'invoice_date', label: 'Invoice Date', format: 'date', inList: true },
        { column: 'vat_status', label: 'VAT Status', format: 'text', inList: true },
        { column: 'purchase_price_ex_vat', label: 'Purch Ex VAT', format: 'gbp', inList: true },
        { column: 'purchase_price_inc_vat', label: 'Purch Inc VAT', format: 'gbp', inList: true },
        { column: 'net_sale_amount', label: 'Sale Ex VAT', format: 'gbp', inList: true },
        { column: 'sale_vat_amount', label: 'VAT', format: 'gbp', inList: true },
        { column: 'gross_sale_amount', label: 'Sale Inc VAT', format: 'gbp', inList: true },
        { column: 'gross_margin', label: 'Gross Margin', format: 'gbp', inList: true },
        { column: 'siv', label: 'SIV', format: 'gbp', inList: true },
        { column: 'recon_cost', label: 'Recon', format: 'gbp', inList: true },
        { column: 'total_costs', label: 'Total Costs', format: 'gbp', inList: true },
        { column: 'net_margin', label: 'Net Margin', format: 'gbp', inList: true },
        { column: 'cap_id', label: 'CAP ID', format: 'text', inList: true },
        { column: 'cap_clean', label: 'CAP Clean', format: 'gbp', inList: true },
        { column: 'purchase_cap_pct', label: 'Purch CAP %', format: 'pct', inList: true },
        { column: 'sale_cap_pct', label: 'Sell CAP %', format: 'pct', inList: true },
    ];
    const INFO = Object.fromEntries(COLUMNS.map(c => [c.column, c]));

    const FORMATS = [['number', 'Number'], ['gbp', '£'], ['pct', '%'], ['text', 'Text'], ['date', 'Date'], ['month', 'Month']];
    // Formats that hold a number: right-aligned, take decimals, can have a total.
    const NUMBER_FORMATS = ['number', 'gbp', 'pct'];
    const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // A saved column, with what it has not got filled in from its starting values.
    function columnSetting(c) {
        const info = INFO[c.column];
        const format = c.format || info.format;
        const decimals = Number.isInteger(c.decimals) ? c.decimals : (format === 'pct' ? 1 : 0);
        return { column: c.column, label: c.label || '', format, decimals, summary: c.summary || 'none' };
    }

    function defaultView() {
        return { columns: COLUMNS.filter(c => c.inList).map(c => columnSetting({ column: c.column })), filters: [] };
    }

    let view = window.TRADE_VEHICLE_VIEW || defaultView();
    let highlightRules = window.TRADE_HIGHLIGHT_RULES || [];
    let state = null;   // the open list: { title, rows, sort, filterVals }

    const heading = c => c.label || INFO[c.column].label;
    const blank = v => v === null || v === undefined || v === '';

    function withCommas(n, decimals) {
        return n.toLocaleString('en-GB', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }

    // { html, num } for one cell. A value that does not suit the chosen format
    // (e.g. text in a Number column) shows as it is.
    function cellFor(value, c) {
        if (blank(value)) return { html: '' };
        if (typeof value === 'number' && c.format === 'number') return { html: escHtml(withCommas(value, c.decimals)), num: true };
        // Minus before the £ sign: -£1,234.
        if (typeof value === 'number' && c.format === 'gbp') {
            return { html: escHtml((value < 0 ? '-£' : '£') + withCommas(Math.abs(value), c.decimals)), num: true };
        }
        if (typeof value === 'number' && c.format === 'pct') return { html: escHtml(withCommas(value * 100, c.decimals) + '%'), num: true };
        const text = String(value);
        const iso = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (iso && c.format === 'date') return { html: `${iso[3]}/${iso[2]}/${iso[1]}` };
        if (iso && c.format === 'month') return { html: `${MONTHS_SHORT[Number(iso[2]) - 1]} ${iso[1]}` };
        return { html: escHtml(text) };
    }

    // ── The list ─────────────────────────────────────────────────────────────
    function open(title, rows) {
        state = { title, rows, sort: null, filterVals: {} };
        document.getElementById('vehiclesTitle').textContent = title;
        document.getElementById('vehiclesColumnsBtn').style.display = IS_ADMIN ? '' : 'none';
        document.getElementById('vehiclesStatus').textContent =
            `${rows.length.toLocaleString('en-GB')} vehicle${rows.length === 1 ? '' : 's'}`;
        renderQuickFilters();
        renderTable();
        document.getElementById('vehiclesModal').classList.add('open');
    }

    function close() {
        state = null;
        document.getElementById('vehiclesModal').classList.remove('open');
    }

    // Click a heading to sort by it; click again to reverse. Blanks go last.
    function sort(column) {
        if (!state) return;
        const cur = state.sort;
        state.sort = { column, dir: cur && cur.column === column ? -cur.dir : 1 };
        renderTable();
    }

    // ── Quick filters above the list (set in Settings > View Vehicles) ──
    // A text column gets a tick-list of the values in the list; a number or date
    // column gets from / to boxes. They only narrow the rows already in the list,
    // so the totals row and the CSV follow them.
    function quickFilterKind(f) {
        const format = INFO[f.column].format;
        if (NUMBER_FORMATS.includes(format)) return 'num';
        if (format === 'date' || format === 'month') return 'date';
        return 'select';
    }

    function renderQuickFilters() {
        const bar = document.getElementById('vehiclesQuick');
        const filters = view.filters || [];
        if (!filters.length) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
        bar.innerHTML = filters.map((f, n) => {
            const label = escHtml(f.label || INFO[f.column].label);
            const kind = quickFilterKind(f);
            if (kind === 'select') {
                const values = [...new Set(state.rows.map(r => r[f.column]).filter(v => !blank(v)).map(String))]
                    .sort((a, b) => a.localeCompare(b, 'en-GB', { numeric: true }));
                return `<div class="veh-multi-field">${label}
                    <details class="veh-multi"><summary id="vehMultiSum${n}">All</summary>
                    <div class="veh-multi-list">
                    <a href="#" onclick="TV.clearQuickSelect(${n}); return false;">Clear</a>
                    ${values.map(v => `<label><input type="checkbox" value="${escHtml(v)}"
                        onchange="TV.toggleQuickSelect(${n}, this)"> ${escHtml(v)}</label>`).join('')}
                    </div></details></div>`;
            }
            const t = kind === 'num' ? 'number' : 'date';
            const step = kind === 'num' ? ' step="any"' : '';
            return `<label>${label}<span class="veh-quick-range">
                <input type="${t}"${step} title="From" oninput="TV.setQuickFilter(${n}, 'min', this.value)">
                <input type="${t}"${step} title="To" oninput="TV.setQuickFilter(${n}, 'max', this.value)"></span></label>`;
        }).join('') + '<span class="veh-quick-count" id="vehQuickCount"></span>';
        bar.classList.add('show');
    }

    // Tick-list filter: selected = the ticked values; none ticked means all.
    function toggleQuickSelect(n, box) {
        const column = view.filters[n].column;
        const sel = new Set((state.filterVals[column] || {}).sel || []);
        if (box.checked) sel.add(box.value); else sel.delete(box.value);
        state.filterVals[column] = { sel: [...sel] };
        showQuickSelectSummary(n);
        renderTable();
    }

    function clearQuickSelect(n) {
        state.filterVals[view.filters[n].column] = { sel: [] };
        document.getElementById(`vehMultiSum${n}`).parentElement
            .querySelectorAll('input[type=checkbox]').forEach(b => { b.checked = false; });
        showQuickSelectSummary(n);
        renderTable();
    }

    function showQuickSelectSummary(n) {
        const sel = (state.filterVals[view.filters[n].column] || {}).sel || [];
        document.getElementById(`vehMultiSum${n}`).textContent =
            !sel.length ? 'All' : sel.length === 1 ? sel[0] : `${sel.length} selected`;
    }

    // Clicking elsewhere closes an open tick-list.
    document.addEventListener('click', e => {
        document.querySelectorAll('.veh-multi[open]').forEach(d => { if (!d.contains(e.target)) d.removeAttribute('open'); });
    });

    function setQuickFilter(n, key, value) {
        const column = view.filters[n].column;
        state.filterVals[column] = { ...state.filterVals[column], [key]: value };
        renderTable();
    }

    // Rows that pass every quick filter that has a value.
    function filteredRows() {
        const active = (view.filters || []).filter(f => {
            const v = state.filterVals[f.column];
            return v && ((v.sel && v.sel.length) || v.min || v.max);
        });
        if (!active.length) return state.rows;
        return state.rows.filter(r => active.every(f => {
            const v = state.filterVals[f.column], cell = r[f.column], kind = quickFilterKind(f);
            if (kind === 'select') return v.sel.includes(blank(cell) ? '' : String(cell));
            if (blank(cell)) return false;
            if (kind === 'num') return (!v.min || cell >= parseFloat(v.min)) && (!v.max || cell <= parseFloat(v.max));
            const day = String(cell).slice(0, 10);   // ISO date, so text order is date order
            return (!v.min || day >= v.min) && (!v.max || day <= v.max);
        }));
    }

    // The vehicles in the order the table shows them.
    function sortedRows() {
        const filtered = filteredRows();
        if (!state.sort) return filtered;
        const { column, dir } = state.sort;
        return [...filtered].sort((p, q) => {
            const a = p[column], b = q[column];
            if (blank(a)) return blank(b) ? 0 : 1;
            if (blank(b)) return -1;
            return (a < b ? -1 : a > b ? 1 : 0) * dir;
        });
    }

    // Settings > Highlighting: the colour of the first rule on this column that
    // matches the value, or '' for none. Text tests ignore capitals.
    function highlightFor(value, column) {
        const hit = highlightRules.find(rule => {
            if (rule.column !== column) return false;
            if (rule.op === 'is_blank') return blank(value);
            if (rule.op === 'is_not_blank') return !blank(value);
            if (blank(value)) return false;
            const text = String(value).toLowerCase(), want = rule.value.toLowerCase();
            if (rule.op === 'contains') return text.includes(want);
            if (['>', '<', '>=', '<='].includes(rule.op)) {
                const n = Number(value), t = Number(rule.value);
                return rule.op === '>' ? n > t : rule.op === '<' ? n < t : rule.op === '>=' ? n >= t : n <= t;
            }
            const same = typeof value === 'number' ? value === Number(rule.value) : text === want;
            return rule.op === '=' ? same : !same;
        });
        if (!hit) return '';
        return `background:${hit.colour};${hit.text ? `color:${hit.text};` : ''}`;
    }

    function renderTable() {
        const columns = view.columns;
        const rows = sortedRows();
        const head = columns.map(c => {
            const mark = state.sort && state.sort.column === c.column ? (state.sort.dir === 1 ? '&#9650;' : '&#9660;') : '';
            const th = `<th${NUMBER_FORMATS.includes(c.format) ? ' class="num"' : ''} onclick="TV.sort('${c.column}')" title="${escHtml(c.column)}">${escHtml(heading(c))}<span class="sort-mark">${mark}</span></th>`;
            return th;
        }).join('');
        const body = rows.map(r => '<tr>' + columns.map(c => {
            const cell = cellFor(r[c.column], c);
            const colour = highlightFor(r[c.column], c.column);
            const changed = c.column === 'branch_name' && r.site_change;
            const td = `<td${cell.num ? ' class="num"' : ''}${colour ? ` style="${colour}"` : ''}>${cell.html}${changed ? VehicleComments.siteButton(r.stock_id, r.vrm, `Site Changed From ${r.site_change.old_site || 'Unknown'}`) : ''}${c.column === 'vrm' ? VehicleComments.button(r.stock_id, r.vrm) : ''}</td>`;
            return td;
        }).join('') + '</tr>').join('');
        document.getElementById('vehiclesTable').innerHTML =
            `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>${summaryRow(columns, rows)}`;
        const count = document.getElementById('vehQuickCount');
        if (count) count.textContent = `${rows.length.toLocaleString('en-GB')} of ${state.rows.length.toLocaleString('en-GB')} vehicles`;
    }

    // Row under the list with each column's Total or Average (set per column in
    // Settings). Blanks are left out of an average. The first cell says what
    // the row is when that column has no figure itself.
    function summaryRow(columns, rows) {
        if (!columns.some(c => c.summary === 'sum' || c.summary === 'avg') || !rows.length) return '';
        const cells = columns.map(c => {
            if (c.summary !== 'sum' && c.summary !== 'avg') return { html: '' };
            const values = rows.map(r => r[c.column]).filter(v => typeof v === 'number');
            if (!values.length) return { html: '', num: true };
            const total = values.reduce((a, b) => a + b, 0);
            const cell = cellFor(c.summary === 'sum' ? total : total / values.length, c);
            const kind = c.summary === 'sum' ? 'Total' : 'Average';
            return { ...cell, title: `${kind} of ${values.length.toLocaleString('en-GB')} vehicle${values.length === 1 ? '' : 's'}` };
        });
        if (!cells[0].html) cells[0] = { html: 'Total / Average' };
        return '<tfoot><tr>' + cells.map(cell =>
            `<td${cell.num ? ' class="num"' : ''}${cell.title ? ` title="${escHtml(cell.title)}"` : ''}>${cell.html}</td>`
        ).join('') + '</tr></tfoot>';
    }

    function csvEscape(v) {
        if (v === null || v === undefined) return '';
        const s = String(v);
        return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }

    // The list as shown (same columns, headings and sort), with the raw values
    // rather than the display formatting, so numbers stay numbers in Excel.
    function downloadCsv() {
        if (!state) return;
        const header = view.columns.map(c => csvEscape(heading(c))).join(',');
        const lines = sortedRows().map(r => view.columns.map(c => csvEscape(r[c.column])).join(','));
        // Byte order mark first so Excel reads the file as UTF-8 (e.g. £ signs).
        const blob = new Blob(['﻿' + [header, ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        const slug = state.title.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `vehicles_${slug}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // ── Settings > View Vehicles ─────────────────────────────────────────────
    // api.draft = { shown: [column settings], filters: [{ column, label }] },
    // what the popup is editing until Save.
    function openSettings() {
        api.draft = {
            shown: view.columns.map(columnSetting),
            filters: (view.filters || []).map(f => ({ ...f })),
            rules: highlightRules.map(r => ({ ...r })),
            auctions: (window.TRADE_AUCTIONS || []).slice(),
        };
        renderAuctionDraft();
        document.getElementById('vehColSearch').value = '';
        document.getElementById('filterColourInput').value =
            getComputedStyle(document.documentElement).getPropertyValue('--filter-on').trim();
        document.getElementById('chartBorderInput').value =
            getComputedStyle(document.documentElement).getPropertyValue('--chart-border').trim();
        const error = document.getElementById('settingsError');
        error.textContent = '';
        error.classList.remove('show');
        renderColumnDraft();
        renderFilterDraft();
        renderRuleDraft();
        document.getElementById('settingsModal').classList.add('open');
    }

    function closeSettings() {
        document.getElementById('settingsModal').classList.remove('open');
    }

    // From the vehicle list straight to the settings.
    function editColumns() {
        close();
        openSettings();
    }

    function renderColumnDraft() {
        const shown = api.draft.shown;
        document.getElementById('vehShownHead').textContent = `Shown, In Order (${shown.length})`;
        document.getElementById('vehShownList').innerHTML = shown.length ? shown.map((c, i) => {
            const formats = FORMATS.map(([value, label]) =>
                `<option value="${value}" ${value === c.format ? 'selected' : ''}>${label}</option>`).join('');
            const decimals = [0, 1, 2, 3, 4].map(n =>
                `<option value="${n}" ${n === c.decimals ? 'selected' : ''}>${n} dp</option>`).join('');
            const hasDecimals = NUMBER_FORMATS.includes(c.format);
            // Totals and averages only make sense on number and % columns.
            const summaries = [['none', 'No Total'], ['sum', 'Total'], ['avg', 'Average']].map(([value, label]) =>
                `<option value="${value}" ${value === (hasDecimals ? c.summary : 'none') ? 'selected' : ''}>${label}</option>`).join('');
            return `
                <div class="veh-col-item">
                    <span class="veh-col-name" title="${escHtml(c.column)}">${escHtml(c.column)}</span>
                    <input type="text" maxlength="60" value="${escHtml(c.label)}" placeholder="${escHtml(INFO[c.column].label)}"
                           title="Heading in the list" oninput="TV.draft.shown[${i}].label = this.value">
                    <select class="veh-col-format" title="How the values show"
                            onchange="TV.draft.shown[${i}].format = this.value; TV.renderColumnDraft()">${formats}</select>
                    <select class="veh-col-decimals" title="Decimal places (number, £ and %)" ${hasDecimals ? '' : 'disabled'}
                            onchange="TV.draft.shown[${i}].decimals = parseInt(this.value, 10)">${decimals}</select>
                    <select class="veh-col-summary" title="Figure in the row at the bottom of the list" ${hasDecimals ? '' : 'disabled'}
                            onchange="TV.draft.shown[${i}].summary = this.value">${summaries}</select>
                    <button type="button" class="btn btn-edit" title="Move to top" onclick="TV.moveColumnTo(${i}, 0)"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-angle-double-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move up" onclick="TV.moveColumnTo(${i}, ${i - 1})"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down" onclick="TV.moveColumnTo(${i}, ${i + 1})"
                            ${i === shown.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-edit" title="Move to bottom" onclick="TV.moveColumnTo(${i}, ${shown.length - 1})"
                            ${i === shown.length - 1 ? 'disabled' : ''}><i class="fas fa-angle-double-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="Stop showing" onclick="TV.hideColumn(${i})">
                        <i class="fas fa-times"></i></button>
                </div>`;
        }).join('') : '<div class="point-status" style="padding:6px;">No columns chosen yet. Add them from the list on the right.</div>';

        const shownNames = new Set(shown.map(c => c.column));
        const search = document.getElementById('vehColSearch').value.trim().toLowerCase();
        document.getElementById('vehHiddenList').innerHTML = COLUMNS
            .filter(c => !shownNames.has(c.column) && (!search || c.column.includes(search) || c.label.toLowerCase().includes(search)))
            .map(c => `
                <div class="veh-col-item">
                    <span class="veh-col-name" title="${escHtml(c.label)}">${escHtml(c.column)}</span>
                    <button type="button" class="btn btn-edit" onclick="TV.showColumn('${c.column}')">
                        <i class="fas fa-plus"></i> Add</button>
                </div>`).join('');
    }

    // Takes the column out and puts it at `to`.
    function moveColumnTo(i, to) {
        const shown = api.draft.shown;
        shown.splice(to, 0, shown.splice(i, 1)[0]);
        renderColumnDraft();
    }

    function hideColumn(i) {
        api.draft.shown.splice(i, 1);
        renderColumnDraft();
    }

    function removeAllColumns() {
        api.draft.shown = [];
        renderColumnDraft();
    }

    function showColumn(column) {
        api.draft.shown.push(columnSetting({ column }));
        renderColumnDraft();
    }

    // The filters shown above the list: a column and the label shown above it.
    // Any column can be used, shown in the list or not.
    function renderFilterDraft() {
        const list = api.draft.filters;
        document.getElementById('vehQuickList').innerHTML = list.map((f, i) => `
                <div class="veh-quick-row">
                    <select onchange="TV.draft.filters[${i}].column = this.value; TV.renderFilterDraft()">
                        <option value="">(Choose A Column)</option>
                        ${COLUMNS.map(c => `<option value="${c.column}" ${c.column === f.column ? 'selected' : ''}>${c.column}</option>`).join('')}
                    </select>
                    <input type="text" maxlength="60" value="${escHtml(f.label || '')}"
                           placeholder="${escHtml(f.column ? INFO[f.column].label : 'Name')}"
                           title="Name shown above the filter" oninput="TV.draft.filters[${i}].label = this.value">
                    <button type="button" class="btn btn-edit" title="Move up" onclick="TV.moveFilter(${i}, -1)"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down" onclick="TV.moveFilter(${i}, 1)"
                            ${i === list.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="Remove filter"
                            onclick="TV.draft.filters.splice(${i}, 1); TV.renderFilterDraft()"><i class="fas fa-trash"></i></button>
                </div>`).join('') || '<div class="point-status" style="padding:2px 0 6px;">No quick filters.</div>';
    }

    // Settings > Highlighting rows: a column, a test, a value and a colour.
    const RULE_OPS = [['=', 'Equals'], ['!=', 'Does Not Equal'], ['>', 'Greater Than'], ['<', 'Less Than'],
                      ['>=', 'Greater Or Equal'], ['<=', 'Less Or Equal'], ['contains', 'Contains'],
                      ['is_blank', 'Is Blank'], ['is_not_blank', 'Is Not Blank']];
    const BLANK_OPS = ['is_blank', 'is_not_blank'];

    // Excel's own Good, Bad and Neutral cell styles. The server holds the same colours.
    const RULE_STYLES = [['good', 'Good'], ['bad', 'Bad'], ['neutral', 'Neutral'], ['custom', 'Custom']];
    const RULE_SAMPLES = {
        good: 'background:#C6EFCE;color:#006100;',
        bad: 'background:#FFC7CE;color:#9C0006;',
        neutral: 'background:#FFEB9C;color:#9C5700;',
    };

    function renderRuleDraft() {
        const list = api.draft.rules;
        document.getElementById('hlRuleList').innerHTML = list.map((r, i) => `
                <div class="hl-rule-row">
                    <select onchange="TV.draft.rules[${i}].column = this.value">
                        <option value="">(Choose A Column)</option>
                        ${COLUMNS.map(c => `<option value="${c.column}" ${c.column === r.column ? 'selected' : ''}>${escHtml(c.label)}</option>`).join('')}
                    </select>
                    <select onchange="TV.draft.rules[${i}].op = this.value; TV.renderRuleDraft()">
                        ${RULE_OPS.map(([op, name]) => `<option value="${op}" ${op === r.op ? 'selected' : ''}>${name}</option>`).join('')}
                    </select>
                    <input type="text" maxlength="60" value="${escHtml(BLANK_OPS.includes(r.op) ? '' : (r.value || ''))}" placeholder="${BLANK_OPS.includes(r.op) ? '(No Value Needed)' : 'Value'}" ${BLANK_OPS.includes(r.op) ? 'disabled' : ''}
                           title="Value to test" oninput="TV.draft.rules[${i}].value = this.value">
                    <select title="Style, as in Excel" onchange="TV.draft.rules[${i}].style = this.value; TV.renderRuleDraft()">
                        ${RULE_STYLES.map(([key, name]) => `<option value="${key}" ${key === r.style ? 'selected' : ''}>${name}</option>`).join('')}
                    </select>
                    ${r.style === 'custom'
                        ? `<input type="color" class="hl-colour" value="${r.colour}" title="Highlight colour" onchange="TV.draft.rules[${i}].colour = this.value">`
                        : `<span class="hl-sample" style="${RULE_SAMPLES[r.style]}">${RULE_STYLES.find(s => s[0] === r.style)[1]}</span>`}
                    <button type="button" class="btn btn-remove-chart" title="Remove rule"
                            onclick="TV.draft.rules.splice(${i}, 1); TV.renderRuleDraft()"><i class="fas fa-trash"></i></button>
                </div>`).join('') || '<div class="point-status" style="padding:2px 0 6px;">No highlight rules.</div>';
    }

    // Settings > Auctions: a tick box per customer. A saved auction with no
    // sales in the data still shows, so it can be unticked.
    function renderAuctionDraft() {
        const chosen = api.draft.auctions;
        const names = (window.TRADE_CUSTOMERS || []).slice();
        chosen.forEach(n => { if (!names.includes(n)) names.push(n); });
        document.getElementById('auctionList').innerHTML = names.map(n => `
                <label class="auction-row" style="display:block;padding:2px 0;">
                    <input type="checkbox" ${chosen.includes(n) ? 'checked' : ''} data-name="${escHtml(n)}"
                           onchange="TV.toggleAuction(this.dataset.name, this.checked)"> ${escHtml(n.trim())}
                </label>`).join('') || '<div class="point-status">No customers loaded.</div>';
    }

    function toggleAuction(name, on) {
        const list = api.draft.auctions;
        const i = list.indexOf(name);
        if (on && i < 0) list.push(name);
        if (!on && i >= 0) list.splice(i, 1);
    }

    function addRule() {
        api.draft.rules.push({ column: '', op: '=', value: '', style: 'bad', colour: '#ff0000' });
        renderRuleDraft();
    }

    function addFilter() {
        api.draft.filters.push({ column: '', label: '' });
        renderFilterDraft();
    }

    function moveFilter(i, step) {
        const list = api.draft.filters, j = i + step;
        if (j < 0 || j >= list.length) return;
        [list[i], list[j]] = [list[j], list[i]];
        renderFilterDraft();
    }

    async function saveSettings() {
        const error = document.getElementById('settingsError');
        const button = document.getElementById('settingsSaveBtn');
        error.classList.remove('show');
        button.disabled = true;
        const box = button.closest('.modal-box');
        const buttonText = button.textContent;
        box.classList.add('busy');
        button.textContent = 'Saving...';
        try {
            // A column switched away from number, £ or % drops its total.
            const columns = api.draft.shown.map(c => ({
                ...c, label: c.label.trim(),
                summary: NUMBER_FORMATS.includes(c.format) ? c.summary : 'none',
            }));
            const payload = { columns, filters: api.draft.filters.map(f => ({ column: f.column, label: (f.label || '').trim() })) };
            const rules = api.draft.rules.map(r => ({ column: r.column, op: r.op, value: BLANK_OPS.includes(r.op) ? '' : (r.value || '').trim(), style: r.style || 'custom', colour: r.colour }));
            const resp = await fetch(BASE_URL + '/reports/trade/vehicle-view', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    view: payload, filter_colour: document.getElementById('filterColourInput').value, highlight_rules: rules,
                    auctions: api.draft.auctions, chart_border: document.getElementById('chartBorderInput').value,
                }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
            view = resp.view;
            highlightRules = resp.highlight_rules;
            window.TRADE_AUCTIONS = resp.auctions;
            document.documentElement.style.setProperty('--chart-border', resp.chart_border);
            // The main grid on the page uses the same rules (trade_report.js).
            document.dispatchEvent(new CustomEvent('trade-settings-saved'));
            document.documentElement.style.setProperty('--filter-on', resp.filter_colour);            closeSettings();
        } catch (err) {
            error.textContent = err.message;
            error.classList.add('show');
        } finally {
            box.classList.remove('busy');
            button.textContent = buttonText;
            button.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        // Clicking the dark area outside the box closes the list, as on KPI.
        document.getElementById('vehiclesModal').addEventListener('click', e => {
            if (e.target.id === 'vehiclesModal') close();
        });
    });

    const api = {
        draft: null,
        open, close, sort, downloadCsv, editColumns,
        toggleQuickSelect, clearQuickSelect, setQuickFilter,
        openSettings, closeSettings, saveSettings,
        renderColumnDraft, moveColumnTo, hideColumn, removeAllColumns, showColumn,
        renderFilterDraft, addFilter, moveFilter,
        renderRuleDraft, addRule, renderAuctionDraft, toggleAuction,
        highlightFor,
    };
    return api;
})();
