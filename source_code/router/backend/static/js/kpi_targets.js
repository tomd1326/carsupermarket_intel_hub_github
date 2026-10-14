// KPI page targets: per-date targets pasted from Excel, the Settings > Targets
// tab, and its per-date popup. Loaded by kpi.html before its own script; the
// functions here use that script's page state (layout, metricsByKey,
// metricNames, BASE_URL, ...) and are only called once the page has loaded.
//
// Where a chart's target comes from (row.target_mode):
//   none       no target
//   fixed      one figure. A plain chart's belongs to the metric
//              (kpi:fixed:<source>:<metric>); a ratio or added-metrics chart's
//              is on the row (ratio_target)
//   import     per-date figures. A plain chart's belong to the metric
//              (kpi:import:<source>:<metric>, every chart on it shares them); a
//              ratio or added-metrics chart's are its own (kpi:row:<row id>)
//   snowflake  the metric's <METRIC>_TARGET column (plain charts only)
//   inputs     ratio and added-metrics charts only: each metric's own target
//              (source picked per metric in row.input_targets) added and
//              divided the same way as the figures
//   calc       plain line charts only: two other targets combined, times or
//              divided by (row.target_calc, see withCalcTarget)

const TARGET_SOURCE_LABELS = {
    none: 'No Target', fixed: 'Fixed Figure', import: 'Per Date',
    snowflake: 'Snowflake Target Column', inputs: 'From Inputs', calc: 'Calculated',
};
const CALC_OP_LABELS = { times: 'times', divide: 'divided by' };
const INPUT_TARGET_LABELS = { import: 'Per Date', fixed: 'Fixed Figure', snowflake: 'Snowflake Target Column' };

// ── Reading pasted figures ──

// YYYY-MM-DD, or DD/MM/YYYY as Excel copies a UK date. Returns YYYY-MM-DD, or
// null when the text is not a real date.
function parsePasteDate(text) {
    let y, m, d;
    let match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text);
    if (match) { [, y, m, d] = match.map(Number); }
    else {
        match = /^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/.exec(text);
        if (!match) return null;
        [, d, m, y] = match.map(Number);
        if (y < 100) y += 2000;
    }
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
    return dt.toISOString().slice(0, 10);
}

function parsePasteNumber(text) {
    const cleaned = String(text).replace(/[£,%\s]/g, '');
    if (cleaned === '') return null;
    const n = Number(cleaned);
    return isNaN(n) ? null : n;
}

// Two columns copied from Excel: date, then target. Excel puts a tab between
// them; a comma works too. A first line that is not a date (a header) is
// skipped, and so is a date with a blank target.
// pct: the chart shows its figures as 0-100 % (format "Percent", every ratio
// chart shown as %). There "12%" is 12 and a plain figure is the decimal
// Excel holds under its % format, so 0.12 is 12. Elsewhere "12%" is 0.12 and
// a plain figure is kept as it is. Returns [{date, value}] oldest first.
function parsePastedTargets(text, pct) {
    const rows = [];
    const seen = new Set();
    const lines = text.split(/\r?\n/);
    const firstLine = lines.findIndex(l => l.trim());
    lines.forEach((line, index) => {
        if (!line.trim()) return;
        const cells = (line.includes('\t') ? line.split('\t') : line.split(',')).map(c => c.trim());
        const date = parsePasteDate(cells[0] || '');
        if (!date) {
            if (index === firstLine) return;
            throw new Error(`Line ${index + 1}: cannot read the date "${cells[0]}"`);
        }
        const raw = cells[1] === undefined ? '' : cells[1];
        if (raw === '') return;
        const n = parsePasteNumber(raw);
        if (n === null) throw new Error(`Line ${index + 1}: cannot read the target "${raw}"`);
        if (seen.has(date)) throw new Error(`Line ${index + 1}: ${date} is in the list twice`);
        seen.add(date);
        const isPct = raw.includes('%');
        const value = pct ? (isPct ? n : n * 100) : (isPct ? n / 100 : n);
        rows.push({ date, value: Math.round(value * 1e8) / 1e8 });
    });
    return rows.sort((a, b) => (a.date < b.date ? -1 : 1));
}

// Stored rows ([[date, value]]) as box text, in the same form a paste is read.
function formatPastedTargets(rows, pct) {
    return rows.map(([date, value]) => `${date}\t${+Number(value).toFixed(6)}${pct ? '%' : ''}`).join('\n');
}

