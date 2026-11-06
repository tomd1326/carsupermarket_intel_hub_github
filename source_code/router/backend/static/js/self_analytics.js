// Self-Analytics page: run a Snowflake query, then chart its rows. Grouping,
// sums and Describe A Chart are in self_analytics_core.js (window.SACore).
// Talks to routers/self_analytics.py. All text from queries, reports and
// charts is written with textContent, never innerHTML.
(function () {
    const root = document.getElementById('self-analytics');
    if (!root || !window.SACore) return;
    const BASE = root.dataset.base;
    const $ = id => document.getElementById(id);
    const C = window.SACore;

    let settings = null, reports = [], hints = [], canSql = false, isAdmin = false, options = {}, me = '', people = [];
    let current = null;          // the open report: {id, name, folder, description, visibility, users, show_table, sql}
    let presenting = false;      // the clean view: no SQL or editing controls (always on for people who only look)
    let charts = [];             // the open report's charts
    let result = null;           // {columns, rows, row_count, truncated, ms}
    let drawn = [];              // Chart.js objects, destroyed before redrawing
    let editingIndex = null, editDraft = null;
    let dirty = false;
    let tableSort = {col: -1, desc: false};

    // ── Helpers ─────────────────────────────────────────────────────────────
    async function api(path, body) {
        const opts = body === undefined ? {} : {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
        const resp = await fetch(BASE + path, opts);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || data.message || `The server replied ${resp.status}. The query may have taken too long.`);
        if (data.status === 'error') throw new Error(data.message);
        return data;
    }

    // While a save runs the popup or button is locked and says what is
    // happening (same as whileSaving in trade_report.js).
    async function whileSaving(button, busyText, work) {
        const box = button.closest('.modal-box');
        const text = button.textContent;
        if (box) box.classList.add('busy');
        button.disabled = true;
        button.textContent = busyText;
        try { await work(); } finally {
            if (box) box.classList.remove('busy');
            button.disabled = false;
            button.textContent = text;
        }
    }

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    function fillSelect(sel, opts, value) {
        sel.innerHTML = '';
        opts.forEach(([v, label]) => {
            const o = document.createElement('option');
            o.value = v; o.textContent = label; o.selected = String(v) === String(value ?? '');
            sel.appendChild(o);
        });
    }

    const TYPE_LABELS = {bar: 'Column', stacked_bar: 'Stacked Column', horizontal_bar: 'Horizontal Bar', line: 'Line',
                         area: 'Area', pie: 'Pie', doughnut: 'Doughnut', scatter: 'Scatter', table: 'Table',
                         tile: 'Single Number Tile'};
    const GRAIN_LABELS = {none: 'No Grouping', day: 'Day', week: 'Week', month: 'Month', quarter: 'Quarter', year: 'Year'};
    const AGG_LABELS = {sum: 'Total', avg: 'Average', count: 'Count', count_distinct: 'Distinct Count',
                        min: 'Lowest', max: 'Highest', median: 'Median'};
    const OP_LABELS = {'=': 'Is', '!=': 'Is Not', '>': 'Above', '>=': 'At Least', '<': 'Below', '<=': 'At Most',
                       contains: 'Contains', not_contains: 'Does Not Contain', is_empty: 'Is Empty', not_empty: 'Is Not Empty'};
    const FORMAT_LABELS = {number: 'Number', currency: 'Money', percent: 'Percent'};
    const SORT_LABELS = {x: 'By The Bottom Axis', value_desc: 'Largest First', value_asc: 'Smallest First'};
    const labelled = (keys, labels) => keys.map(k => [k, labels[k] || k]);

    function fmt(v, chart) {
        if (v === null || v === undefined || isNaN(v)) return '';
        const d = chart.decimals || 0;
        const text = Number(v).toLocaleString('en-GB', {minimumFractionDigits: d, maximumFractionDigits: d});
        if (chart.format === 'currency') return (v < 0 ? '-' : '') + settings.currency_symbol + text.replace('-', '');
        if (chart.format === 'percent') return text + '%';
        return text;
    }

    function setDirty(on) {
        dirty = on;
        const save = $('sa-save');
        if (save) save.textContent = on && current && current.id ? 'Save *' : 'Save';
    }
    window.addEventListener('beforeunload', e => { if (dirty && canSql) { e.preventDefault(); e.returnValue = ''; } });

    // ── Load ────────────────────────────────────────────────────────────────
    async function load(selectId) {
        try {
            const data = await api('/self-analytics/data');
            settings = data.settings; reports = data.reports; hints = data.hints;
            canSql = data.can_write_sql; isAdmin = data.is_admin; options = data.options; me = data.user;
            people = data.people || [];
            applyAppearance();
            // First load: a link with ?report=ID opens and runs that report;
            // ?present=1 opens it in the clean view.
            const firstLoad = selectId === undefined;
            if (firstLoad) {
                const params = new URLSearchParams(location.search);
                selectId = params.get('report') || undefined;
                setPresenting(!canSql || params.get('present') === '1');
            }
            fillReports(selectId, firstLoad);
        } catch (e) {
            status(e.message, true);
        }
    }

    function applyAppearance() {
        root.style.setProperty('--sa-cols', settings.charts_per_row);
        root.style.setProperty('--sa-chart-h', settings.chart_height_px + 'px');
        root.style.setProperty('--sa-title', settings.chart_title_px + 'px');
        root.style.setProperty('--sa-report-title', settings.report_title_px + 'px');
        root.style.setProperty('--sa-tile-value', settings.tile_value_px + 'px');
        root.style.setProperty('--sa-tile-label', settings.tile_label_px + 'px');
        $('sa-intro').textContent = settings.intro;
        if (window.Chart) {
            Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
            Chart.defaults.font.size = settings.axis_font_px;
        }
    }

    function fillReports(selectId, run = false) {
        const sel = $('sa-report');
        sel.innerHTML = '';
        const first = document.createElement('option');
        first.value = ''; first.textContent = canSql ? 'New Report (Not Saved)' : 'Choose A Report';
        sel.appendChild(first);
        const folders = [...new Set(reports.map(r => r.folder || ''))].sort();
        folders.forEach(folder => {
            const parent = folder ? Object.assign(document.createElement('optgroup'), {label: folder}) : sel;
            reports.filter(r => (r.folder || '') === folder).forEach(r => {
                const o = document.createElement('option');
                o.value = r.id;
                const who = r.visibility === 'everyone' ? '' : r.visibility === 'chosen' ? ' (Chosen People)' : ' (Only Me)';
                o.textContent = r.name + (canSql ? who : '') + (canSql && r.created_by !== me ? ` - ${r.created_by}` : '');
                parent.appendChild(o);
            });
            if (parent !== sel) sel.appendChild(parent);
        });
        const dl = $('sa-folder-list');
        if (dl) { dl.innerHTML = ''; folders.filter(Boolean).forEach(f => { const o = document.createElement('option'); o.value = f; dl.appendChild(o); }); }
        sel.value = selectId && reports.some(r => String(r.id) === String(selectId)) ? String(selectId) : '';
        if (sel.value) openReport(sel.value, run);
        else if (!current) newReport();
    }

    function newReport() {
        current = {id: null, name: '', folder: '', description: '', visibility: 'only_me', users: [],
                   show_table: false, sql: ''};
        charts = [];
        showReportFields();
        renderCharts();
        setDirty(false);
    }

    function showReportFields() {
        const head = $('sa-report-head');
        head.style.display = current && current.id ? '' : 'none';
        if (current && current.id) {
            $('sa-report-title').textContent = current.name;
            $('sa-report-desc').textContent = current.description || '';
        }
        // The data table: always while editing; in the clean view only if the report says so.
        $('sa-data-panel').style.display = !presenting || (current && current.show_table) ? '' : 'none';
        if (!canSql) return;
        $('sa-sql').value = current.sql;
        $('sa-delete').style.display = current.id ? '' : 'none';
        $('sa-export').style.display = current.id ? '' : 'none';
    }

    function setPresenting(on) {
        presenting = !canSql || on;
        root.classList.toggle('presenting', presenting);
        if ($('sa-present')) $('sa-present').textContent = presenting ? 'Edit' : 'Present';
        if (current) showReportFields();
        renderCharts();
    }

    function openReport(id, run = true) {
        const r = reports.find(x => String(x.id) === String(id));
        if (!r) { newReport(); return; }
        current = {id: r.id, name: r.name, folder: r.folder, description: r.description || '',
                   visibility: r.visibility || (r.shared ? 'everyone' : 'only_me'), users: (r.users || []).slice(),
                   show_table: !!r.show_table, sql: r.sql, created_by: r.created_by};
        charts = JSON.parse(JSON.stringify(r.charts || []));
        showReportFields();
        setDirty(false);
        const url = new URL(location.href);
        url.searchParams.set('report', r.id);
        history.replaceState(null, '', url);
        if (run) runQuery();
        else renderCharts();
    }

    $('sa-report').addEventListener('change', () => {
        if (dirty && canSql && !confirm('Your changes to this report are not saved. Open the other one anyway?')) {
            $('sa-report').value = current && current.id ? String(current.id) : '';
            return;
        }
        const id = $('sa-report').value;
        if (id) openReport(id); else { newReport(); result = null; renderData(); }
    });

    // ── Run ─────────────────────────────────────────────────────────────────
    function status(text, error) {
        const s = $('sa-status');
        s.textContent = text;
        s.className = 'sa-status' + (error ? ' error' : '');
    }

    // A saved report whose SQL is unchanged is run by its id, so the server can
    // reuse its rows for the cache time; refresh asks for fresh rows.
    async function runQuery(refresh = false) {
        const btn = presenting ? $('sa-refresh') : $('sa-run');
        const saved = current && current.id && (!canSql || $('sa-sql').value.trim() === (current.sql || '').trim());
        const body = saved ? {report_id: current.id, refresh: refresh === true}
            : canSql ? {sql: $('sa-sql').value} : null;
        if (!body) { status('Choose a report first.', true); return; }
        status('Running...');
        try {
            await whileSaving(btn, 'Running...', async () => {
                const data = await api('/self-analytics/run', body);
                result = data;
                tableSort = {col: -1, desc: false};
                $('sa-as-at').textContent = `Data as at ${data.as_at}` + (data.from_cache ? ' (saved copy, Refresh for the latest)' : '');
                status(`${data.row_count.toLocaleString()} rows` + (data.from_cache ? '' : ` in ${(data.ms / 1000).toFixed(1)}s`) +
                    (data.truncated ? ` - cut off at the ${settings.row_limit.toLocaleString()} row limit` : ''), data.truncated);
                $('sa-csv').disabled = false;
                renderData();
                renderCharts();
            });
        } catch (e) {
            status(e.message, true);
        }
    }

    $('sa-run').addEventListener('click', () => runQuery(false));
    $('sa-refresh').addEventListener('click', () => runQuery(true));
    if ($('sa-sql')) {
        $('sa-sql').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runQuery(false); } });
        $('sa-sql').addEventListener('input', () => setDirty(true));
    }

    $('sa-copy-link').addEventListener('click', e => {
        if (!current || !current.id) { status('Save the report first; then it has a link.', true); return; }
        const url = `${location.origin}${location.pathname}?report=${current.id}&present=1`;
        navigator.clipboard.writeText(url).then(() => {
            e.target.textContent = 'Copied';
            setTimeout(() => { e.target.textContent = 'Copy Link'; }, 1500);
        }).catch(() => prompt('Copy this link:', url));
    });
    if ($('sa-present')) $('sa-present').addEventListener('click', () => setPresenting(!presenting));

    // ── Save ────────────────────────────────────────────────────────────────
    function reportPayload() {
        return {name: current.name, folder: current.folder, description: current.description,
                visibility: current.visibility, users: current.users, show_table: current.show_table,
                sql: $('sa-sql').value, charts};
    }

    async function saveReport(asNew, button) {
        if (!current.name) { openDetails(); $('sa-d-note').textContent = 'Give the report a name, then save.'; return; }
        const payload = reportPayload();
        if (!asNew && current.id) payload.id = current.id;
        if (asNew && current.id) payload.name = payload.name + ' (Copy)';
        try {
            await whileSaving(button, 'Saving...', async () => {
                const data = await api('/self-analytics/save', payload);
                setDirty(false);
                await load(data.report.id);
                status('Saved.');
            });
        } catch (e) {
            status(e.message, true);
            throw e;
        }
    }

    // ── Details and sharing ─────────────────────────────────────────────────
    function openDetails() {
        $('sa-d-name').value = current.name;
        $('sa-d-folder').value = current.folder;
        $('sa-d-desc').value = current.description || '';
        $('sa-d-table').checked = !!current.show_table;
        document.querySelectorAll('input[name="sa-d-vis"]').forEach(r => { r.checked = r.value === current.visibility; });
        const box = $('sa-d-people');
        box.innerHTML = '';
        people.filter(p => p.username !== me).forEach(p => {
            const lab = document.createElement('label');
            const cb = document.createElement('input');
            cb.type = 'checkbox'; cb.value = p.username; cb.checked = current.users.includes(p.username);
            lab.appendChild(cb);
            lab.appendChild(document.createTextNode(p.name));
            box.appendChild(lab);
        });
        showPeople();
        $('sa-d-note').textContent = '';
        openModal('sa-details-modal');
    }

    function showPeople() {
        const chosen = document.querySelector('input[name="sa-d-vis"]:checked');
        $('sa-d-people-wrap').style.display = chosen && chosen.value === 'chosen' ? '' : 'none';
    }

    if ($('sa-details-modal')) {
        $('sa-details-btn').addEventListener('click', openDetails);
        document.querySelectorAll('input[name="sa-d-vis"]').forEach(r => r.addEventListener('change', showPeople));
        $('sa-d-cancel').addEventListener('click', () => closeModal('sa-details-modal'));
        $('sa-d-save').addEventListener('click', async e => {
            const vis = document.querySelector('input[name="sa-d-vis"]:checked');
            current.name = $('sa-d-name').value.trim();
            current.folder = $('sa-d-folder').value.trim();
            current.description = $('sa-d-desc').value.trim();
            current.show_table = $('sa-d-table').checked;
            current.visibility = vis ? vis.value : 'only_me';
            current.users = [...$('sa-d-people').querySelectorAll('input:checked')].map(cb => cb.value);
            if (!current.name) { $('sa-d-note').textContent = 'A name is needed.'; return; }
            try {
                await saveReport(false, e.target);
                closeModal('sa-details-modal');
            } catch (err) { $('sa-d-note').textContent = err.message; }
        });
    }

    // ── Import and export ───────────────────────────────────────────────────
    if ($('sa-import-modal')) {
        $('sa-import-btn').addEventListener('click', () => {
            $('sa-i-text').value = ''; $('sa-i-file').value = ''; $('sa-i-note').textContent = '';
            openModal('sa-import-modal');
        });
        $('sa-i-file').addEventListener('change', async () => {
            const file = $('sa-i-file').files[0];
            if (file) $('sa-i-text').value = await file.text();
        });
        $('sa-i-cancel').addEventListener('click', () => closeModal('sa-import-modal'));
        $('sa-i-go').addEventListener('click', async e => {
            let payload;
            try { payload = JSON.parse($('sa-i-text').value); }
            catch (err) { $('sa-i-note').textContent = 'That is not valid JSON: ' + err.message; return; }
            try {
                await whileSaving(e.target, 'Importing...', async () => {
                    const data = await api('/self-analytics/import', payload);
                    closeModal('sa-import-modal');
                    setDirty(false);
                    await load(data.reports[0].id);
                    runQuery(false);
                    status(`${data.reports.length} report${data.reports.length === 1 ? '' : 's'} imported.`);
                });
            } catch (err) { $('sa-i-note').textContent = err.message; }
        });
        $('sa-export').addEventListener('click', () => {
            if (current && current.id) location.href = `${BASE}/self-analytics/export/${current.id}`;
        });
    }

    if ($('sa-save')) {
        $('sa-save').addEventListener('click', e => saveReport(false, e.target).catch(() => {}));
        $('sa-save-as').addEventListener('click', e => saveReport(true, e.target).catch(() => {}));
        $('sa-new').addEventListener('click', () => {
            if (dirty && !confirm('Your changes to this report are not saved. Start a new one anyway?')) return;
            $('sa-report').value = '';
            newReport();
            result = null; renderData();
        });
        $('sa-delete').addEventListener('click', async e => {
            if (!current || !current.id || !confirm(`Delete "${current.name}"? This cannot be undone.`)) return;
            try {
                await whileSaving(e.target, 'Removing...', async () => {
                    await api('/self-analytics/delete', {id: current.id});
                    current = null;
                    await load();
                    status('Deleted.');
                });
            } catch (err) { status(err.message, true); }
        });
    }

    // ── Data table and CSV ──────────────────────────────────────────────────
    function hintFor(column) {
        const name = column.toLowerCase();
        const h = hints.find(x => x.title.toLowerCase() === name || x.title.toLowerCase().endsWith('.' + name)
            || new RegExp(`(^|[^a-z0-9_])${name}([^a-z0-9_]|$)`).test(String(x.columns || '').toLowerCase()));
        return h ? `${h.title}: ${h.definition}` : '';
    }

    function renderData() {
        const box = $('sa-data');
        box.innerHTML = '';
        if (!result) { box.appendChild(el('div', 'sa-empty p-2', 'Run a query to see its rows.')); $('sa-data-note').textContent = ''; return; }
        let rows = result.rows.slice();
        if (tableSort.col >= 0) {
            const i = tableSort.col;
            rows.sort((a, b) => {
                const x = a[i], y = b[i];
                if (x === y) return 0;
                if (x === null || x === undefined) return 1;
                if (y === null || y === undefined) return -1;
                const r = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
                return tableSort.desc ? -r : r;
            });
        }
        const shown = rows.slice(0, settings.table_rows_shown);
        $('sa-data-note').textContent = `Showing ${shown.length.toLocaleString()} of ${result.rows.length.toLocaleString()} rows. ` +
            'Click a heading to sort. Hover a heading for its Semantic Layer definition.';
        const table = el('table', 'sa-table');
        const head = table.createTHead().insertRow();
        result.columns.forEach((c, i) => {
            const th = el('th', '', c.name + (tableSort.col === i ? (tableSort.desc ? ' (Z-A)' : ' (A-Z)') : ''));
            th.title = hintFor(c.name) || c.type;
            th.addEventListener('click', () => {
                tableSort = {col: i, desc: tableSort.col === i ? !tableSort.desc : false};
                renderData();
            });
            head.appendChild(th);
        });
        const body = table.createTBody();
        shown.forEach(r => {
            const tr = body.insertRow();
            r.forEach((v, i) => {
                const td = tr.insertCell();
                td.textContent = v === null || v === undefined ? '' : String(v);
                if (result.columns[i].type === 'number') td.className = 'num';
            });
        });
        box.appendChild(table);
    }

    $('sa-csv').addEventListener('click', () => {
        if (!result) return;
        const cell = v => { const s = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
        const lines = [result.columns.map(c => cell(c.name)).join(',')].concat(result.rows.map(r => r.map(cell).join(',')));
        const blob = new Blob([lines.join('\n')], {type: 'text/csv'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = ((current && current.name) || 'self_analytics').replace(/[^\w\- ]+/g, '') + '.csv';
        a.click();
        URL.revokeObjectURL(a.href);
    });

    // ── Charts ──────────────────────────────────────────────────────────────
    function blankChart() {
        const dates = result ? result.columns.filter(c => c.type === 'date' || c.type === 'datetime') : [];
        return {id: 'c' + Date.now(), title: '', subtitle: '', type: settings.default_chart_type,
                x: dates.length ? dates[0].name : null, grain: dates.length ? settings.default_grain : 'none',
                split: null, measures: [], filters: [], sort: 'x', top_n: 0, format: 'number', decimals: 0,
                from_zero: true, data_labels: false, legend: true, width: 1, description: ''};
    }

    const colourOf = (chart, i, m) => (m && m.color) || settings.palette[i % settings.palette.length];

    function dataLabelPlugin(chart) {
        return {
            id: 'saLabels',
            afterDatasetsDraw(c) {
                if (!chart.data_labels) return;
                const ctx = c.ctx;
                ctx.save();
                ctx.font = `${settings.data_label_px}px 'DM Sans', sans-serif`;
                ctx.fillStyle = '#334155';
                ctx.textAlign = 'center';
                c.data.datasets.forEach((ds, di) => {
                    const meta = c.getDatasetMeta(di);
                    if (meta.hidden) return;
                    meta.data.forEach((elem, i) => {
                        const v = ds.data[i];
                        if (v === null || v === undefined || typeof v === 'object') return;
                        const p = elem.tooltipPosition();
                        const horizontal = chart.type === 'horizontal_bar';
                        ctx.textBaseline = horizontal ? 'middle' : 'bottom';
                        ctx.fillText(fmt(v, chart), p.x + (horizontal ? 18 : 0), p.y - (horizontal ? 0 : 4));
                    });
                });
                ctx.restore();
            },
        };
    }

    function chartConfig(chart, data) {
        const pie = chart.type === 'pie' || chart.type === 'doughnut';
        const horizontal = chart.type === 'horizontal_bar';
        const stacked = chart.type === 'stacked_bar';
        const line = chart.type === 'line' || chart.type === 'area';
        const typeMap = {bar: 'bar', stacked_bar: 'bar', horizontal_bar: 'bar', line: 'line', area: 'line',
                         pie: 'pie', doughnut: 'doughnut', scatter: 'scatter'};
        let datasets;
        if (pie) {
            const s = data.series[0] || {label: '', data: []};
            datasets = [{label: s.label, data: s.data, backgroundColor: data.labels.map((_, i) => settings.palette[i % settings.palette.length])}];
        } else {
            datasets = data.series.map((s, i) => {
                const colour = colourOf(chart, i, s.measure && data.series.length === chart.measures.length ? s.measure : null);
                return {label: s.label, data: s.data, borderColor: colour, backgroundColor: line ? colour + '33' : colour,
                        fill: chart.type === 'area', tension: 0.2, spanGaps: true, pointRadius: line ? 3 : 2, borderWidth: line ? 2 : 0};
            });
        }
        const tick = {font: {size: settings.axis_font_px}};
        const valueTicks = {...tick, callback: v => fmt(v, chart)};
        const scales = pie ? {} : {
            x: {stacked, ticks: horizontal ? valueTicks : tick, beginAtZero: horizontal && chart.from_zero,
                type: chart.type === 'scatter' ? 'linear' : undefined},
            y: {stacked, ticks: horizontal ? tick : valueTicks, beginAtZero: !horizontal && chart.from_zero},
        };
        return {
            type: typeMap[chart.type] || 'bar',
            data: {labels: data.labels, datasets},
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                indexAxis: horizontal ? 'y' : 'x',
                plugins: {
                    legend: {display: chart.legend && (pie || datasets.length > 1),
                             labels: {font: {size: settings.legend_font_px}, boxWidth: 12}},
                    tooltip: {callbacks: {label: ctx => {
                        const v = ctx.raw && typeof ctx.raw === 'object' ? ctx.raw.y : ctx.raw;
                        return `${ctx.dataset.label}: ${fmt(v, chart)}`;
                    }}},
                },
                scales,
            },
            plugins: [dataLabelPlugin(chart)],
        };
    }

    function chartTable(chart, data) {
        const wrap = el('div', 'sa-table-wrap');
        const table = el('table', 'sa-table');
        const head = table.createTHead().insertRow();
        head.appendChild(el('th', '', chart.x ? C.columnLabel(chart.grain !== 'none' ? chart.grain : chart.x) : ''));
        data.series.forEach(s => head.appendChild(el('th', '', s.label)));
        const body = table.createTBody();
        data.labels.forEach((label, i) => {
            const tr = body.insertRow();
            tr.insertCell().textContent = label;
            data.series.forEach(s => { const td = tr.insertCell(); td.className = 'num'; td.textContent = fmt(s.data[i], chart); });
        });
        wrap.appendChild(table);
        return wrap;
    }

    // One number: the first value worked out over every row that passes the
    // filters (the X column and split are not used).
    function chartTile(chart) {
        const one = {...chart, x: null, split: null, grain: 'none', measures: chart.measures.slice(0, 1), top_n: 0};
        const data = C.buildSeries(one, result.columns, result.rows);
        const value = data.series[0] ? data.series[0].data[0] : null;
        const box = el('div', 'sa-tile');
        box.appendChild(el('div', 'sa-tile-value', value === null || value === undefined ? '-' : fmt(value, chart)));
        box.appendChild(el('div', 'sa-tile-label', data.series[0] ? data.series[0].label : ''));
        return box;
    }

    function renderCharts() {
        if (!settings) return;
        drawn.forEach(c => c.destroy());
        drawn = [];
        const grid = $('sa-charts');
        grid.innerHTML = '';
        if (!charts.length) {
            grid.appendChild(el('div', 'sa-empty', result ? 'No charts yet. Describe one above or add one by hand.'
                : 'Run a query, then describe a chart above or add one by hand.'));
            return;
        }
        charts.forEach((chart, index) => {
            const card = el('div', 'chart-card');
            card.style.gridColumn = `span ${Math.min(chart.width || 1, settings.charts_per_row)}`;
            const head = el('div', 'sa-card-head');
            head.appendChild(el('h2', '', chart.title || C.titleFor(chart)));
            const btn = (text, title, fn) => {
                const b = el('button', 'btn btn-sm btn-link', text);
                b.type = 'button'; b.title = title;
                b.addEventListener('click', fn);
                head.appendChild(b);
            };
            btn('Edit', 'Change this chart', () => openEditor(index));
            btn('Copy', 'Add a copy of this chart', () => { charts.splice(index + 1, 0, {...JSON.parse(JSON.stringify(chart)), id: 'c' + Date.now()}); setDirty(true); renderCharts(); });
            if (index > 0) btn('Earlier', 'Move this chart earlier', () => { [charts[index - 1], charts[index]] = [charts[index], charts[index - 1]]; setDirty(true); renderCharts(); });
            if (index < charts.length - 1) btn('Later', 'Move this chart later', () => { [charts[index + 1], charts[index]] = [charts[index], charts[index + 1]]; setDirty(true); renderCharts(); });
            btn('PNG', 'Download this chart as a picture', () => downloadPng(index));
            btn('Remove', 'Remove this chart', () => { if (confirm('Remove this chart?')) { charts.splice(index, 1); setDirty(true); renderCharts(); } });
            card.appendChild(head);
            if (chart.subtitle) card.appendChild(el('div', 'sa-card-sub', chart.subtitle));
            if (!result) {
                card.appendChild(el('div', 'sa-empty', 'Run the query to draw this chart.'));
                grid.appendChild(card);
                return;
            }
            const data = C.buildSeries(chart, result.columns, result.rows);
            data.notes.forEach(n => card.appendChild(el('div', 'sa-card-note', n)));
            if (chart.type === 'tile') {
                card.appendChild(chartTile(chart));
            } else if (chart.type === 'table') {
                card.appendChild(chartTable(chart, data));
            } else {
                const wrap = el('div', 'chart-wrap');
                const canvas = document.createElement('canvas');
                canvas.dataset.index = index;
                wrap.appendChild(canvas);
                card.appendChild(wrap);
                grid.appendChild(card);
                try { drawn.push(new Chart(canvas, chartConfig(chart, data))); }
                catch (e) { card.appendChild(el('div', 'sa-card-note', 'Could not draw: ' + e.message)); }
                return;
            }
            grid.appendChild(card);
        });
    }

    function downloadPng(index) {
        const canvas = $('sa-charts').querySelector(`canvas[data-index="${index}"]`);
        if (!canvas) { alert('Only drawn charts can be downloaded as a picture.'); return; }
        // White background, so the picture is not see-through.
        const out = document.createElement('canvas');
        out.width = canvas.width; out.height = canvas.height;
        const ctx = out.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, out.width, out.height);
        ctx.drawImage(canvas, 0, 0);
        const a = document.createElement('a');
        a.href = out.toDataURL('image/png');
        a.download = (charts[index].title || C.titleFor(charts[index])).replace(/[^\w\- ]+/g, '').slice(0, 80) + '.png';
        a.click();
    }

    // ── Describe A Chart ────────────────────────────────────────────────────
    function describeChart() {
        const text = $('sa-describe').value.trim();
        const out = $('sa-understood');
        if (!text) return;
        if (!result) { out.textContent = 'Run a query first, so the columns are known.'; return; }
        const r = C.describe(text, result.columns, hints,
            {agg: settings.describe_default_agg, grain: settings.default_grain, type: settings.default_chart_type});
        if (!r.ok) {
            out.textContent = 'No column names were found in that. Use words from the column names, e.g. ' +
                result.columns.slice(0, 4).map(c => C.columnLabel(c.name).toLowerCase()).join(', ') + '.';
            return;
        }
        charts.push({...blankChart(), ...r.chart, id: 'c' + Date.now(), description: text});
        out.textContent = 'Understood as: ' + r.understood.join('. ') + '. Click Edit on the chart to change anything.';
        setDirty(true);
        renderCharts();
    }

    $('sa-describe-go').addEventListener('click', describeChart);
    $('sa-describe').addEventListener('keydown', e => { if (e.key === 'Enter') describeChart(); });
    $('sa-add-chart').addEventListener('click', () => {
        if (!result) { $('sa-understood').textContent = 'Run a query first, so the columns are known.'; return; }
        openEditor(null);
    });

    // ── Chart editor ────────────────────────────────────────────────────────
    const openModal = id => $(id).classList.add('open');
    const closeModal = id => $(id).classList.remove('open');

    function columnOptions(blankLabel) {
        const cols = result ? result.columns.map(c => [c.name, C.columnLabel(c.name)]) : [];
        // Keep a column a saved chart names even if this query no longer returns it.
        const named = [editDraft.x, editDraft.split].concat(editDraft.measures.flatMap(m => [m.column, m.minus]))
            .filter(n => n && !cols.some(c => c[0].toLowerCase() === n.toLowerCase()));
        return [['', blankLabel]].concat(cols, [...new Set(named)].map(n => [n, n + ' (Not In Query)']));
    }

    function openEditor(index) {
        editingIndex = index;
        editDraft = JSON.parse(JSON.stringify(index === null ? blankChart() : charts[index]));
        $('sa-e-title').value = editDraft.title;
        $('sa-e-subtitle').value = editDraft.subtitle;
        fillSelect($('sa-e-type'), labelled(options.chart_types, TYPE_LABELS), editDraft.type);
        fillSelect($('sa-e-x'), columnOptions('Nothing (One Total)'), editDraft.x);
        fillSelect($('sa-e-grain'), labelled(options.grains, GRAIN_LABELS), editDraft.grain);
        fillSelect($('sa-e-split'), columnOptions('No Split'), editDraft.split);
        fillSelect($('sa-e-sort'), labelled(options.sorts, SORT_LABELS), editDraft.sort);
        fillSelect($('sa-e-format'), labelled(options.formats, FORMAT_LABELS), editDraft.format);
        $('sa-e-top').value = editDraft.top_n;
        $('sa-e-decimals').value = editDraft.decimals;
        $('sa-e-width').value = editDraft.width;
        $('sa-e-zero').checked = editDraft.from_zero;
        $('sa-e-labels').checked = editDraft.data_labels;
        $('sa-e-legend').checked = editDraft.legend;
        if (!editDraft.measures.length) editDraft.measures.push({column: '', agg: 'sum', minus: null, as_pct: false, label: '', color: ''});
        renderMeasures();
        renderFilters();
        $('sa-e-note').textContent = '';
        openModal('sa-editor-modal');
    }

    function rowInput(type, value, onChange) {
        const i = document.createElement('input');
        i.type = type;
        if (type === 'checkbox') i.checked = !!value; else i.value = value ?? '';
        i.addEventListener(type === 'checkbox' || type === 'color' ? 'change' : 'input', () => onChange(type === 'checkbox' ? i.checked : i.value));
        return i;
    }

    function rowSelect(opts, value, onChange) {
        const s = document.createElement('select');
        fillSelect(s, opts, value);
        s.addEventListener('change', () => onChange(s.value));
        return s;
    }

    function removeButton(list, i, rerender) {
        const b = el('button', 'btn btn-sm btn-outline-secondary', 'Remove');
        b.type = 'button';
        b.addEventListener('click', () => { list.splice(i, 1); rerender(); });
        return b;
    }

    function renderMeasures() {
        const body = $('sa-e-measures').querySelector('tbody');
        body.innerHTML = '';
        editDraft.measures.forEach((m, i) => {
            const tr = body.insertRow();
            tr.insertCell().appendChild(rowSelect(labelled(options.aggs, AGG_LABELS), m.agg, v => { m.agg = v; }));
            tr.insertCell().appendChild(rowSelect(columnOptions(m.agg === 'count' ? 'Rows' : 'Choose'), m.column, v => { m.column = v || null; }));
            tr.insertCell().appendChild(rowSelect(columnOptions('Nothing'), m.minus, v => { m.minus = v || null; }));
            tr.insertCell().appendChild(rowInput('checkbox', m.as_pct, v => { m.as_pct = v; }));
            tr.insertCell().appendChild(rowInput('text', m.label, v => { m.label = v; }));
            const colour = m.color || settings.palette[i % settings.palette.length];
            tr.insertCell().appendChild(rowInput('color', colour, v => { m.color = v; }));
            tr.insertCell().appendChild(removeButton(editDraft.measures, i, renderMeasures));
        });
    }

    function renderFilters() {
        const body = $('sa-e-filters').querySelector('tbody');
        body.innerHTML = '';
        editDraft.filters.forEach((f, i) => {
            const tr = body.insertRow();
            tr.insertCell().appendChild(rowSelect(columnOptions('Choose'), f.column, v => { f.column = v; }));
            tr.insertCell().appendChild(rowSelect(labelled(options.filter_ops, OP_LABELS), f.op, v => { f.op = v; }));
            tr.insertCell().appendChild(rowInput('text', f.value, v => { f.value = v; }));
            tr.insertCell().appendChild(removeButton(editDraft.filters, i, renderFilters));
        });
    }

    $('sa-e-add-measure').addEventListener('click', () => {
        editDraft.measures.push({column: '', agg: editDraft.measures.length ? editDraft.measures[0].agg : 'sum',
                                 minus: null, as_pct: false, label: '', color: ''});
        renderMeasures();
    });
    $('sa-e-add-filter').addEventListener('click', () => {
        editDraft.filters.push({column: '', op: '=', value: ''});
        renderFilters();
    });
    $('sa-e-cancel').addEventListener('click', () => closeModal('sa-editor-modal'));
    $('sa-e-apply').addEventListener('click', () => {
        const d = editDraft;
        d.title = $('sa-e-title').value.trim();
        d.subtitle = $('sa-e-subtitle').value.trim();
        d.type = $('sa-e-type').value;
        d.x = $('sa-e-x').value || null;
        d.grain = $('sa-e-grain').value;
        d.split = $('sa-e-split').value || null;
        d.sort = $('sa-e-sort').value;
        d.format = $('sa-e-format').value;
        d.top_n = Math.max(0, parseInt($('sa-e-top').value, 10) || 0);
        d.decimals = Math.min(4, Math.max(0, parseInt($('sa-e-decimals').value, 10) || 0));
        d.width = Math.min(4, Math.max(1, parseInt($('sa-e-width').value, 10) || 1));
        d.from_zero = $('sa-e-zero').checked;
        d.data_labels = $('sa-e-labels').checked;
        d.legend = $('sa-e-legend').checked;
        d.measures = d.measures.filter(m => m.column || m.agg === 'count');
        d.filters = d.filters.filter(f => f.column);
        if (!d.measures.length) { $('sa-e-note').textContent = 'Add at least one value.'; return; }
        if (d.type === 'scatter' && (!d.x || !d.measures[0].column)) {
            $('sa-e-note').textContent = 'A scatter chart needs a column along the bottom and a value column.';
            return;
        }
        if (editingIndex === null) charts.push(d); else charts[editingIndex] = d;
        closeModal('sa-editor-modal');
        setDirty(true);
        renderCharts();
    });

    // ── Page settings (admin) ───────────────────────────────────────────────
    let paletteDraft = [];

    function renderPalette() {
        const box = $('sa-s-palette');
        box.innerHTML = '';
        paletteDraft.forEach((c, i) => {
            box.appendChild(rowInput('color', c, v => { paletteDraft[i] = v; }));
            const b = el('button', 'btn btn-sm btn-link p-0 me-2', 'Remove');
            b.type = 'button';
            b.addEventListener('click', () => { paletteDraft.splice(i, 1); renderPalette(); });
            box.appendChild(b);
        });
        const add = el('button', 'btn btn-sm btn-outline-primary', 'Add Colour');
        add.type = 'button';
        add.addEventListener('click', () => { paletteDraft.push('#64748b'); renderPalette(); });
        box.appendChild(add);
    }

    const SETTING_INPUTS = {
        row_limit: 'sa-s-row-limit', max_response_mb: 'sa-s-max-mb', table_rows_shown: 'sa-s-table-rows',
        charts_per_row: 'sa-s-cols', chart_height_px: 'sa-s-height', chart_title_px: 'sa-s-title',
        axis_font_px: 'sa-s-axis', legend_font_px: 'sa-s-legend', data_label_px: 'sa-s-labels',
        cache_minutes: 'sa-s-cache', report_title_px: 'sa-s-report-title', tile_value_px: 'sa-s-tile-value',
        tile_label_px: 'sa-s-tile-label',
    };

    if ($('sa-settings-modal')) {
        $('sa-settings-btn').addEventListener('click', () => {
            $('sa-s-intro').value = settings.intro;
            Object.entries(SETTING_INPUTS).forEach(([k, id]) => { $(id).value = settings[k]; });
            fillSelect($('sa-s-type'), labelled(options.chart_types, TYPE_LABELS), settings.default_chart_type);
            fillSelect($('sa-s-grain'), labelled(options.grains, GRAIN_LABELS), settings.default_grain);
            fillSelect($('sa-s-agg'), labelled(options.aggs, AGG_LABELS), settings.describe_default_agg);
            $('sa-s-currency').value = settings.currency_symbol;
            paletteDraft = settings.palette.slice();
            renderPalette();
            $('sa-s-note').textContent = '';
            openModal('sa-settings-modal');
        });
        $('sa-s-cancel').addEventListener('click', () => closeModal('sa-settings-modal'));
        $('sa-s-save').addEventListener('click', async e => {
            const payload = {intro: $('sa-s-intro').value, default_chart_type: $('sa-s-type').value,
                             default_grain: $('sa-s-grain').value, describe_default_agg: $('sa-s-agg').value,
                             currency_symbol: $('sa-s-currency').value, palette: paletteDraft};
            Object.entries(SETTING_INPUTS).forEach(([k, id]) => { payload[k] = $(id).value; });
            try {
                await whileSaving(e.target, 'Saving...', async () => {
                    settings = (await api('/self-analytics/settings', payload)).settings;
                    applyAppearance();
                    closeModal('sa-settings-modal');
                    renderData();
                    renderCharts();
                });
            } catch (err) { $('sa-s-note').textContent = err.message; }
        });
        $('sa-s-reset').addEventListener('click', async e => {
            if (!confirm('Put every Self-Analytics page setting back to the default? Saved reports are not changed.')) return;
            try {
                await whileSaving(e.target, 'Saving...', async () => {
                    settings = (await api('/self-analytics/settings/reset', {})).settings;
                    applyAppearance();
                    closeModal('sa-settings-modal');
                    renderData();
                    renderCharts();
                });
            } catch (err) { $('sa-s-note').textContent = err.message; }
        });
    }

    load();
})();
