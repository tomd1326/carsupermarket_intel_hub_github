// Weekly Business Review > Report view. Loaded before kpi.js on the review page
// only; everything it calls from kpi.js (escHtml, BASE_URL ...) is only used once
// the page has loaded. wbr_deck.js decides which view shows and calls setVisible.
//
// The server builds every section (routers/wbr_report_sections.py) and sends raw
// numbers; this formats and draws them. A section not built yet says so.
const WbrReport = (() => {
    let visible = false;
    let report = null;        // the last response from the server
    let error = '';
    let loading = false;
    let current = 0;          // index of the section showing
    let reviewDate = '';      // admin: a date picked on the page, instead of the saved one
    let printing = false;     // every section is drawn, one to a page, while printing
    const drawn = [];         // the Charts showing, destroyed before each draw
    const waiting = new Map();   // canvas id -> chart spec not drawn yet (a closed More stays waiting)

    // Settings > Report (admin). Nothing is saved until the settings have loaded,
    // so a failed load cannot overwrite them with blanks.
    let sections = null;
    let titles = {};
    let chartSettings = null;

    function setVisible(on) {
        visible = on;
        const box = document.getElementById('wbrReport');
        if (!box) return;
        box.style.display = on ? '' : 'none';
        if (!on) return;
        if (!report && !loading) load();
        else render();
    }

    // refresh: fetch the data again rather than use what is kept for the hour.
    async function load(refresh) {
        loading = true;
        error = '';
        report = null;
        render();
        try {
            const query = new URLSearchParams();
            if (refresh) query.set('refresh', '1');
            if (reviewDate) query.set('update_date', reviewDate);
            const text = query.toString();
            const url = BASE_URL + '/weekly-business-review/report' + (text ? '?' + text : '');
            const resp = await fetch(url).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'Load failed');
            report = resp;
        } catch (err) {
            report = null;
            error = err.message;
        } finally {
            loading = false;
            render();
        }
    }

    function metaHtml() {
        const m = report.meta;
        const weeks = m.weeks.length ? `${m.weeks[0].label} To ${m.weeks[m.weeks.length - 1].label}` : '';
        const warnings = m.warnings.map(w => `<div class="wbr-report-warning">${escHtml(w)}</div>`).join('');
        return `<div class="wbr-deck-tools-row">
                    <span class="wbr-deck-count">Review Date ${escHtml(m.update_date)}</span>
                    <span class="wbr-deck-count">Months ${escHtml(m.months[0] || m.current_month)} To ${escHtml(m.current_month)}</span>
                    <span class="wbr-deck-count">Weeks ${escHtml(weeks)}</span>
                    <span class="wbr-deck-count">Last Closed Month ${escHtml(m.last_closed_month)}</span>
                    ${!isUserView() ? `<label class="wbr-deck-count">Review Date <input type="date" value="${escHtml(reviewDate || m.update_date)}" onchange="WbrReport.pickDate(this.value)"></label>
                    <button type="button" class="btn btn-edit" onclick="WbrReport.reload()"><i class="fas fa-sync"></i> Reload Data</button>` : ''}
                    <button type="button" class="btn btn-edit" onclick="WbrReport.print()"><i class="fas fa-print"></i> Print / Save PDF</button>
                </div>${warnings}`;
    }

    // ── Numbers ──
    const WHOLE = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
    const PENCE = new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    function money(value, format) {
        const text = format.format(Math.abs(value));
        return (value < 0 && /[1-9]/.test(text) ? '-£' : '£') + text;
    }

    function formatValue(value, format) {
        if (value === null || value === undefined) return '';
        if (typeof value === 'string') return value;
        switch (format) {
            case 'gbp0': return money(value, WHOLE);
            case 'gbp': return money(value, PENCE);
            case 'pct0': return WHOLE.format(value * 100) + '%';
            case 'pct': return (value * 100).toFixed(1) + '%';
            case 'days': return value.toFixed(1);
            case 'k': return WHOLE.format(value / 1000) + ' k';
            default: return WHOLE.format(value);
        }
    }

    // ── Tables ──
    function cellHtml(row, column) {
        if (column.group === 'spacer') return '<td class="wbr-report-gap"></td>';
        if (column.key === 'trend') {
            const label = row.trend === 'up' ? 'Up' : 'Down';
            return `<td class="wbr-report-trend">${row.trend ? `<span class="wbr-trend-${row.trend}" role="img" aria-label="${label}" title="${label}"></span>` : ''}</td>`;
        }
        return `<td>${escHtml(formatValue(row.values[column.key], row.format))}</td>`;
    }

    function tableHtml(table) {
        const head = table.columns.map(c => c.group === 'spacer'
            ? '<th class="wbr-report-gap"></th>' : `<th>${escHtml(c.label)}</th>`).join('');
        const body = table.rows.map(row => {
            if (!row.label) return `<tr class="wbr-report-blank"><td colspan="${table.columns.length + 1}"></td></tr>`;
            const classes = [row.bold ? 'wbr-report-bold' : '', row.indent ? 'wbr-report-indent' : ''].join(' ');
            return `<tr class="${classes}"><td class="wbr-report-label">${escHtml(row.label)}</td>${table.columns.map(c => cellHtml(row, c)).join('')}</tr>`;
        }).join('');
        return `<div class="wbr-report-table-wrap"><table class="wbr-report-table">
                    <thead><tr><th class="wbr-report-label">${escHtml(table.title)}</th>${head}</tr></thead>
                    <tbody>${body}</tbody>
                </table></div>`;
    }

    // ── Charts ──
    function chartsHtml(list) {
        if (!list.length) return '';
        return `<div class="wbr-report-charts">${list.map(spec => {
            const id = 'wbrReportChart' + waiting.size;
            waiting.set(id, spec);
            return `<div class="wbr-report-chart">
                        <div class="wbr-report-chart-title">${escHtml(spec.title)}</div>
                        <div class="wbr-report-plot"><canvas id="${id}" role="img" aria-label="${escHtml(spec.title)}"></canvas></div>
                    </div>`;
        }).join('')}</div>`;
    }

    function chartOptions(spec, extra) {
        const label = value => formatValue(value, spec.unit);
        return {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 }, filter: item => item.text !== '' } },
                tooltip: {
                    filter: item => item.dataset.label !== '' && item.raw !== null,
                    callbacks: { label: item => `${item.dataset.label}: ${label(item.raw)}` },
                },
            },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 11 } } },
                y: { grid: { color: '#e2e8f0' }, ticks: { font: { size: 11 }, callback: label }, ...extra },
            },
        };
    }

    function hasData(values) {
        return values.some(v => v !== null && v !== undefined);
    }

    function lineDataset(label, data, colour, dashed) {
        return { label, data, borderColor: colour, backgroundColor: colour, borderWidth: 2, tension: 0,
                 borderDash: dashed ? [5, 4] : [], pointRadius: 3, pointHoverRadius: 5,
                 pointBorderColor: '#fff', pointBorderWidth: 1, spanGaps: false, fill: false };
    }

    // The actual line against a budget band: one colour below the budget, another above.
    function drawBandChart(canvas, spec, look) {
        const { actual, n1, budget } = spec.series;
        const datasets = [
            { label: spec.budget_label, data: budget, borderColor: look.budget_line, backgroundColor: look.below_budget,
              borderWidth: 1, pointRadius: 0, stepped: 'middle', fill: 'start', spanGaps: true, order: 3 },
            { label: '', data: budget.map(() => spec.axis.max), borderWidth: 0, pointRadius: 0, pointHoverRadius: 0,
              backgroundColor: look.above_budget, fill: { target: 0 }, order: 4 },
            { ...lineDataset('Actual', actual, look.actual), order: 1 },
        ];
        if (hasData(n1)) datasets.push({ ...lineDataset('N-1', n1, look.n1, true), order: 2 });
        return new Chart(canvas, { type: 'line', data: { labels: spec.labels, datasets },
                                   options: chartOptions(spec, { min: spec.axis.min, max: spec.axis.max }) });
    }

    function drawLinesChart(canvas, spec, look) {
        const datasets = spec.series.filter(s => hasData(s.data))
            .map(s => lineDataset(s.name, s.data, look[s.kind], s.dashed));
        return new Chart(canvas, { type: 'line', data: { labels: spec.labels, datasets }, options: chartOptions(spec) });
    }

    // One hue, light to dark: oldest period lightest. The target is a marker on each stage.
    function drawBarsChart(canvas, spec, look) {
        const count = spec.series.length;
        const datasets = spec.series.map((s, i) => ({
            label: s.name, data: s.data, backgroundColor: look.actual + Math.round((0.35 + 0.65 * (i + 1) / count) * 255).toString(16).padStart(2, '0'),
            borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom', categoryPercentage: 0.8, barPercentage: 0.85, order: 2,
        }));
        datasets.push({ type: 'line', label: 'Target', data: spec.target, showLine: false, pointStyle: 'line',
                        pointRadius: 16, pointHoverRadius: 16, pointBorderWidth: 3, borderColor: look.series_three,
                        backgroundColor: look.series_three, order: 1 });
        return new Chart(canvas, { type: 'bar', data: { labels: spec.labels, datasets }, options: chartOptions(spec, { beginAtZero: true }) });
    }

    function drawWaiting(root) {
        if (typeof Chart === 'undefined') return;
        const look = report.meta.appearance;
        root.querySelectorAll('canvas').forEach(canvas => {
            const spec = waiting.get(canvas.id);
            if (!spec || canvas.closest('details:not([open])')) return;
            waiting.delete(canvas.id);
            const draw = { band: drawBandChart, lines: drawLinesChart, bars: drawBarsChart }[spec.type];
            drawn.push(draw(canvas, spec, look));
        });
    }

    // ── A section ──
    function sectionHtml(section) {
        const more = section.more || { tables: [], charts: [] };
        const hasMore = more.tables.length || more.charts.length;
        const empty = !section.tables.length && !section.charts.length;
        return `<div class="wbr-slide-head"><div class="wbr-slide-title">${escHtml(section.title)}</div></div>
                ${empty ? '<div class="wbr-report-empty">This section has no data yet.</div>' : ''}
                ${section.tables.map(tableHtml).join('')}
                ${chartsHtml(section.charts)}
                ${hasMore ? `<details class="wbr-report-more" ${printing ? 'open' : ''} ontoggle="WbrReport.drawMore(this)">
                                <summary>More</summary>
                                ${more.tables.map(tableHtml).join('')}
                                ${chartsHtml(more.charts)}
                             </details>` : ''}
                ${(section.notes || []).map(n => `<div class="wbr-report-note">${escHtml(n)}</div>`).join('')}`;
    }

    function clearCharts() {
        drawn.splice(0).forEach(chart => chart.destroy());
        waiting.clear();
    }

    function drawSlide(slide, html) {
        const look = report.meta.appearance;
        slide.style.setProperty('--wbr-report-chart-height', look.chart_height + 'px');
        slide.style.setProperty('--wbr-report-table-font', look.table_font_px + 'px');
        slide.innerHTML = html;
        drawWaiting(slide);
    }

    function render() {
        if (!visible) return;
        const strip = document.getElementById('wbrReportStrip');
        const meta = document.getElementById('wbrReportMeta');
        const slide = document.getElementById('wbrReportSlide');
        // Before any section's HTML is built, as building it lists the charts to draw.
        clearCharts();
        if (!report) {
            strip.innerHTML = '';
            meta.innerHTML = '';
            slide.innerHTML = `<div class="wbr-report-empty">${escHtml(loading ? 'Loading...' : error)}</div>`;
            return;
        }
        const list = report.sections;
        current = Math.min(Math.max(current, 0), Math.max(list.length - 1, 0));
        strip.innerHTML = list.map((s, i) => `
            <div class="wbr-strip-slide ${i === current ? 'active' : ''}" onclick="WbrReport.show(${i})">
                <span class="wbr-strip-num">${i + 1}</span>
                <span class="wbr-strip-title">${escHtml(s.title)}</span>
            </div>`).join('');
        meta.innerHTML = metaHtml();
        const section = list[current];
        if (printing) drawSlide(slide, list.map(s => `<div class="wbr-report-page">${sectionHtml(s)}</div>`).join(''));
        else if (section) drawSlide(slide, sectionHtml(section));
        else drawSlide(slide, '<div class="wbr-report-empty">Every section is switched off in Settings > Report.</div>');
    }

    function show(index) {
        current = index;
        render();
    }

    function pickDate(value) {
        reviewDate = value;
        load();
    }

    function print() {
        if (!report) return;
        printing = true;
        render();
        // Two frames, so the charts have their size before the page is printed.
        requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
    }

    window.addEventListener('afterprint', () => {
        if (!printing) return;
        printing = false;
        render();
    });

    // Left / right arrow keys move between sections (not while typing).
    document.addEventListener('keydown', event => {
        if (!visible || !report) return;
        if (event.target.closest('input, textarea, select, [contenteditable]')) return;
        if (document.querySelector('.modal-overlay.open')) return;
        if (event.key === 'ArrowLeft' && current > 0) show(current - 1);
        else if (event.key === 'ArrowRight' && current < report.sections.length - 1) show(current + 1);
    });

    // ── Settings > Report (admin) ──
    function sectionsHtml() {
        const named = (chartSettings && chartSettings.titles) || {};
        return sections.map((s, i) => `
            <div class="wbr-report-section">
                <input type="checkbox" aria-label="Show ${escHtml(titles[s.id] || s.id)}" ${s.on ? 'checked' : ''} onchange="WbrReport.setSectionOn(${i}, this.checked)">
                <input type="text" class="wbr-report-section-title" data-cs="titles.${s.id}" value="${escHtml(named[s.id] || titles[s.id] || s.id)}" aria-label="Section Title">
                <span class="wbr-strip-btns">
                    <button type="button" class="chart-title-btn" title="Move Up" onclick="WbrReport.moveSection(${i}, -1)" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="chart-title-btn" title="Move Down" onclick="WbrReport.moveSection(${i}, 1)" ${i === sections.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                </span>
            </div>`).join('');
    }

    function drawSections() {
        document.getElementById('wbrReportSections').innerHTML = sections ? sectionsHtml() : '';
    }

    function numberInput(path, value, step) {
        return `<input type="number" step="${step || 'any'}" data-cs="${path}" value="${value === undefined ? '' : escHtml(String(value))}">`;
    }

    function textInput(path, value) {
        return `<input type="text" data-cs="${path}" value="${escHtml(value || '')}">`;
    }

    // Everything a section shows that is not data. Saved as one, with the rest of the tab.
    function chartSettingsHtml(cs) {
        const chartRows = Object.entries(cs.charts).map(([id, c]) => `<tr>
            <td>${textInput(`charts.${id}.title`, c.title)}</td>
            <td>${'min' in c ? numberInput(`charts.${id}.min`, c.min) : ''}</td>
            <td>${'max' in c ? numberInput(`charts.${id}.max`, c.max) : ''}</td>
            <td>${'target' in c ? numberInput(`charts.${id}.target`, c.target) : ''}</td></tr>`).join('');
        const stages = { upstream: 'Upstream Stock', in_prep: 'In Prep', for_sale: 'For Sale', sold_not_delivered: 'Sold Not Delivered', total: 'Total' };
        const sleeping = { upstream: 'Upstream Stock', in_prep: 'In Prep', on_sale: 'On Sale Stock', sold_not_delivered: 'Sold Not Delivered', trade_hold: 'Trade Hold' };
        const field = (label, input) => `<div class="modal-field"><label>${label}${input}</label></div>`;
        const colours = { actual: 'Actual Line', n1: 'N-1 Line', series_three: 'Third Line', budget_line: 'Budget Line',
                          below_budget: 'Below Budget', above_budget: 'Above Budget' };
        return `
            <p class="modal-sub">Axis limits for a percent chart are fractions: 0.8 Is 80%.</p>
            <table class="wbr-report-settings-table">
                <thead><tr><th>Chart Title</th><th>Axis Min</th><th>Axis Max</th><th>Target</th></tr></thead>
                <tbody>${chartRows}</tbody>
            </table>
            <div class="wbr-settings-title">Targets</div>
            <div class="wbr-settings-grid">
                ${Object.entries(stages).map(([k, name]) => field(`Coverage: ${name} (Days)`, numberInput(`coverage_targets.${k}`, cs.coverage_targets[k]))).join('')}
                ${Object.entries(sleeping).map(([k, name]) => field(`Sleeping Stock: ${name}`, textInput(`sleeping_targets.${k}`, cs.sleeping_targets[k]))).join('')}
            </div>
            <div class="wbr-settings-title">Calculation</div>
            <div class="wbr-settings-grid">
                ${field('Days Per Month For Deliveries', numberInput('constants.deliveries_days_per_month', cs.constants.deliveries_days_per_month))}
                <div class="modal-field"><label>Trend Compares Last Week With
                    <select data-cs="trend_basis">
                        <option value="last_week" ${cs.trend_basis === 'last_week' ? 'selected' : ''}>The Week Before</option>
                        <option value="months" ${cs.trend_basis === 'months' ? 'selected' : ''}>The Average Of The Months Shown</option>
                    </select></label></div>
            </div>
            <div class="wbr-settings-title">Appearance</div>
            <div class="wbr-settings-grid">
                ${Object.entries(colours).map(([k, name]) => field(name, `<input type="color" class="setting-colour" data-cs="appearance.${k}" value="${escHtml(cs.appearance[k])}">`)).join('')}
                ${field('Chart Height (px)', numberInput('appearance.chart_height', cs.appearance.chart_height, 1))}
                ${field('Table Text Size (px)', numberInput('appearance.table_font_px', cs.appearance.table_font_px, 1))}
            </div>
            <div class="wbr-settings-title">Notes Under Each Section</div>
            ${Object.keys(titles).map(id => field(escHtml(titles[id]),
                `<textarea rows="2" data-cs="notes.${id}">${escHtml((cs.notes || {})[id] || '')}</textarea>`)).join('')}`;
    }

    // What the tab's inputs hold, over what was loaded. A blank number uses the default.
    function readChartSettings() {
        const out = JSON.parse(JSON.stringify(chartSettings));
        document.querySelectorAll('#wbrTabReport [data-cs]').forEach(input => {
            const path = input.dataset.cs.split('.');
            const last = path.pop();
            let holder = out;
            path.forEach(key => { holder = holder[key] = holder[key] || {}; });
            if (input.type === 'number') holder[last] = input.value === '' ? null : Number(input.value);
            else holder[last] = input.value;
        });
        return out;
    }

    // The SharePoint files' state, edited in Settings > SharePoint. Accounts come from Snowflake.
    function sourcesHtml(files) {
        const when = value => value ? escHtml(String(value).slice(0, 16).replace('T', ' ')) : 'Never';
        return files.map(f => `<div><strong>${escHtml(f.name)}</strong>:
            ${f.enabled ? `Saved ${when(f.last_saved_at)}, Checked ${when(f.last_checked_at)}` : 'Switched Off'}
            ${f.last_error ? `<span class="wbr-report-source-error">${escHtml(f.last_error)}</span>` : ''}</div>`).join('');
    }

    async function openSettings() {
        sections = null;
        chartSettings = null;
        drawSections();
        document.getElementById('wbrReportChartSettings').innerHTML = '';
        const status = document.getElementById('wbrReportSettingsStatus');
        status.textContent = 'Loading...';
        try {
            const resp = await fetch(BASE_URL + '/weekly-business-review/report/settings').then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'Load failed');
            document.getElementById('setWbrReportUpdateDate').value = resp.settings.update_date;
            document.getElementById('setWbrReportLastClosed').value = resp.settings.last_closed_month;
            document.getElementById('setWbrReportAutoviewStart').value = resp.settings.autoview_start;
            titles = resp.titles;
            sections = resp.settings.sections;
            chartSettings = resp.settings.chart_settings;
            status.textContent = '';
            drawSections();
            document.getElementById('wbrReportChartSettings').innerHTML = chartSettingsHtml(chartSettings);
            document.getElementById('wbrReportSources').innerHTML = sourcesHtml(resp.sources || []);
        } catch (err) {
            status.textContent = 'Report settings could not be loaded: ' + err.message;
        }
    }

    function setSectionOn(index, on) {
        sections[index].on = on;
    }

    function moveSection(index, by) {
        chartSettings = readChartSettings();   // keeps titles typed but not saved yet
        const [entry] = sections.splice(index, 1);
        sections.splice(index + by, 0, entry);
        drawSections();
    }

    // Called by Save in the popup. Throws if the save fails, so the popup stays
    // open. The report is fetched again when it next shows.
    async function saveSettings() {
        if (!sections) return;
        const resp = await fetch(BASE_URL + '/weekly-business-review/report/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings: {
                update_date: document.getElementById('setWbrReportUpdateDate').value,
                last_closed_month: document.getElementById('setWbrReportLastClosed').value,
                autoview_start: document.getElementById('setWbrReportAutoviewStart').value,
                sections,
                chart_settings: readChartSettings(),
            } }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Report settings save failed');
        report = null;
    }

    return { setVisible, show, reload: () => load(true), pickDate, print, drawMore: drawWaiting,
             openSettings, saveSettings, setSectionOn, moveSection };
})();