// The line under a paste box: what was read, or the first problem.
function showPasteCheck(inputId, checkId, pct) {
    const box = document.getElementById(checkId);
    const text = document.getElementById(inputId).value;
    if (!text.trim()) { box.textContent = 'Empty - saving removes every per-date target here.'; return; }
    try {
        const rows = parsePastedTargets(text, pct);
        if (!rows.length) { box.textContent = 'No figures found.'; return; }
        const first = rows[0];
        box.textContent = `${rows.length} dates, ${first.date} to ${rows[rows.length - 1].date}. `
            + `First figure read as ${+first.value.toFixed(6)}${pct ? '%' : ''}.`;
    } catch (err) {
        box.textContent = err.message;
    }
}

// The figure when every pasted date has the same one (and there is more than
// one date), else null. Such a list is saved as a fixed figure instead of a
// row per date. A fixed figure covers every date, not just the ones pasted.
function sameFigure(rows) {
    return rows.length > 1 && rows.every(r => r.value === rows[0].value) ? rows[0].value : null;
}

// Layout rows with every chart that draws the metric's per-date figures
// switched to its fixed figure, From Inputs charts included.
function switchMetricToFixed(rows, source, metric) {
    const key = metricKey(source, metric);
    const toFixed = ref => (ref && ref.kind === 'metric' && (ref.source || 'snowflake') === source
                            && ref.metric === metric && ref.mode === 'import' ? { ...ref, mode: 'fixed' } : ref);
    return rows.map(r => {
        if (!rowUsesMetricTarget(r, source, metric, 'import')) return r;
        if (r.target_mode === 'calc') return { ...r, target_calc: { ...r.target_calc, a: toFixed(r.target_calc.a), b: toFixed(r.target_calc.b) } };
        if (rowOwnTarget(r)) return { ...r, input_targets: { ...rowInputTargets(r), [key]: 'fixed' } };
        return { ...r, target_mode: 'fixed' };
    });
}

async function saveMetricFixed(source, metric, value) {
    const resp = await fetch(`${BASE_URL}/kpi/metric-target/fixed`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, metric, value }),
    }).then(r => r.json());
    if (resp.status !== 'ok') throw new Error(resp.message || 'target save failed');
}

async function fetchTargetDates(params) {
    const resp = await fetch(`${BASE_URL}/kpi/target-dates?${new URLSearchParams(params)}`).then(r => r.json());
    if (resp.status !== 'ok') throw new Error(resp.message || 'could not read the targets');
    return resp.rows;
}

