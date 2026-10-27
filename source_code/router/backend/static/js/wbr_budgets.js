// Weekly Business Review > Settings > Budgets (admin only). The monthly budget
// lines from the old review sheet's Budget tab, kept in budget_lines /
// budget_values (routers/budgets.py).
//
// Import CSV saves straight away. Typed figures and Maps To choices are kept
// as a draft and saved by the popup's Save button (kpi.js saveWbrSettings
// calls WbrBudgets.save()); Cancel drops them. escHtml comes from charts.js.
const WbrBudgets = (() => {
    const URL_BASE = BASE_URL + '/weekly-business-review/budgets';
    let data = null;          // {lines, months} as loaded
    const CHART = 'chart';    // map_source of a line mapped to a KPI chart
    let edits = {};           // 'lineId|month' -> text typed
    let mapEdits = {};        // lineId -> 'source|column' or ''
    let loading = null;

    const el = id => document.getElementById(id);
    const status = text => { el('wbrBudgetStatus').textContent = text || ''; };
    const changeCount = () => Object.keys(edits).length + Object.keys(mapEdits).length;

    // A request's JSON, or an Error saying what came back instead (a server
    // error page, a login page after the session ran out).
    async function getJson(url, options) {
        const r = await fetch(url, options);
        const text = await r.text();
        let json;
        try { json = JSON.parse(text); } catch (e) {
            throw new Error(`the server answered ${r.status} without data${r.status === 200 ? ' (signed out?)' : ''}`);
        }
        if (!r.ok && !json.message) throw new Error(`the server answered ${r.status}`);
        return json;
    }

    function showMessages(lines, isError) {
        const box = el('wbrBudgetMsgs');
        box.className = 'wbr-budget-msgs' + (isError ? ' error' : '');
        box.innerHTML = (lines || []).map(m => `<div>${escHtml(m)}</div>`).join('');
    }

    // Figures shown as typed in the sheet, without the £ and with commas.
    function formatValue(v) {
        if (v === null || v === undefined) return '';
        return Number(v).toLocaleString('en-GB', { maximumFractionDigits: 2 });
    }

    function mapKey(line) {
        return line.map_source && line.map_column ? `${line.map_source}|${line.map_column}` : '';
    }

    // Maps To lists the KPI charts (layout, shownRows, rowTitle from kpi.js):
    // the review's own first, then the rest. On the review, the chart picked
    // draws this line as its target (kpi.js wbrBudgetData).
    function chartGroups() {
        const onReview = shownRows();
        const ids = new Set(onReview.map(r => r.id));
        return [{ name: 'On The Review', rows: onReview },
                { name: 'Other KPI Charts', rows: layout.rows.filter(r => !ids.has(r.id)) }]
            .filter(g => g.rows.length);
    }

    function chartName(row) {
        return rowTitle(row) + (row.subtitle ? ` - ${row.subtitle}` : '');
    }

    function mapSelectHtml(line, groups) {
        const chosen = line.id in mapEdits ? mapEdits[line.id] : mapKey(line);
        let html = '<option value="">Not Mapped</option>';
        let found = chosen === '';
        groups.forEach(g => {
            html += `<optgroup label="${escHtml(g.name)}">`;
            g.rows.forEach(r => {
                const key = `${CHART}|${r.id}`;
                if (key === chosen) found = true;
                html += `<option value="${escHtml(key)}"${key === chosen ? ' selected' : ''}>${escHtml(chartName(r))}</option>`;
            });
            html += '</optgroup>';
        });
        // A chart since deleted, or a data-column mapping from before charts
        // could be picked (the review ignores those), still shows as saved.
        if (!found) {
            const name = chosen.startsWith(CHART + '|') ? 'Chart No Longer There' : `Old Mapping (Not Used): ${chosen.replace('|', ' > ')}`;
            html += `<option value="${escHtml(chosen)}" selected>${escHtml(name)}</option>`;
        }
        return `<select class="wbr-budget-map" data-line="${line.id}">${html}</select>`;
    }

    function render() {
        const box = el('wbrBudgetGrid');
        if (!data) { box.innerHTML = ''; return; }
        // Sections to pick from in Add Line.
        el('wbrBudgetSections').innerHTML = [...new Set(data.lines.map(l => l.section))]
            .map(s => `<option value="${escHtml(s)}">`).join('');
        if (!data.lines.length) {
            box.innerHTML = '<div class="modal-hint">No budget lines yet. Import the CSV to start.</div>';
            return;
        }
        const months = data.months;
        const groups = chartGroups();
        let html = '<table class="wbr-budget-table"><thead><tr><th class="wbr-budget-label">Line</th><th>Maps To (Review Target)</th>'
            + months.map(m => `<th>${escHtml(m)}</th>`).join('') + '<th></th></tr></thead><tbody>';
        let section = null;
        data.lines.forEach(line => {
            if (line.section !== section) {
                section = line.section;
                html += `<tr class="wbr-budget-section"><td class="wbr-budget-label">${escHtml(section)}</td><td colspan="${months.length + 2}"></td></tr>`;
            }
            html += `<tr><td class="wbr-budget-label" title="${escHtml(line.label)}">${escHtml(line.label)}</td>`
                + `<td>${mapSelectHtml(line, groups)}</td>`;
            months.forEach(m => {
                const key = `${line.id}|${m}`;
                const changed = key in edits;
                const shown = changed ? edits[key] : formatValue(line.values[m]);
                html += `<td><input type="text" class="wbr-budget-cell${changed ? ' changed' : ''}" data-key="${key}" value="${escHtml(shown)}"></td>`;
            });
            const mvBtn = (to, icon, title) =>
                `<button type="button" class="btn btn-edit btn-sm" title="${title}" data-move="${to}" data-line="${line.id}"><i class="fas fa-${icon}"></i></button>`;
            html += `<td class="wbr-budget-actions">${mvBtn('top', 'angle-double-up', 'Move to the top of its section')}`
                + `${mvBtn('up', 'angle-up', 'Move up')}${mvBtn('down', 'angle-down', 'Move down')}`
                + `${mvBtn('bottom', 'angle-double-down', 'Move to the bottom of its section')}`
                + `<button type="button" class="btn btn-edit btn-sm" title="Delete this line" data-del="${line.id}"><i class="fas fa-trash"></i></button></td></tr>`;
        });
        box.innerHTML = html + '</tbody></table>';
        status(changeCount() ? `${changeCount()} unsaved change(s)` : '');
    }

    function onInput(e) {
        const t = e.target;
        if (t.classList.contains('wbr-budget-cell')) {
            const [lineId, month] = t.dataset.key.split('|');
            const line = data.lines.find(l => String(l.id) === lineId);
            if (t.value === formatValue(line.values[month])) delete edits[t.dataset.key];
            else edits[t.dataset.key] = t.value;
            t.classList.toggle('changed', t.dataset.key in edits);
        } else if (t.classList.contains('wbr-budget-map')) {
            const line = data.lines.find(l => String(l.id) === t.dataset.line);
            if (t.value === mapKey(line)) delete mapEdits[line.id];
            else mapEdits[line.id] = t.value;
        } else return;
        status(changeCount() ? `${changeCount()} unsaved change(s)` : '');
    }

    // Moves a line within its section; saved straight away, typed figures kept.
    async function moveLine(lineId, to) {
        try {
            const resp = await getJson(`${URL_BASE}/line/${lineId}/move`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ to }),
            });
            if (resp.status !== 'ok') throw new Error(resp.message || 'failed');
            await load();
        } catch (err) {
            showMessages(['Move failed: ' + err.message], true);
        }
    }

    async function onClick(e) {
        const mv = e.target.closest('[data-move]');
        if (mv) return moveLine(mv.dataset.line, mv.dataset.move);
        const btn = e.target.closest('[data-del]');
        if (!btn) return;
        const line = data.lines.find(l => String(l.id) === btn.dataset.del);
        if (!confirm(`Delete the budget line "${line.label}" (${line.section}) and all its figures? This saves straight away.`)) return;
        try {
            const resp = await getJson(`${URL_BASE}/line/${line.id}/delete`, { method: 'POST' });
            if (resp.status !== 'ok') throw new Error(resp.message || 'failed');
            Object.keys(edits).forEach(k => { if (k.startsWith(line.id + '|')) delete edits[k]; });
            delete mapEdits[line.id];
            await load();
        } catch (err) {
            showMessages(['Delete failed: ' + err.message], true);
        }
    }

    // Throws on failure; the status line is cleared either way.
    async function load() {
        status('Loading...');
        try {
            const resp = await getJson(URL_BASE + '/data');
            if (resp.status !== 'ok') throw new Error(resp.message || 'failed');
            data = resp;
            render();
        } finally {
            if (el('wbrBudgetStatus').textContent === 'Loading...') status('');
        }
    }

    // Called each time the Budgets tab is shown. Loads once per popup opening.
    function open() {
        if (loading) return;
        const box = el('wbrBudgetGrid');
        if (!box.dataset.bound) {
            box.addEventListener('input', onInput);
            box.addEventListener('change', onInput);
            box.addEventListener('click', onClick);
            box.dataset.bound = '1';
        }
        loading = load().catch(err => showMessages(['Could not load the budgets: ' + err.message], true));
    }

    // Opening the popup starts again: any unsaved budget edits were cancelled.
    function reset() {
        edits = {};
        mapEdits = {};
        loading = null;
        data = null;
        showMessages([]);
        if (el('wbrBudgetGrid')) render();
    }

    // A line typed in by hand. Saved straight away (with no figures), so the
    // grid is reloaded; figures typed but not saved are kept.
    async function addLine() {
        const body = {
            section: el('wbrBudgetNewSection').value.trim(),
            label: el('wbrBudgetNewLabel').value.trim(),
        };
        if (!body.section || !body.label) return showMessages(['Type a section and a line name first.'], true);
        try {
            const resp = await getJson(URL_BASE + '/line', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (resp.status !== 'ok') throw new Error(resp.message || 'failed');
            el('wbrBudgetNewLabel').value = '';
            await load();
            showMessages([`Added ${body.label} to ${body.section}.`]);
        } catch (err) {
            showMessages(['Add line failed: ' + err.message], true);
        }
    }

    async function importFile(input) {
        const file = input.files[0];
        input.value = '';
        if (!file) return;
        if (changeCount() && !confirm('Importing reloads the grid and drops the changes not yet saved. Carry on?')) return;
        const form = new FormData();
        form.append('file', file);
        status('Importing...');
        let resp;
        try {
            resp = await getJson(URL_BASE + '/import', { method: 'POST', body: form });
        } catch (err) {
            status('');
            return showMessages(['Import failed: ' + err.message], true);
        }
        status('');
        if (resp.status !== 'ok') return showMessages([resp.message || 'Import failed'].concat(resp.errors || []), true);
        edits = {};
        mapEdits = {};
        try {
            await load();
        } catch (err) {
            return showMessages([`Imported ${resp.lines} lines, but could not show them: ${err.message}`], true);
        }
        showMessages([`Imported ${resp.lines} lines (${resp.cells} figures) from ${file.name}.`].concat(resp.errors || []),
            (resp.errors || []).length > 0);
    }

    // Called by the popup's Save. Throws so the popup shows the error and stays
    // open. True when the budgets may have changed: something saved here, or
    // the tab was opened (Import, Add, Move and Delete save straight away).
    async function save() {
        if (!changeCount()) return data !== null;
        const values = Object.entries(edits).map(([key, value]) => {
            const [line_id, month] = key.split('|');
            return { line_id: Number(line_id), month, value };
        });
        const maps = Object.entries(mapEdits).map(([line_id, key]) => {
            const i = key.indexOf('|');
            return { line_id: Number(line_id), source: i < 0 ? '' : key.slice(0, i), column: i < 0 ? '' : key.slice(i + 1) };
        });
        const resp = await getJson(URL_BASE + '/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ values, maps }),
        });
        if (resp.status !== 'ok') throw new Error('Budgets: ' + (resp.message || 'save failed'));
        edits = {};
        mapEdits = {};
        return true;
    }

    return { open, reset, importFile, addLine, save };
})();
