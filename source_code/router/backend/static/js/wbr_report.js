// Weekly Business Review > Report view. Loaded before kpi.js on the review page
// only; everything it calls from kpi.js (escHtml, BASE_URL ...) is only used once
// the page has loaded. wbr_deck.js decides which view shows and calls setVisible.
//
// The server builds each section's tables (routers/wbr_report_sections.py) and sends
// raw numbers; this formats them. The charts are the KPI page's own: each section
// lists the KPI charts chosen for it, drawn with buildRowCharts as the slides do.
const WbrReport = (() => {
    let visible = false;
    let report = null;        // the last response from the server
    let error = '';
    let loading = false;
    let current = 0;          // index of the section showing
    let reviewDate = '';      // admin: a date picked on the page, instead of the saved one
    let printing = false;     // every section is drawn, one to a page, while printing

    // {section id: [{id, ref (KPI row id), period: 'm'|'w'|'d', title?, subtitle?}]}
    let kpiCharts = {};
    const drawn = {};         // item id -> Chart
    let saving = Promise.resolve();

    // Settings > Report (admin). Nothing is saved until the settings have loaded,
    // so a failed load cannot overwrite them with blanks.
    let sections = null;
    let titles = {};
    let chartSettings = null;
    let glossaryDefault = '';

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
        const format = (row.formats && row.formats[column.key]) || row.format;
        return `<td>${escHtml(formatValue(row.values[column.key], format))}</td>`;
    }

    function tableHtml(table) {
        const head = table.columns.map(c => c.group === 'spacer'
            ? '<th class="wbr-report-gap"></th>' : `<th>${escHtml(c.label)}</th>`).join('');
        const body = table.rows.map(row => {
            if (!row.label) return `<tr class="wbr-report-blank"><td colspan="${table.columns.length + 1}"></td></tr>`;
            const classes = [row.bold ? 'wbr-report-bold' : '', row.indent ? 'wbr-report-indent' : ''].join(' ');
            const hint = row.hint ? ` title="${escHtml(row.hint)}"` : '';
            return `<tr class="${classes}"><td class="wbr-report-label"${hint}>${escHtml(row.label)}</td>${table.columns.map(c => cellHtml(row, c)).join('')}</tr>`;
        }).join('');
        // A table of words (the glossary) has its first heading in label_heading.
        const heading = table.text ? `<div class="wbr-report-table-title">${escHtml(table.title)}</div>` : '';
        const corner = table.text ? (table.label_heading || '') : table.title;
        return `${heading}<div class="wbr-report-table-wrap"><table class="wbr-report-table ${table.text ? 'wbr-report-table-text' : ''}">
                    <thead><tr><th class="wbr-report-label">${escHtml(corner)}</th>${head}</tr></thead>
                    <tbody>${body}</tbody>
                </table></div>`;
    }

    // ── KPI charts ──
    function setCharts(value) {
        kpiCharts = value && typeof value === 'object' ? value : {};
    }

    function refs() {
        return Object.values(kpiCharts).flat().map(item => item.ref);
    }

    function sectionId() {
        const section = report && report.sections[current];
        return section ? section.id : null;
    }

    function sectionItems() {
        return kpiCharts[sectionId()] || [];
    }

    // The items on the page: the section showing, or every section while printing.
    function shownItems() {
        if (!report) return [];
        return printing ? report.sections.flatMap(s => kpiCharts[s.id] || []) : sectionItems();
    }

    function chartActionsHtml(item, index, count) {
        return `<div class="wbr-item-actions">
                    <button type="button" class="chart-title-btn" onclick="WbrReport.moveChart('${item.id}', -1)" ${index === 0 ? 'disabled' : ''}><i class="fas fa-arrow-left"></i> Move Left</button>
                    <button type="button" class="chart-title-btn" onclick="WbrReport.moveChart('${item.id}', 1)" ${index === count - 1 ? 'disabled' : ''}><i class="fas fa-arrow-right"></i> Move Right</button>
                    <button type="button" class="chart-title-btn" onclick="WbrReport.removeChart('${item.id}')"><i class="fas fa-times"></i> Remove</button>
                    ${wbrTitleBoxes(item, `WbrReport.setChartText('${item.id}', KEY, this.value)`)}
                </div>`;
    }

    function chartHtml(item, index, count) {
        const row = findRow(item.ref);
        const scroller = item.period === 'd' ? '' : wbrScrollerHtml(item.period);
        const comments = typeof WbrComments !== 'undefined' ? WbrComments.slotHtml(`chart:${item.ref}|${item.period}`) : '';
        const card = row ? chartCardHtml({
            canvasId: 'wbrr-' + item.id, title: wbrItemTitle(item, row), subtitle: wbrItemSubtitle(item, row),
            legend: false, dayScroller: item.period === 'd',
            headExtra: `<span class="chart-period">${escHtml(PERIOD_NAMES[item.period])}</span>${scroller}${comments}`,
        }) : '<div class="wbr-item-missing">This chart has been removed from the KPI page.</div>';
        const actions = isUserView() || printing ? '' : chartActionsHtml(item, index, count);
        return `<div class="wbr-report-chart">${actions}${card}</div>`;
    }

    function chartsHtml(items) {
        if (!items.length) return '';
        return `<div class="wbr-report-charts">${items.map((item, i) => chartHtml(item, i, items.length)).join('')}</div>`;
    }

    function drawChart(item) {
        const canvas = document.getElementById('wbrr-' + item.id);
        const row = findRow(item.ref);
        const full = row && customRowData[row.id];
        if (!canvas || !full) return;   // drawn when its data arrives (rowLoaded)
        if (drawn[item.id]) { drawn[item.id].destroy(); delete drawn[item.id]; }
        // The same window as every other review chart (Settings, wbrWindow).
        drawn[item.id] = buildRowCharts(row, wbrSliceData(row, full), [{ k: item.period, canvasId: canvas.id, existing: null,
                                                                         first: item.period === 'd' ? 0 : wbrStart(row.id, item.period) }])[0];
    }

    // A KPI row's data has arrived or changed: redraw its charts on the page.
    function rowLoaded(row) {
        if (!visible) return;
        shownItems().filter(item => item.ref === row.id).forEach(drawChart);
        updateWbrSliders();
    }

    function clearCharts() {
        Object.keys(drawn).forEach(id => { drawn[id].destroy(); delete drawn[id]; });
    }

    async function saveCharts(charts) {
        blockSaveIfLoadFailed();
        const resp = await fetch(BASE_URL + '/weekly-business-review/report/charts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ charts }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
    }

    // The page changes straight away and is put back if the save fails.
    async function changeSection(items, what) {
        const before = kpiCharts;
        const mine = { ...kpiCharts, [sectionId()]: items };
        kpiCharts = mine;
        renderLayout();
        const done = saving.then(() => saveCharts(mine));
        saving = done.catch(() => {});
        try {
            await done;
            return true;
        } catch (err) {
            if (kpiCharts === mine) { kpiCharts = before; renderLayout(); }
            document.getElementById('statusText').textContent = `${what} failed: ${err.message}`;
            return false;
        }
    }

    // From the picker: into the section showing. The popup is locked while it saves.
    async function addCharts(ref, periods, button) {
        const row = findRow(ref);
        if (!row || !sectionId()) return;
        const newId = () => 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        const added = periods.map(period => ({ id: newId(), ref, period }));
        const box = button.closest('.modal-box');
        box.classList.add('busy');
        button.disabled = true;
        button.textContent = 'Saving...';
        const ok = await changeSection([...sectionItems(), ...added], 'Add');
        box.classList.remove('busy');
        renderWbrPicker();
        if (ok && !customRowData[ref]) loadCustomRow(row);
    }

    function removeChart(id) {
        changeSection(sectionItems().filter(item => item.id !== id), 'Remove');
    }

    function moveChart(id, step) {
        const items = [...sectionItems()];
        const a = items.findIndex(item => item.id === id), b = a + step;
        if (a < 0 || b < 0 || b >= items.length) return;
        [items[a], items[b]] = [items[b], items[a]];
        changeSection(items, 'Move');
    }

    // A chart's own title / subtitle in this section; blank = the KPI chart's.
    function setChartText(id, key, value) {
        const text = String(value || '').trim().slice(0, key === 'title' ? 200 : 300);
        changeSection(sectionItems().map(item => {
            if (item.id !== id) return item;
            const next = { ...item };
            if (text) next[key] = text;
            else delete next[key];
            return next;
        }), 'Change');
    }

    // ── A section ──
    function sectionHtml(section) {
        const more = section.more || { tables: [] };
        const items = kpiCharts[section.id] || [];
        const empty = !section.tables.length && !items.length;
        const linked = section.link ? report.sections.findIndex(s => s.id === section.link.section) : -1;
        return `<div class="wbr-slide-head"><div class="wbr-slide-title">${escHtml(section.title)}</div>
                    ${linked >= 0 && !printing ? `<button type="button" class="wbr-report-link" onclick="WbrReport.show(${linked})">${escHtml(section.link.label)}</button>` : ''}</div>
                ${empty ? '<div class="wbr-report-empty">This section has no data yet.</div>' : ''}
                ${section.tables.map(tableHtml).join('')}
                ${chartsHtml(items)}
                ${more.tables.length ? `<details class="wbr-report-more" ${printing ? 'open' : ''}>
                                <summary>More</summary>
                                ${more.tables.map(tableHtml).join('')}
                             </details>` : ''}
                ${(section.notes || []).map(n => `<div class="wbr-report-note">${escHtml(n)}</div>`).join('')}`;
    }

    function drawSlide(slide, html) {
        const look = report.meta.appearance;
        slide.style.setProperty('--wbr-report-chart-width', look.chart_width + 'px');
        slide.style.setProperty('--wbr-report-chart-height', look.chart_height + 'px');
        slide.style.setProperty('--wbr-report-table-font', look.table_font_px + 'px');
        slide.innerHTML = html;
        shownItems().forEach(drawChart);
        updateWbrSliders();
        DayWindow.updateScrollers();
        if (typeof WbrComments !== 'undefined') WbrComments.refreshButtons();
    }

    function render() {
        if (!visible) return;
        const strip = document.getElementById('wbrReportStrip');
        const meta = document.getElementById('wbrReportMeta');
        const slide = document.getElementById('wbrReportSlide');
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
        return `<input type="number" step="${step || 'any'}" data-cs="${path}" value="${value === undefined || value === null ? '' : escHtml(String(value))}">`;
    }

    function textInput(path, value) {
        return `<input type="text" data-cs="${path}" value="${escHtml(value || '')}">`;
    }

    // Everything a section shows that is not data. Saved as one, with the rest of the tab.
    function chartSettingsHtml(cs) {
        const stages = { upstream: 'Upstream Stock', in_prep: 'In Prep', for_sale: 'For Sale', sold_not_delivered: 'Sold Not Delivered', total: 'Total' };
        const sleeping = { upstream: 'Upstream Stock', in_prep: 'In Prep', on_sale: 'On Sale Stock', sold_not_delivered: 'Sold Not Delivered', trade_hold: 'Trade Hold' };
        const field = (label, input) => `<div class="modal-field"><label>${label}${input}</label></div>`;
        const constants = { deliveries_days_per_month: 'Days Per Month For Deliveries', fiscal_year_start_month: 'Financial Year Starts In Month (1 To 12)',
                            top_suppliers: 'Top Suppliers Shown', sites_shown: 'Sites Shown', finance_companies_shown: 'Finance Companies Shown',
                            sensitivity_gpu_step: 'Sensitivity GPU-MSB Step (£)', sensitivity_deliveries_step: 'Sensitivity Deliveries Step' };
        return `
            <p class="modal-sub">Charts Are KPI Charts: Add Them With Add Chart From KPI, While The Section Is Showing, And Edit Them On The KPI Page.</p>
            <div class="wbr-settings-title">Targets</div>
            <div class="wbr-settings-grid">
                ${Object.entries(stages).map(([k, name]) => field(`Coverage: ${name} (Days)`, numberInput(`coverage_targets.${k}`, cs.coverage_targets[k]))).join('')}
                ${Object.entries(sleeping).map(([k, name]) => field(`Sleeping Stock: ${name}`, textInput(`sleeping_targets.${k}`, cs.sleeping_targets[k]))).join('')}
            </div>
            <div class="wbr-settings-title">Calculation</div>
            <div class="wbr-settings-grid">
                ${Object.entries(constants).map(([k, name]) => field(name, numberInput(`constants.${k}`, cs.constants[k]))).join('')}
                <div class="modal-field"><label>Trend Compares Last Week With
                    <select data-cs="trend_basis">
                        <option value="last_week" ${cs.trend_basis === 'last_week' ? 'selected' : ''}>The Week Before</option>
                        <option value="months" ${cs.trend_basis === 'months' ? 'selected' : ''}>The Average Of The Months Shown</option>
                    </select></label></div>
            </div>
            <div class="wbr-settings-title">Appearance</div>
            <div class="wbr-settings-grid">
                ${field('Chart Width (px)', numberInput('appearance.chart_width', cs.appearance.chart_width, 1))}
                ${field('Chart Height (px)', numberInput('appearance.chart_height', cs.appearance.chart_height, 1))}
                ${field('Table Text Size (px)', numberInput('appearance.table_font_px', cs.appearance.table_font_px, 1))}
            </div>
            <div class="wbr-settings-title">Notes Under Each Section</div>
            ${Object.keys(titles).map(id => field(escHtml(titles[id]),
                `<textarea rows="2" data-cs="notes.${id}">${escHtml((cs.notes || {})[id] || '')}</textarea>`)).join('')}
            <div class="wbr-settings-title">Glossary</div>
            <p class="modal-sub"># Starts A Table, The Next Line Is Its Headings, And | Splits The Cells. Clear It To Go Back To The Standard Text.</p>
            <textarea rows="12" data-cs="glossary" aria-label="Glossary">${escHtml(cs.glossary || glossaryDefault)}</textarea>`;
    }

    // What the tab's inputs hold, over what was loaded. A blank number uses the default.
    function readChartSettings() {
        const out = JSON.parse(JSON.stringify(chartSettings));
        document.querySelectorAll('#wbrTabReport [data-cs]').forEach(input => {
            const path = input.dataset.cs.split('.');
            const last = path.pop();
            let holder = out;
            path.forEach(key => { holder = holder[key] = holder[key] || {}; });
            if (input.type === 'checkbox') holder[last] = input.checked;
            else if (input.type === 'number') holder[last] = input.value === '' ? null : Number(input.value);
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
            glossaryDefault = resp.glossary_default || '';
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

    return { setVisible, show, reload: () => load(true), pickDate, print, active: () => visible,
             setCharts, refs, sectionItems, rowLoaded, addCharts, removeChart, moveChart, setChartText,
             charts: () => Object.values(drawn),
             openSettings, saveSettings, setSectionOn, moveSection };
})();