async function saveTargetDates(params, rows) {
    const resp = await fetch(`${BASE_URL}/kpi/target-dates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...params, rows }),
    }).then(r => r.json());
    if (resp.status !== 'ok') throw new Error(resp.message || 'save failed');
}

// ── Which charts use which targets ──

// The metrics a ratio or added-metrics chart is built from: [{source, metric}].
function rowInputParts(row) {
    return [{ source: rowSource(row), metric: row.metric }, ...rowPlus(row),
            ...(row.divide_by ? [row.divide_by, ...rowDividePlus(row)] : [])];
}

// {"source|metric": target source} for each of those metrics. Per Date
// unless another source was picked for it.
function rowInputTargets(row) {
    const set = row.input_targets || {};
    return Object.fromEntries(rowInputParts(row).map(p => {
        const key = metricKey(p.source || 'snowflake', p.metric);
        return [key, set[key] || 'import'];
    }));
}

// The metric targets a calculated target is built from: [{source, metric, mode}].
function rowCalcMetricRefs(row) {
    const calc = row.target_mode === 'calc' && row.target_calc;
    return calc ? [calc.a, calc.b].filter(r => r && r.kind === 'metric') : [];
}

// Does this chart draw the given metric's stored fixed / per-date figures?
function rowUsesMetricTarget(row, source, metric, mode) {
    if (isStacked(row)) return false;
    if (rowCalcMetricRefs(row).some(r => (r.source || 'snowflake') === source && r.metric === metric && r.mode === mode)) {
        return true;
    }
    if (rowOwnTarget(row)) {
        return row.target_mode === 'inputs' && rowInputTargets(row)[metricKey(source, metric)] === mode;
    }
    return rowSource(row) === source && row.metric === metric && rowTargetMode(row) === mode;
}

// Figures are shared per metric, so every chart that draws them needs
// reloading after a change, ratio charts built from that metric included.
function refreshRowsOnMetric(source, metric, modes) {
    shownRows()
        .filter(r => modes.some(mode => rowUsesMetricTarget(r, source, metric, mode)))
        .forEach(r => loadCustomRow(r));
}

// Figures shown as 0-100 % on this chart (see parsePastedTargets).
function rowIsPct(row) {
    return row.fmt === 'pct';
}

function metricIsPct(source, metric) {
    return layout.rows.some(r => !isStacked(r) && !rowOwnTarget(r) && rowSource(r) === source
                                 && r.metric === metric && rowIsPct(r));
}

// ── A ratio or added-metrics chart's own per-date targets on its periods ──

function addIsoDays(iso, n) {
    const t = new Date(iso + 'T00:00:00Z');
    t.setUTCDate(t.getUTCDate() + n);
    return t.toISOString().slice(0, 10);
}

// The chart's day, week and month targets from its per-date figures. A ratio
// is a rate, so a week or month is the average of its days' figures. An
// added-metrics Sum chart's week or month is the average day times the
// period's full length, as the server does for metric targets, so a part-
// covered period does not dive. A period with no figures has no target.
function rollUpRowTargets(row, rows, d) {
    const byDate = Object.fromEntries(rows);
    const sum = !row.divide_by && row.agg === 'sum';
    const period = days => {
        const vals = days.map(x => byDate[x]).filter(v => v !== undefined);
        if (!vals.length) return null;
        const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
        return sum ? mean * days.length : mean;
    };
    const weekDays = start => [0, 1, 2, 3, 4, 5, 6].map(i => addIsoDays(start, i));
    const monthDays = start => {
        const days = [];
        for (let x = start; x.slice(0, 7) === start.slice(0, 7); x = addIsoDays(x, 1)) days.push(x);
        return days;
    };
    return {
        ...d,
        values_target: (d.dates || []).map(x => byDate[x] ?? null),
        weekly_values_target: (d.weekly_starts || []).map(s => period(weekDays(s))),
        monthly_values_target: (d.monthly_starts || []).map(s => period(monthDays(s))),
    };
}

// d with the chart's own per-date targets drawn on it. A failed read leaves
// the chart without a target rather than without its figures.
async function withRowDateTargets(row, d) {
    if (d.status !== 'ok') return d;
    try {
        return rollUpRowTargets(row, await fetchTargetDates({ row_id: row.id }), d);
    } catch (err) {
        document.getElementById('statusText').textContent = `${rowTitle(row)}: targets not shown - ${err.message}`;
        return d;
    }
}

// ── Calculated target: two other targets combined ──
// A % is used as a decimal throughout: a target on a chart showing 0-100 %
// is divided by 100 first, and the result is multiplied back by 100 when
// this chart shows 0-100 %. So "enquiries x 12%" gives orders, and "revenue
// / orders" gives an average price, on any chart format.

// One side's targets as plain numbers: {values_target, weekly_values_target,
// monthly_values_target}, or {constant} for a typed figure.
async function calcRefTargets(ref, row) {
    if (ref.kind === 'value') return { constant: ref.value };
    let d, scale;
    if (ref.kind === 'metric') {
        const source = ref.source || 'snowflake';
        // Fetched with the metric's own usual Sum / Average, so a revenue
        // target adds up over a month while an average stays an average.
        const info = metricsByKey[metricKey(source, ref.metric)] || {};
        d = await fetchMetricData(source, ref.metric, info.suggested_agg || 'sum', ref.mode, rowCurrent(row));
        if (d.status !== 'ok') throw new Error(`${ref.metric}: ${d.message}`);
        scale = metricIsPct(source, ref.metric) ? 0.01 : 1;
    } else {
        const other = findRow(ref.row_id);
        if (!other) throw new Error('the chart it uses has been removed');
        if (rowTargetMode(other) === 'calc') throw new Error(`${rowTitle(other)} has a calculated target itself`);
        d = await fetchRowData(other);
        if (d.status !== 'ok') throw new Error(`${rowTitle(other)}: ${d.message}`);
        scale = rowIsPct(other) ? 0.01 : 1;
    }
    const scaled = list => (list || []).map(v => (v === null || v === undefined ? null : v * scale));
    return {
        values_target: scaled(d.values_target),
        weekly_values_target: scaled(d.weekly_values_target),
        monthly_values_target: scaled(d.monthly_values_target),
    };
}

// d with its target worked out from row.target_calc, period by period: a
// week's target is the week's A target times (or divided by) the week's B
// target. A failed side leaves the chart without a target, not without its figures.
async function withCalcTarget(row, d) {
    const calc = row.target_calc;
    if (d.status !== 'ok' || !calc) return d;
    try {
        const [a, b] = await Promise.all([calcRefTargets(calc.a, row), calcRefTargets(calc.b, row)]);
        const out = rowIsPct(row) ? 100 : 1;
        const at = (side, key, i) => ('constant' in side ? side.constant : (side[key] || [])[i]);
        const combine = (key, labels) => (labels || []).map((_, i) => {
            const x = at(a, key, i), y = at(b, key, i);
            if (x === null || x === undefined || y === null || y === undefined) return null;
            if (calc.op === 'divide') return y === 0 ? null : (x / y) * out;
            return x * y * out;
        });
        return {
            ...d,
            values_target: combine('values_target', d.dates),
            weekly_values_target: combine('weekly_values_target', d.weekly_labels),
            monthly_values_target: combine('monthly_values_target', d.monthly_labels),
        };
    } catch (err) {
        document.getElementById('statusText').textContent = `${rowTitle(row)}: target not shown - ${err.message}`;
        return d;
    }
}

function calcRefText(ref) {
    if (!ref) return '?';
    if (ref.kind === 'value') return String(ref.value);
    if (ref.kind === 'chart') {
        const other = findRow(ref.row_id);
        return other ? `${rowTitle(other)} (chart target)` : 'removed chart';
    }
    return `${metricName(ref.source || 'snowflake', ref.metric)} (${INPUT_TARGET_LABELS[ref.mode] || ref.mode})`;
}

// The Calculated From lists: every metric target that exists, every chart
// with a target of its own kind, and a typed figure. Read from Settings >
// Targets' list of stored targets, so only targets that hold figures are offered.
async function loadCalcOptions() {
    const s = chartModalState;
    s.calcLoaded = true;
    ['calcA', 'calcB'].forEach(id => { document.getElementById(id).innerHTML = '<option value="">Loading...</option>'; });
    let stored = [];
    try {
        const resp = await fetch(`${BASE_URL}/kpi/targets/overview`).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'load failed');
        stored = resp.targets || [];
    } catch (err) {
        if (chartModalState === s) showChartError('Could not load the targets to calculate from: ' + err.message);
    }
    if (chartModalState !== s) return;
    const metricOpts = [];
    metricsCatalogue.forEach(m => {
        const modes = [];
        if (stored.some(t => t.kind === 'dates' && t.source === m.source && t.metric === m.column)) modes.push('import');
        if (stored.some(t => t.kind === 'fixed' && t.source === m.source && t.metric === m.column)) modes.push('fixed');
        if (m.has_target) modes.push('snowflake');
        modes.forEach(mode => metricOpts.push({ kind: 'metric', source: m.source, metric: m.column, mode }));
    });
    const chartOpts = layout.rows
        .filter(r => !isStacked(r) && r.id !== s.rowId && !['none', 'calc'].includes(rowTargetMode(r)))
        .map(r => ({ kind: 'chart', row_id: r.id }));
    const fill = (id, chosen) => {
        const key = ref => JSON.stringify(ref && ref.kind === 'value' ? { kind: 'value' } : ref);
        const metrics = [...metricOpts];
        const charts = [...chartOpts];
        // A saved choice whose figures have gone is still shown, so it is not lost silently.
        if (chosen && chosen.kind === 'metric' && !metrics.some(o => key(o) === key(chosen))) metrics.unshift(chosen);
        if (chosen && chosen.kind === 'chart' && !charts.some(o => key(o) === key(chosen))) charts.unshift(chosen);
        const opt = ref => `<option value="${escHtml(key(ref))}"${chosen && key(chosen) === key(ref) ? ' selected' : ''}>${escHtml(calcRefText(ref))}</option>`;
        document.getElementById(id).innerHTML = '<option value="">Choose a target</option>'
            + `<optgroup label="Metric Targets">${metrics.map(opt).join('')}</optgroup>`
            + `<optgroup label="Chart Targets">${charts.map(opt).join('')}</optgroup>`
            + `<optgroup label="Other"><option value="${escHtml(key({ kind: 'value' }))}"${chosen && chosen.kind === 'value' ? ' selected' : ''}>Typed Figure</option></optgroup>`;
    };
    fill('calcA', s.calc && s.calc.a);
    fill('calcB', s.calc && s.calc.b);
    document.getElementById('calcOp').value = (s.calc && s.calc.op) || 'times';
    document.getElementById('calcAValue').value = s.calc && s.calc.a && s.calc.a.kind === 'value' ? s.calc.a.value : '';
    document.getElementById('calcBValue').value = s.calc && s.calc.b && s.calc.b.kind === 'value' ? s.calc.b.value : '';
    updateCalcValueInputs();
}

// The figure box shows only beside a side set to Typed Figure.
function updateCalcValueInputs() {
    [['calcA', 'calcAValue'], ['calcB', 'calcBValue']].forEach(([selectId, inputId]) => {
        const v = document.getElementById(selectId).value;
        document.getElementById(inputId).style.display = v && JSON.parse(v).kind === 'value' ? '' : 'none';
    });
}

// The calculated target from the popup; throws with what is missing.
function readCalc() {
    const side = (selectId, inputId, name) => {
        const v = document.getElementById(selectId).value;
        if (!v) throw new Error(`Choose the ${name} target for the calculated target.`);
        const ref = JSON.parse(v);
        if (ref.kind === 'value') {
            const n = parseFloat(document.getElementById(inputId).value);
            if (isNaN(n)) throw new Error(`Type the ${name} figure for the calculated target.`);
            return { kind: 'value', value: n };
        }
        return ref;
    };
    return { a: side('calcA', 'calcAValue', 'first'), op: document.getElementById('calcOp').value,
             b: side('calcB', 'calcBValue', 'second') };
}

// ── Edit Chart popup: per-date box and From Inputs list ──

// Is the popup's box in 0-100 % units? Follows the Format chosen in the popup.
function chartPastePct() {
    return document.getElementById('fmtSelect').value === 'pct';
}

// The box holds the metric's list on a plain chart and the chart's own list
// on a ratio or added-metrics chart. Switching between the two keeps what was
// typed in each.
function showChartDates(kind) {
    const s = chartModalState;
    if (!s) return;
    const input = document.getElementById('chartDatesInput');
    // Only swap the text when the list changes, so what was typed stays put
    // while other settings in the popup change.
    if (s.datesKind !== kind) {
        if (s.datesKind) s.dates[s.datesKind] = input.value;
        s.datesKind = kind;
        input.value = s.dates[kind] || '';
        input.placeholder = s.loading[kind] ? 'Loading...' : '';
    }
    document.getElementById('chartDatesHint').textContent = kind === 'row'
        ? 'This chart\'s own figure for each date, in its units (12% or 0.12 for 12% on a % chart). '
          + 'Paste two columns from Excel: date, target. Weeks and months show the average.'
        : 'Paste two columns from Excel: date, target. They belong to the metric, so every chart on it '
          + 'shares them. Taking a date out of the box removes it. Saved with the chart.';
    showPasteCheck('chartDatesInput', 'chartDatesCheck', chartPastePct());
}

// Loads one list into the popup, unless the popup has moved on since.
async function loadChartDates(kind, params) {
    const s = chartModalState;
    const input = document.getElementById('chartDatesInput');
    s.dates[kind] = s.datesOrig[kind] = '';
    s.loading[kind] = true;
    if (s.datesKind === kind) { input.value = ''; input.placeholder = 'Loading...'; }
    try {
        const rows = await fetchTargetDates(params);
        if (chartModalState !== s) return;
        if (kind === 'metric' && s.metricKey !== metricKey(params.source, params.metric)) return;
        s.dates[kind] = s.datesOrig[kind] = formatPastedTargets(rows, chartPastePct());
    } catch (err) {
        if (chartModalState === s) showChartError('Could not load the per-date targets: ' + err.message);
    }
    s.loading[kind] = false;
    if (chartModalState === s && s.datesKind === kind) {
        input.value = s.dates[kind];
        input.placeholder = '';
        showPasteCheck('chartDatesInput', 'chartDatesCheck', chartPastePct());
    }
}

// Shows the per-date box, the From Inputs list and the right radio options
// for the chosen target source.
function updateTargetFields() {
    if (!chartModalState) return;
    const show = (id, on) => { document.getElementById(id).style.display = on ? '' : 'none'; };
    if (chartTypeValue() === 'stacked') {
        show('importField', false);
        show('inputTargetsField', false);
        return;
    }
    const own = selectedOwnTarget();
    const mode = radioValue('targetMode', 'none');
    show('targetModeInputs', own);
    show('importField', mode === 'import');
    show('inputTargetsField', own && mode === 'inputs');
    show('calcField', !own && mode === 'calc');
    showChartDates(own ? 'row' : 'metric');
    if (own && mode === 'inputs') renderInputTargets();
    if (!own && mode === 'calc' && !chartModalState.calcLoaded) loadCalcOptions();
    updateFixedUnit();
}

// The fixed target box's unit: "%" on a chart showing 0-100 %, a reminder
// on one showing a decimal as %.
function updateFixedUnit() {
    const unit = document.getElementById('fixedTargetUnit');
    if (!unit) return;
    const fmt = document.getElementById('fmtSelect').value;
    unit.textContent = chartTypeValue() === 'stacked' ? '' : fmt === 'pct' ? '%' : fmt === 'pct100' ? 'as a decimal (0.12 = 12%)' : '';
}

// One line per metric in the chart, with where its target comes from.
function renderInputTargets() {
    const s = chartModalState;
    const m = selectedMetric() || {};
    const divide = selectedDivide();
    const parts = [{ source: m.source, metric: m.column }, ...selectedPlus('metric'),
                   ...(divide ? [divide, ...selectedPlus('divide')] : [])].filter(p => p.metric);
    document.getElementById('inputTargetsList').innerHTML = parts.map(p => {
        const key = metricKey(p.source || 'snowflake', p.metric);
        const chosen = s.inputTargets[key] || 'import';
        const info = metricsByKey[key] || {};
        const options = Object.entries(INPUT_TARGET_LABELS)
            .filter(([mode]) => mode !== 'snowflake' || info.has_target || chosen === 'snowflake')
            .map(([mode, label]) => `<option value="${mode}"${mode === chosen ? ' selected' : ''}>${label}</option>`).join('');
        return `<div class="input-target-row"><span title="${escHtml(p.metric)}">${escHtml(metricName(p.source, p.metric))}</span>
            <select data-key="${escHtml(key)}" onchange="chartModalState.inputTargets[this.dataset.key] = this.value">${options}</select></div>`;
    }).join('');
}

// The From Inputs choices for the metrics now in the chart.
function readInputTargets(row) {
    return Object.fromEntries(rowInputParts(row).map(p => {
        const key = metricKey(p.source || 'snowflake', p.metric);
        return [key, chartModalState.inputTargets[key] || 'import'];
    }));
}

// Checks the box before anything is saved. Returns {changed, rows}; throws
// with the problem.
function readChartDates(mode) {
    const s = chartModalState;
    const kind = s.datesKind;
    // Saving before the stored list has arrived would replace it with nothing.
    if (s.loading[kind]) throw new Error('The per-date targets are still loading - try again in a moment.');
    const text = document.getElementById('chartDatesInput').value;
    const rows = parsePastedTargets(text, chartPastePct());
    if (mode === 'import' && !rows.length) throw new Error('Paste the per-date targets, or choose another target option.');
    return { kind, rows, changed: text.trim() !== (s.datesOrig[kind] || '').trim() };
}

// ── Settings > Targets tab ──

let targetsOverview = [];

async function loadTargetsTab() {
    const box = document.getElementById('targetsList');
    box.textContent = 'Loading...';
    try {
        const resp = await fetch(`${BASE_URL}/kpi/targets/overview`).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'load failed');
        targetsOverview = resp.targets || [];
        renderTargetsTab();
    } catch (err) {
        box.textContent = 'Could not load the targets: ' + err.message;
    }
}

function storedTarget(kind, source, metric) {
    return targetsOverview.find(t => t.kind === kind && t.source === source && t.metric === metric) || null;
}

function storedRowTarget(rowId) {
    return targetsOverview.find(t => t.kind === 'row' && t.row_id === rowId) || null;
}

function datesText(t) {
    return t ? `${t.count} dates, ${t.first} to ${t.last}` : '';
}

function fmtTargetFigure(value, pct) {
    return value === null || value === undefined ? '' : `${+Number(value).toFixed(4)}${pct ? '%' : ''}`;
}

// Warning text for a target source with nothing stored behind it.
const NOTHING_SET = '<span class="target-warn">Nothing set</span>';

function viewButton(onclick) {
    return `<button type="button" class="btn btn-edit btn-sm" onclick="${onclick}">View</button>`;
}

// What one chart's target is, for the Charts table: [details html, view button html].
function chartTargetDetails(row) {
    const mode = isStacked(row) ? (row.target_mode === 'fixed' ? 'fixed' : 'none') : rowTargetMode(row);
    if (mode === 'none') return ['', ''];
    if (isStacked(row)) return [fmtTargetFigure(row.stack_target) || NOTHING_SET, ''];
    const id = escHtml(JSON.stringify(row.id));
    if (rowOwnTarget(row)) {
        if (mode === 'fixed') return [fmtTargetFigure(row.ratio_target, rowIsPct(row)) || NOTHING_SET, ''];
        if (mode === 'import') {
            const t = storedRowTarget(row.id);
            return [t ? datesText(t) : NOTHING_SET, viewButton(`openRowTargetDates(${id})`)];
        }
        const inputs = rowInputTargets(row);
        return [rowInputParts(row).map(p => {
            const source = p.source || 'snowflake';
            const mode = inputs[metricKey(source, p.metric)];
            let what;
            if (mode === 'snowflake') what = 'Snowflake target column';
            else if (mode === 'fixed') what = fmtTargetFigure((storedTarget('fixed', source, p.metric) || {}).value) || NOTHING_SET;
            else what = datesText(storedTarget('dates', source, p.metric)) || NOTHING_SET;
            return `${escHtml(metricName(source, p.metric))}: ${INPUT_TARGET_LABELS[mode]} - ${what}`;
        }).join('<br>'), ''];
    }
    const source = rowSource(row);
    if (mode === 'calc') {
        const calc = row.target_calc || {};
        return [escHtml(`${calcRefText(calc.a)} ${CALC_OP_LABELS[calc.op] || ''} ${calcRefText(calc.b)}`), ''];
    }
    if (mode === 'snowflake') return [escHtml(`${row.metric}_TARGET`), ''];
    if (mode === 'fixed') {
        return [fmtTargetFigure((storedTarget('fixed', source, row.metric) || {}).value, rowIsPct(row)) || NOTHING_SET, ''];
    }
    const t = storedTarget('dates', source, row.metric);
    return [t ? datesText(t) : NOTHING_SET,
            viewButton(`openMetricTargetDates(${escHtml(JSON.stringify(source))}, ${escHtml(JSON.stringify(row.metric))})`)];
}

function chartFormula(row) {
    const name = p => metricName(p.source, p.metric);
    if (isStacked(row)) return (row.series || []).map(seriesLabel).join(', ');
    return formulaText([name({ source: rowSource(row), metric: row.metric }), ...rowPlus(row).map(name)],
        row.divide_by ? [name(row.divide_by), ...rowDividePlus(row).map(name)] : []);
}

function renderTargetsTab() {
    const chartRows = layout.rows.map(row => {
        const mode = isStacked(row) ? (row.target_mode === 'fixed' ? 'fixed' : 'none') : rowTargetMode(row);
        const [details, button] = chartTargetDetails(row);
        return `<tr><td>${escHtml(rowTitle(row))}${row.hidden ? ' (hidden)' : ''}<br><small>${escHtml(row.section)}</small></td>
            <td>${escHtml(chartFormula(row))}</td><td>${TARGET_SOURCE_LABELS[mode] || mode}</td>
            <td>${details}</td><td>${button}</td></tr>`;
    }).join('');

    // Every stored figure, with the charts drawing it.
    const storedRows = targetsOverview.map(t => {
        let belongs, type, figure, users, view = '', remove;
        if (t.kind === 'row') {
            const row = findRow(t.row_id);
            belongs = row ? `Chart: ${escHtml(rowTitle(row))}` : '<span class="target-warn">Removed chart</span>';
            type = 'Per Date (Chart)';
            figure = datesText(t);
            users = row && row.target_mode === 'import' ? [row] : [];
            const id = escHtml(JSON.stringify(t.row_id));
            view = viewButton(`openRowTargetDates(${id})`);
            remove = `removeStoredTarget('row', ${id})`;
        } else {
            const source = escHtml(JSON.stringify(t.source)), metric = escHtml(JSON.stringify(t.metric));
            belongs = escHtml(metricOptionText(t.source, t.metric));
            if (t.kind === 'fixed') {
                type = 'Fixed Figure';
                figure = fmtTargetFigure(t.value, metricIsPct(t.source, t.metric));
                users = layout.rows.filter(r => rowUsesMetricTarget(r, t.source, t.metric, 'fixed'));
                remove = `removeStoredTarget('fixed', ${source}, ${metric})`;
            } else {
                type = 'Per Date';
                figure = datesText(t);
                users = layout.rows.filter(r => rowUsesMetricTarget(r, t.source, t.metric, 'import'));
                view = viewButton(`openMetricTargetDates(${source}, ${metric})`);
                remove = `removeStoredTarget('dates', ${source}, ${metric})`;
            }
        }
        const used = users.length ? users.map(r => escHtml(rowTitle(r))).join(', ')
                                  : '<span class="target-warn">Not used by any chart</span>';
        return `<tr><td>${belongs}</td><td>${type}</td><td>${figure}</td><td>${used}</td>
            <td>${view} <button type="button" class="btn btn-edit btn-sm" onclick="${remove}">Remove</button></td></tr>`;
    }).join('');

    document.getElementById('targetsList').innerHTML = `
        <div class="modal-section-title">Charts</div>
        <table class="vehicles-table targets-table"><thead><tr><th>Chart</th><th>Metric</th><th>Target Source</th>
            <th>Target</th><th></th></tr></thead><tbody>${chartRows}</tbody></table>
        <div class="modal-section-title">Stored Targets</div>
        ${storedRows ? `<table class="vehicles-table targets-table"><thead><tr><th>Belongs To</th><th>Type</th>
            <th>Figure</th><th>Charts Using It</th><th></th></tr></thead><tbody>${storedRows}</tbody></table>`
                     : '<p class="modal-hint">No targets stored yet.</p>'}`;
}

async function removeStoredTarget(kind, a, b) {
    const what = kind === 'row' ? 'this chart\'s per-date targets'
        : `the ${kind === 'fixed' ? 'fixed figure' : 'per-date targets'} for ${metricName(a, b)}`;
    if (!confirm(`Remove ${what}? Every chart using them loses that target.`)) return;
    try {
        if (kind === 'fixed') {
            const resp = await fetch(`${BASE_URL}/kpi/metric-target/fixed`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: a, metric: b, value: null }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'remove failed');
            refreshRowsOnMetric(a, b, ['fixed']);
        } else if (kind === 'row') {
            await saveTargetDates({ row_id: a }, []);
            const row = findRow(a);
            if (row && !row.hidden) loadCustomRow(row);
        } else {
            await saveTargetDates({ source: a, metric: b }, []);
            refreshRowsOnMetric(a, b, ['import']);
        }
        loadTargetsTab();
    } catch (err) {
        alert('Remove failed: ' + err.message);
    }
}

// ── Per-date popup opened from the Targets tab ──

let targetDatesState = null;   // { params, pct, after }

function openMetricTargetDates(source, metric) {
    openTargetDatesModal({ source, metric }, metricOptionText(source, metric),
        'Belongs to the metric, so every chart on it shares them.', metricIsPct(source, metric),
        () => refreshRowsOnMetric(source, metric, ['import']));
}

function openRowTargetDates(rowId) {
    const row = findRow(rowId);
    openTargetDatesModal({ row_id: rowId }, row ? rowTitle(row) : 'Removed chart',
        'This chart\'s own figure for each date, in its units.', !!row && rowIsPct(row),
        () => { if (row && !row.hidden) loadCustomRow(row); });
}

async function openTargetDatesModal(params, title, sub, pct, after) {
    const state = { params, pct, after };
    targetDatesState = state;
    const input = document.getElementById('targetDatesInput');
    document.getElementById('targetDatesTitle').textContent = title;
    document.getElementById('targetDatesSub').textContent =
        `${sub} Paste two columns from Excel: date, target. Taking a date out of the box removes it.`;
    document.getElementById('targetDatesError').classList.remove('show');
    document.getElementById('targetDatesCheck').textContent = '';
    input.value = 'Loading...';
    input.disabled = true;
    document.getElementById('targetDatesSaveBtn').disabled = true;
    document.getElementById('targetDatesModal').classList.add('open');
    try {
        const rows = await fetchTargetDates(params);
        if (targetDatesState !== state) return;
        input.value = formatPastedTargets(rows, pct);
        input.disabled = false;
        document.getElementById('targetDatesSaveBtn').disabled = false;
        showPasteCheck('targetDatesInput', 'targetDatesCheck', pct);
    } catch (err) {
        if (targetDatesState === state) showTargetDatesError('Could not load the targets: ' + err.message);
    }
}

function closeTargetDatesModal() {
    targetDatesState = null;
    document.getElementById('targetDatesModal').classList.remove('open');
}

function showTargetDatesError(message) {
    const box = document.getElementById('targetDatesError');
    box.textContent = message;
    box.classList.add('show');
}

async function saveTargetDatesModal() {
    const state = targetDatesState;
    if (!state) return;
    const button = document.getElementById('targetDatesSaveBtn');
    document.getElementById('targetDatesError').classList.remove('show');
    let rows;
    try { rows = parsePastedTargets(document.getElementById('targetDatesInput').value, state.pct); }
    catch (err) { showTargetDatesError(err.message); return; }
    button.disabled = true;
    try {
        const same = sameFigure(rows);
        if (same === null) {
            await saveTargetDates(state.params, rows);
            closeTargetDatesModal();
            state.after();
        } else {
            // One figure for every date: saved as a fixed figure, the per-date
            // list cleared, and the charts using it switched to Fixed.
            const { source, metric, row_id: rowId } = state.params;
            let next;
            if (rowId) {
                next = layout.rows.map(r => (r.id === rowId ? { ...r, target_mode: 'fixed', ratio_target: same } : r));
            } else {
                await saveMetricFixed(source, metric, same);
                next = switchMetricToFixed(layout.rows, source, metric);
            }
            await saveLayout(next);
            await saveTargetDates(state.params, []);
            layout.rows = next;
            closeTargetDatesModal();
            if (rowId) { const row = findRow(rowId); if (row && !row.hidden) loadCustomRow(row); }
            else refreshRowsOnMetric(source, metric, ['fixed']);
            document.getElementById('statusText').textContent =
                'Every date had the same figure, so it was saved as a fixed figure.';
        }
        loadTargetsTab();
    } catch (err) {
        showTargetDatesError('Save failed. ' + err.message);
    } finally {
        button.disabled = false;
    }
}
