// KPI page script, moved out of templates/kpi.html. BASE_URL and IS_ADMIN are set
// by a small inline script in the template, which must load before this file.
    // Shared helpers (escHtml, dateLabel, offsetRange, makeBgPlugin,
    // makeDataLabelPlugin, chartCardHtml, DayWindow, ...) come from charts.js.
    const SOURCE_LABELS = { snowflake: 'Snowflake', sharepoint: 'SharePoint', autotrader: 'Autotrader Costs' };
    const Y_MIN_INPUTS = { m: 'yMinM', w: 'yMinW', d: 'yMinD' };
    const Y_MAX_INPUTS = { m: 'yMaxM', w: 'yMaxW', d: 'yMaxD' };

    // ── State ────────────────────────────────────────────────────────────────
    // Section names, in order. Set from /kpi/layout, edited in the Settings popup.
    let SECTIONS = [];
    let layout = { rows: [] };
    let metricsCatalogue = [];
    let metricsByKey = {};
    // Colour themes for multi-series charts, from /kpi/layout (Settings > Appearance).
    let themes = [];
    // Settings > Autotrader: dated allowance and cost rows (see kpi.py _AT_PERIODS_KEY).
    let atPeriods = [];
    let atPpcPeriods = [];   // dated cost per PPC click, a separate list
    // Display names for raw columns, shared by every chart ({ 'source|column':
    // name }), from /kpi/layout. Set in a stacked chart's series list or in
    // Settings > Column Names.
    let metricNames = {};
    // { 'source|column': { fmt, decimals } }: how each column's figure shows in
    // the point popup's breakdown (Settings > Column Names).
    let metricFormats = {};
    // { 'source|column*source|column': name }: the name of a metric multiplied
    // by another (Settings > Column Names > Derived Column Names). Kept apart
    // from metricNames so naming the result never renames the column itself.
    let derivedNames = {};
    function derivedKey(s) {
        const m = s.multiply_by;
        return m && m.metric ? `${metricKey(s.source || 'snowflake', s.metric)}*${metricKey(m.source || 'snowflake', m.metric)}` : '';
    }
    // Shown when a derived metric has no name of its own.
    function derivedDefaultName(s) {
        return `${metricName(s.source, s.metric)} × ${metricName(s.multiply_by.source, s.multiply_by.metric)}`;
    }
    function metricName(source, column) {
        return metricNames[metricKey(source || 'snowflake', column)] || column;
    }
    // Dropdown text: the name with the raw column after it, or just the column.
    function metricOptionText(source, column) {
        const name = metricNames[metricKey(source || 'snowflake', column)];
        return name ? `${name} (${column})` : column;
    }
    let rowCharts = {};        // rowId -> [Chart, Chart, Chart]
    const customRowEls = {};   // rowId -> element
    const customRowData = {};  // rowId -> last /kpi/custom-data response
    let layoutLoaded = false;
    // Set when the saved layout could not be read. Every save is blocked, since
    // saving what is on screen would replace the real layout, notes or settings.
    let layoutLoadFailed = false;
    function blockSaveIfLoadFailed() {
        if (layoutLoadFailed) throw new Error('The saved layout could not be loaded, so saving is turned off. Reload the page once the problem is fixed.');
    }

    // Chart periods and label angle, shared by everyone, set by the admin.
    let settings = { months: 14, weeks: 10, days_window: 35, days_total: 90, label_angle: 0, label_below: 0 };
    let settingLimits = {};

    // Dates of the day charts (all rows share the same dates).
    let kpiDates = [];

    // Chart notes: [{ id, date, title, text, row_id }] (row_id null = every chart).
    let chartNotes = [];

    DayWindow.configure({
        getCount: () => kpiDates.length,
        // The chart popup can show more days; the page goes back on close.
        getSize: () => (chartZoom && chartZoom.days) || settings.days_window,
        onChange: () => {
            DayWindow.applyToCharts(dayWindowCharts());
            updateDayTitles();
        },
        // A daily chart in the popup scrolls on its own; the page's scrollers stay put.
        getScrollerRoot: () => (zoomOnDaily() ? chartZoom.overlay : document),
    });

    function metricKey(source, column) { return source + '|' + column; }
    function rowSource(row) { return row.source || 'snowflake'; }
    // Rows saved before target modes existed drew the Snowflake target column.
    function rowTargetMode(row) { return row.target_mode || 'snowflake'; }
    function rowMetricInfo(row) { return metricsByKey[metricKey(rowSource(row), row.metric)] || {}; }
    // Metrics added to the chart's metric and to its divide-by metric, e.g.
    // orders / (calls + emails): [{ source, metric, op? }].
    function rowPlus(row) { return row.metric_plus || []; }
    function rowDividePlus(row) { return row.divide_by ? (row.divide_plus || []) : []; }
    // A ratio chart or one with added metrics keeps its own target on the row
    // (ratio_target): the stored targets each belong to a single metric.
    function rowOwnTarget(row) { return !!row.divide_by || rowPlus(row).length > 0; }
    // "orders / (calls + emails)": brackets only round a side of more than
    // one metric, and only when there is a divide. topOps / bottomOps hold the
    // operator before each name after the first ('+', '-', '*', '/'); missing
    // ones are '+', as on charts saved before operators could be picked.
    function formulaText(top, bottom, topOps = [], bottomOps = []) {
        const join = (names, ops) => names.map((n, i) => (i ? ` ${ops[i - 1] || '+'} ${n}` : n)).join('');
        const side = (names, ops) => names.length > 1 && bottom.length ? `(${join(names, ops)})` : join(names, ops);
        return bottom.length ? `${side(top, topOps)} / ${side(bottom, bottomOps)}` : side(top, topOps);
    }
    // The operators of a row's added metrics, in order.
    function plusOps(list) { return list.map(p => p.op || '+'); }
    // The symbol only, so the operator box stays narrow and the metric beside
    // it has room to show its name. Its tooltip says what each one does.
    const PLUS_OPS = [['+', '+'], ['-', '-'], ['*', '*'], ['/', '/']];

    function sectionDomId(name) {
        return 'sec-' + name.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    }

    // Weekly Business Review page (kpi.html in 'wbr' mode): a list of single
    // KPI charts. See the Weekly Business Review block further down.
    const WBR = typeof PAGE_MODE !== 'undefined' && PAGE_MODE === 'wbr';

    // ── Admin View / User View (admin only) ──
    // User View hides the editing buttons (Add Chart, Settings, Notes, Edit
    // Chart, Copy Chart Below). Kept in this browser only.
    const PAGE_VIEW_KEY = 'kpiPageView';

    function isUserView() {
        return !IS_ADMIN || document.body.classList.contains('user-view');
    }

    function setPageView(view) {
        document.body.classList.toggle('user-view', view === 'user');
        document.querySelectorAll('.view-toggle button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
        try { localStorage.setItem(PAGE_VIEW_KEY, view); } catch (e) {}
        // Admin View also lists empty sections as tabs (see renderLayout).
        if (layoutLoaded) { renderLayout(); redrawActiveSection(); }
    }

    if (IS_ADMIN) {
        let savedView = 'admin';
        try { savedView = localStorage.getItem(PAGE_VIEW_KEY) || 'admin'; } catch (e) {}
        setPageView(savedView);
    }

    async function init() {
        // The layout and metric list load before any chart data, so show
        // the loading state from the start, not only once loadAll begins.
        setPageLoading(true);
        document.getElementById('loadIcon').classList.add('spin');
        document.getElementById('loadBtn').disabled = true;
        try {
            const [layoutResp, metricsResp] = await Promise.all([
                fetch(BASE_URL + '/kpi/layout').then(r => r.json()),
                fetch(BASE_URL + '/kpi/metrics').then(r => r.json()),
                loadWbrBudgetTargets(),
            ]);
            if (layoutResp.status !== 'ok') throw new Error(layoutResp.message || 'no layout returned');
            if (layoutResp.sections) SECTIONS = layoutResp.sections;
            hiddenSections = new Set(layoutResp.hidden_sections || []);
            sectionSources = layoutResp.section_sources || {};
            sectionCompanies = layoutResp.section_companies || {};
            loadCollapsedRows(layoutResp.collapsed_rows || []);
            loadRollingRows(layoutResp.rolling_rows || []);
            myCharts = layoutResp.my_charts || [];
            wbrItems = layoutResp.wbr_items || [];
            if (WBR && typeof WbrDeck !== 'undefined') WbrDeck.setSaved(layoutResp.wbr_deck || null);
            layout = layoutResp.layout || { rows: [] };
            if (layoutResp.settings) settings = layoutResp.settings;
            applyAppearance();
            settingLimits = layoutResp.setting_limits || {};
            chartNotes = layoutResp.notes || [];
            themes = layoutResp.themes || [];
            atPeriods = layoutResp.at_periods || [];
            atPpcPeriods = layoutResp.at_ppc_periods || [];
            metricNames = layoutResp.metric_names || {};
            metricFormats = layoutResp.metric_formats || {};
            derivedNames = layoutResp.derived_names || {};
            metricsCatalogue = metricsResp.metrics || [];
            metricsByKey = Object.fromEntries(metricsCatalogue.map(m => [metricKey(m.source, m.column), m]));
        } catch (err) {
            showLayoutError('Layout load failed', err);
            return;
        }
        layoutLoaded = true;
        try {
            renderLayout();
        } catch (err) {
            // Without this the error only reaches the browser console and the
            // page sits on "Loading" for good.
            showLayoutError('Charts could not be shown', err);
            return;
        }
        loadAll();
        // A link from the alerts bell opens the comment it is about.
        if (WBR && typeof WbrComments !== 'undefined') WbrComments.openFromLink();
    }

    // Stops the page loading, turns saving off, shows the reason in the
    // status line and writes it to the server log (CloudWatch).
    function showLayoutError(what, err) {
        layoutLoadFailed = true;
        setPageLoading(false);
        document.getElementById('loadIcon').classList.remove('spin');
        const status = document.getElementById('statusText');
        status.textContent =
            what + ' - saving is turned off so the saved charts are not overwritten. ' + err.message;
        awsLoginIfExpired(status, err.message);
        console.error(err);
        fetch(BASE_URL + '/kpi/client-error', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: what + ': ' + err.message, stack: err.stack || '' }),
        }).catch(() => {});
    }
    document.addEventListener('DOMContentLoaded', init);

    // ── Layout rendering ─────────────────────────────────────────────────────
    function renderLayout() {
        if (WBR) { renderWbr(); return; }
        const container = document.getElementById('sectionsContainer');

        // Ensure section blocks exist, in order (existing blocks keep their open
        // state). My Charts comes first, then the admin's sections.
        [MY_CHARTS, ...SECTIONS].forEach(name => {
            const mine = name === MY_CHARTS;
            let sec = document.getElementById(mine ? MY_CHARTS_DOM_ID : sectionDomId(name));
            if (!sec && mine) {
                sec = document.createElement('div');
                sec.className = 'category-section';
                sec.id = MY_CHARTS_DOM_ID;
                const body = document.createElement('div');
                body.className = 'category-body open';
                body.appendChild(collapseToolsElement(name));
                const empty = document.createElement('div');
                empty.className = 'my-charts-empty';
                empty.textContent = 'No charts here yet. Use Add To My Charts on any chart to add it.';
                body.appendChild(empty);
                sec.appendChild(body);
            } else if (!sec) {
                sec = document.createElement('div');
                sec.className = 'category-section';
                sec.id = sectionDomId(name);
                // Sections start open on every page load.
                const header = document.createElement('div');
                header.className = 'category-header clickable open';
                header.innerHTML = '<i class="fas fa-chevron-right chevron"></i> ' + escHtml(name);
                header.onclick = () => toggleSection(name);
                const body = document.createElement('div');
                body.className = 'category-body open';
                // Autotrader advert figures above the first chart.
                if (/autotrader/i.test(name)) {
                    const strip = document.createElement('div');
                    strip.className = 'at-strip';
                    body.appendChild(strip);
                }
                // Collapse All / Expand All, below the Autotrader figures.
                body.appendChild(collapseToolsElement(name));
                sec.appendChild(header);
                sec.appendChild(body);
            }
            container.appendChild(sec);
        });

        // Place rows into section bodies, in layout order
        const missing = [...new Set(layout.rows
            .filter(row => !document.getElementById(sectionDomId(row.section)))
            .map(row => row.section))];
        if (missing.length) {
            throw new Error('Charts are saved in section(s) that are not in the section list: '
                + missing.join(', ') + '. Sections on this page: ' + SECTIONS.join(', ') + '.');
        }
        layout.rows.forEach(row => {
            if (row.hidden || hiddenSections.has(row.section)) { dropRowElement(row.id); return; }
            document.getElementById(sectionDomId(row.section)).querySelector('.category-body')
                .appendChild(getRowElement(row));
        });

        // One tab per section with shown charts (all sections until the layout
        // has loaded, and always in Admin View, so an empty section can still
        // be opened to add its first chart); only the chosen tab's section is shown.
        // My Charts is always there, for everyone, once the layout has loaded.
        const tabNames = (layoutLoaded ? [MY_CHARTS] : []).concat(SECTIONS.filter(name => !hiddenSections.has(name)
            && (!layoutLoaded || !isUserView() || shownRows().some(r => r.section === name))));
        if (!tabNames.includes(activeSection)) {
            let saved = null;
            try { saved = localStorage.getItem(ACTIVE_SECTION_KEY); } catch (e) {}
            activeSection = tabNames.includes(saved) ? saved : tabNames[layoutLoaded ? 1 : 0] || tabNames[0];
        }

        // A chart row is one set of charts on the page, so while My Charts
        // shows, this user's chosen rows are moved into it in their order;
        // on any other tab they stay in their own section.
        const mineBody = document.getElementById(MY_CHARTS_DOM_ID).querySelector('.category-body');
        const mineRows = myChartRows();
        if (activeSection === MY_CHARTS) mineRows.forEach(row => mineBody.appendChild(getRowElement(row)));
        mineBody.querySelector('.my-charts-empty').style.display = mineRows.length ? 'none' : '';
        const mineIds = new Set(mineRows.map(r => r.id));
        Object.entries(customRowEls).forEach(([id, el]) => el.classList.toggle('in-my-charts', mineIds.has(id)));

        document.getElementById('sectionTabs').innerHTML = tabNames.map((name, i) => `
            <div class="kpi-tab ${name === activeSection ? 'active' : ''} ${name === MY_CHARTS ? 'my-charts-tab' : ''}" role="tab"
                 aria-selected="${name === activeSection}" onclick="showSection(${i})">
                ${escHtml(name)}
            </div>`).join('');
        sectionTabNames = tabNames;
        SECTIONS.forEach(name => {
            document.getElementById(sectionDomId(name)).style.display = name === activeSection ? '' : 'none';
        });
        document.getElementById(MY_CHARTS_DOM_ID).style.display = activeSection === MY_CHARTS ? '' : 'none';
        showAtAllowance();

        setTimeout(resizeAllCharts, 10);
    }

    // Collapse All Charts / Expand All Charts row for a section's tab. Same
    // small button style as each row's own collapse button.
    function collapseToolsElement(name) {
        const tools = document.createElement('div');
        tools.className = 'section-tools';
        [['Collapse All Charts', 'fa-compress-alt', true], ['Expand All Charts', 'fa-expand-alt', false]]
            .forEach(([label, icon, collapse]) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'chart-title-btn';
                btn.innerHTML = `<i class="fas ${icon}"></i> ${label}`;
                btn.onclick = () => setSectionRowsCollapsed(name, collapse);
                tools.appendChild(btn);
            });
        return tools;
    }

    // ── My Charts ──
    // Each user's own tab of charts picked from any section. Saved against
    // their login on the server, in the order they set.
    const MY_CHARTS = 'My Charts';
    const MY_CHARTS_DOM_ID = 'sec-my-charts-own';
    let myCharts = [];

    // Shown rows on a tab, in the order that tab draws them.
    function rowsInTab(name) {
        if (WBR) return shownRows();
        return name === MY_CHARTS ? myChartRows() : shownRows().filter(r => r.section === name);
    }
    function myChartRows() {
        const shown = new Map(shownRows().map(r => [r.id, r]));
        return myCharts.map(id => shown.get(id)).filter(Boolean);
    }

    let myChartsSaving = Promise.resolve();
    function saveMyCharts(before) {
        const rows = [...myCharts];
        myChartsSaving = myChartsSaving.then(() => fetch(BASE_URL + '/kpi/my-charts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rows }),
        }).then(r => r.json()).then(j => {
            if (j.status !== 'ok') throw new Error(j.message || 'Save failed');
        }).catch(err => {
            if (myCharts.join() === rows.join()) { myCharts = before; renderLayout(); redrawActiveSection(); }
            document.getElementById('statusText').textContent = 'My Charts save failed: ' + err.message;
        }));
    }

    function addToMyCharts(id) {
        if (myCharts.includes(id)) return;
        const before = [...myCharts];
        myCharts = [...myCharts, id];
        renderLayout();
        saveMyCharts(before);
    }

    function removeFromMyCharts(id) {
        const before = [...myCharts];
        myCharts = myCharts.filter(x => x !== id);
        renderLayout();
        redrawActiveSection();
        saveMyCharts(before);
    }

    // Swap with the next shown My Charts row above (-1) or below (1).
    function moveMyChart(id, step) {
        const visible = myChartRows().map(r => r.id);
        const other = visible[visible.indexOf(id) + step];
        if (!other) return;
        const before = [...myCharts];
        const rows = [...myCharts];
        const a = rows.indexOf(id), b = rows.indexOf(other);
        [rows[a], rows[b]] = [rows[b], rows[a]];
        myCharts = rows;
        renderLayout();
        saveMyCharts(before);
    }

    // ── Weekly Business Review ──
    // The review is an ordered list of single charts, each one KPI chart row
    // and one of its periods: [{ id, ref (row id), period: 'm'|'w'|'d' }],
    // saved in kpi_layout. Charts are created and edited on the KPI page only.
    // Each KPI row the review uses is built and loaded once, as on the KPI
    // page, but kept hidden; the chosen period's card is moved into the grid,
    // so its targets, notes, popups and day scroller all work unchanged.
    let wbrItems = [];
    const wbrItemEls = {};   // item id -> grid cell
    const WBR_PERIOD_INDEX = { m: 0, w: 1, d: 2 };

    function wbrCard(item) {
        const canvas = document.getElementById(`cv-${item.ref}-${item.period}`);
        return canvas ? canvas.closest('.chart-card') : null;
    }

    // A removed item's card goes back into its hidden row, ready to be used again.
    function returnWbrCard(itemId) {
        const cell = wbrItemEls[itemId];
        const card = cell && cell.querySelector('.chart-card');
        const rowEl = cell && customRowEls[cell.dataset.ref];
        if (card && rowEl) rowEl.querySelector('.charts-row').appendChild(card);
    }

    function renderWbr() {
        const hidden = document.getElementById('sectionsContainer');
        const grid = document.getElementById('wbrGrid');
        shownRows().forEach(row => hidden.appendChild(getRowElement(row)));
        const live = new Set(wbrItems.map(i => i.id));
        Object.keys(wbrItemEls).forEach(id => {
            if (live.has(id)) return;
            returnWbrCard(id);
            wbrItemEls[id].remove();
            delete wbrItemEls[id];
        });
        wbrItems.forEach((item, i) => {
            let cell = wbrItemEls[item.id];
            if (!cell) {
                cell = document.createElement('div');
                cell.className = 'wbr-item';
                cell.dataset.ref = item.ref;
                wbrItemEls[item.id] = cell;
            }
            const old = cell.querySelector(':scope > .wbr-item-head');
            if (old) old.remove();
            const actions = isUserView() ? '' : `
                <div class="wbr-item-actions">
                    <button type="button" class="chart-title-btn" onclick="moveWbrItem('${item.id}', -1)" ${i === 0 ? 'disabled' : ''}>
                        <i class="fas fa-arrow-left"></i> Move Left
                    </button>
                    <button type="button" class="chart-title-btn" onclick="moveWbrItem('${item.id}', 1)" ${i === wbrItems.length - 1 ? 'disabled' : ''}>
                        <i class="fas fa-arrow-right"></i> Move Right
                    </button>
                    <button type="button" class="chart-title-btn" onclick="removeWbrItem('${item.id}')">
                        <i class="fas fa-times"></i> Remove
                    </button>
                    ${wbrTitleBoxes(item, `setWbrText('${item.id}', KEY, this.value)`)}
                </div>`;
            if (actions) cell.insertAdjacentHTML('afterbegin', `<div class="wbr-item-head">${actions}</div>`);
            // The card, unless it is already here (or open in the chart popup).
            if (!cell.querySelector('.chart-card, .chart-zoom-holder, .wbr-item-missing')) {
                const card = findRow(item.ref) ? wbrCard(item) : null;
                if (card) cell.appendChild(card);
                else cell.insertAdjacentHTML('beforeend', '<div class="wbr-item-missing">This chart has been removed from the KPI page.</div>');
            }
            const row = findRow(item.ref);
            const card = cell.querySelector('.chart-card');
            if (row && card) applyWbrTitles(card, item, row);
            // Month / week scroller in the card's title line, where the KPI
            // page has the day scroller (shown only when there is more to see).
            if (card && item.period !== 'd' && !card.querySelector('[data-wbr-scroller]')) {
                const zoom = card.querySelector('.chart-card-head .chart-zoom-btn');
                if (zoom) zoom.insertAdjacentHTML('beforebegin', wbrScrollerHtml(item.period));
            }
            // Comments button (wbr_comments.js fills it while Comments are on).
            if (card && typeof WbrComments !== 'undefined' && !card.querySelector('.wbr-comment-slot')) {
                const zoom = card.querySelector('.chart-card-head .chart-zoom-btn');
                if (zoom) zoom.insertAdjacentHTML('beforebegin', WbrComments.slotHtml(`chart:${item.ref}|${item.period}`));
            }
            grid.appendChild(cell);
        });
        document.getElementById('wbrEmpty').style.display = wbrItems.length ? 'none' : '';
        updateWbrSliders();
        if (typeof WbrDeck !== 'undefined') WbrDeck.render();
        if (typeof WbrComments !== 'undefined') WbrComments.refreshButtons();
        setTimeout(resizeAllCharts, 10);
    }

    // ── Review chart titles ──
    // A review chart (grid or slide) can have its own title and subtitle;
    // blank uses the KPI chart's. Only the review shows them: the card's
    // heading is changed on this page, never the KPI chart itself.
    function wbrItemTitle(item, row) {
        return (item.title || '').trim() || rowTitle(row);
    }
    function wbrItemSubtitle(item, row) {
        return (item.subtitle || '').trim() || row.subtitle || '';
    }

    // Title and Subtitle boxes; onchange has KEY replaced by 'title' / 'subtitle'.
    function wbrTitleBoxes(item, onchange) {
        const row = findRow(item.ref);
        const box = (key, label, width, fallback) => `
            <label class="wbr-count">${label}
                <input type="text" style="width:${width}px" maxlength="${key === 'title' ? 200 : 300}"
                       value="${escHtml(item[key] || '')}" placeholder="${escHtml(fallback || '')}"
                       onchange="${onchange.replace('KEY', `'${key}'`)}">
            </label>`;
        return box('title', 'Title', 170, row ? rowTitle(row) : '')
            + box('subtitle', 'Subtitle', 170, row ? row.subtitle || '' : '');
    }

    // The grid card's heading and subtitle line, set to the review's own.
    function applyWbrTitles(card, item, row) {
        const h2 = card.querySelector('.chart-card-head h2, h2');
        if (h2) h2.textContent = wbrItemTitle(item, row);
        const text = wbrItemSubtitle(item, row);
        let sub = card.querySelector('.chart-card-subtitle');
        if (!text) { if (sub) sub.remove(); return; }
        if (!sub) {
            sub = document.createElement('div');
            sub.className = 'chart-card-subtitle';
            const head = card.querySelector('.chart-card-head');
            if (head) head.after(sub);
            else card.prepend(sub);
        }
        sub.textContent = text;
    }

    function setWbrText(id, key, value) {
        const text = String(value || '').trim().slice(0, key === 'title' ? 200 : 300);
        changeWbrItems(wbrItems.map(i => {
            if (i.id !== id) return i;
            const next = { ...i };
            if (text) next[key] = text;
            else delete next[key];
            return next;
        }), 'Change');
    }

    // ── Review chart windows ──
    // Every month chart on the review shows the same window: Settings
    // wbr_months_before months before the current month to wbr_months_after
    // after it (weeks likewise). A scroller on each chart, styled as the KPI
    // page's day scroller, moves every chart of that period together. The
    // review loads wbr_months_scroll months back (sendMetricBatch), which is
    // as far as the scroller goes; when the window holds everything loaded,
    // there is no scroller.
    const wbrShift = { m: 0, w: 0 };   // points moved from the usual window (negative = earlier)
    const WBR_WINDOW_KEYS = { m: ['wbr_months_before', 'wbr_months_after'], w: ['wbr_weeks_before', 'wbr_weeks_after'] };

    // { start, end } drawn, lo / hi the range end can move over, home the
    // usual end. null when everything loaded is drawn (or not the review).
    function wbrWindow(k, starts, today) {
        if (!WBR || (k !== 'm' && k !== 'w') || !starts || !starts.length) return null;
        const n = starts.length;
        const [beforeKey, afterKey] = WBR_WINDOW_KEYS[k];
        const after = Math.max(0, Number(settings[afterKey]) || 0);
        const size = Math.max(0, Number(settings[beforeKey]) || 0) + after + 1;
        if (size >= n) return null;
        let cur = n - 1;
        if (today) {
            const found = starts.map(s => String(s).slice(0, 10)).filter(s => s <= today).length - 1;
            if (found >= 0) cur = found;
        }
        const lo = size - 1, hi = n - 1;
        const home = Math.min(Math.max(cur + after, lo), hi);
        const end = Math.min(Math.max(home + wbrShift[k], lo), hi);
        return { start: end - size + 1, end, lo, hi, home, size, n };
    }

    const wbrStarts = (d, k) => (k === 'm' ? d.monthly_starts : d.weekly_starts);

    // Index in the loaded data of a review chart's first drawn point.
    function wbrStart(rowId, kind) {
        const d = customRowData[rowId];
        const r = d && wbrWindow(kind, wbrStarts(d, kind), d.today);
        return r ? r.start : 0;
    }

    // The row's data with each month/week series cut to the review window.
    function wbrSliceData(row, d) {
        const out = { ...d };
        const cuts = {};
        [['m', 'monthly_'], ['w', 'weekly_']].forEach(([k, prefix]) => {
            const r = wbrWindow(k, wbrStarts(d, k), d.today);
            if (!r) return;
            const cut = a => a.slice(r.start, r.end + 1);
            cuts[k] = cut;
            Object.keys(d).forEach(key => {
                if (key.startsWith(prefix) && Array.isArray(d[key])) out[key] = cut(d[key]);
            });
        });
        if (d.stacked && (cuts.m || cuts.w)) {
            out.stacked = d.stacked.map(s => ({ ...s, m: cuts.m && s.m ? cuts.m(s.m) : s.m,
                                                     w: cuts.w && s.w ? cuts.w(s.w) : s.w }));
        }
        return out;
    }

    // The window for period k from any loaded chart (they all load the same periods).
    function wbrCurrentWindow(k) {
        const d = Object.values(customRowData).find(x => x && wbrStarts(x, k));
        return d ? wbrWindow(k, wbrStarts(d, k), d.today) : null;
    }

    // Same markup and classes as the KPI day scroller (DayWindow.scrollerHtml),
    // with its own data- names so the day scroller's handlers leave it alone.
    function wbrScrollerHtml(k) {
        return `
            <div class="chart-day-scroller" data-wbr-scroller="${k}" style="display:none;">
                <button type="button" class="chart-day-step" data-wbr-prev="${k}" title="Earlier">
                    <i class="fas fa-chevron-left"></i>
                </button>
                <div class="chart-day-track" data-wbr-track="${k}">
                    <div class="chart-day-thumb" data-wbr-thumb></div>
                </div>
                <button type="button" class="chart-day-step" data-wbr-next="${k}" title="Later">
                    <i class="fas fa-chevron-right"></i>
                </button>
                <button type="button" class="chart-day-latest" data-wbr-home="${k}" title="Back to the usual window">Current</button>
            </div>`;
    }

    function updateWbrSliders() {
        ['m', 'w'].forEach(k => {
            const r = wbrCurrentWindow(k);
            document.querySelectorAll(`[data-wbr-scroller="${k}"]`).forEach(el => {
                if (!r) { el.style.display = 'none'; return; }
                el.style.display = '';
                const thumb = el.querySelector('[data-wbr-thumb]');
                thumb.style.left = (r.start / r.n * 100) + '%';
                thumb.style.width = (r.size / r.n * 100) + '%';
                el.querySelector('[data-wbr-prev]').disabled = r.end <= r.lo;
                el.querySelector('[data-wbr-next]').disabled = r.end >= r.hi;
            });
        });
    }

    // Move every chart of period k so its window ends at point `end`.
    function wbrSlideTo(k, end) {
        const r = wbrCurrentWindow(k);
        if (!r) return;
        const target = Math.min(Math.max(end, r.lo), r.hi);
        if (target === r.end) return;
        wbrShift[k] = target - r.home;
        shownRows().forEach(row => { if (customRowData[row.id]) renderCustomRow(row, customRowData[row.id]); });
        updateWbrSliders();
    }

    if (WBR) {
        document.addEventListener('click', event => {
            const prev = event.target.closest('[data-wbr-prev]');
            const next = event.target.closest('[data-wbr-next]');
            const home = event.target.closest('[data-wbr-home]');
            const k = (prev || next || home) && (prev || next || home).getAttribute(prev ? 'data-wbr-prev' : next ? 'data-wbr-next' : 'data-wbr-home');
            if (!k) return;
            const r = wbrCurrentWindow(k);
            if (!r) return;
            wbrSlideTo(k, home ? r.home : r.end + (prev ? -1 : 1));
        });
        // Dragging the thumb, as on the day scroller: the thumb keeps the
        // point under the pointer; clicking the bare track centres it there.
        let wbrDrag = null;
        const trackToEnd = (k, track, clientX, grab) => {
            const r = wbrCurrentWindow(k);
            if (!r) return null;
            const rect = track.getBoundingClientRect();
            const thumbWidth = rect.width * (r.size / r.n);
            const usable = Math.max(1, rect.width - thumbWidth);
            const fraction = Math.min(Math.max((clientX - rect.left - grab) / usable, 0), 1);
            return Math.round(fraction * (r.n - r.size)) + r.size - 1;
        };
        document.addEventListener('pointerdown', event => {
            const track = event.target.closest('[data-wbr-track]');
            if (!track) return;
            const k = track.dataset.wbrTrack;
            const thumb = track.querySelector('[data-wbr-thumb]');
            const rect = thumb.getBoundingClientRect();
            const grab = event.target === thumb ? event.clientX - rect.left : rect.width / 2;
            wbrDrag = { k, track, grab };
            track.classList.add('dragging');
            track.setPointerCapture(event.pointerId);
            const end = trackToEnd(k, track, event.clientX, grab);
            if (end !== null) wbrSlideTo(k, end);
            event.preventDefault();
            event.stopPropagation();
        }, true);
        document.addEventListener('pointermove', event => {
            if (!wbrDrag) return;
            const end = trackToEnd(wbrDrag.k, wbrDrag.track, event.clientX, wbrDrag.grab);
            if (end !== null) wbrSlideTo(wbrDrag.k, end);
        });
        const endDrag = () => {
            if (!wbrDrag) return;
            wbrDrag.track.classList.remove('dragging');
            wbrDrag = null;
        };
        document.addEventListener('pointerup', endDrag);
        document.addEventListener('pointercancel', endDrag);
    }

    // ── Review Settings popup (admin) ──
    // The wbr_* settings, saved with the KPI settings so they apply for everyone.
    const WBR_SETTING_INPUTS = {
        wbr_months_before: 'setWbrMonthsBefore', wbr_months_after: 'setWbrMonthsAfter', wbr_months_scroll: 'setWbrMonthsScroll',
        wbr_weeks_before: 'setWbrWeeksBefore', wbr_weeks_after: 'setWbrWeeksAfter', wbr_weeks_scroll: 'setWbrWeeksScroll',
        wbr_columns: 'setWbrColumns', wbr_notes: 'setWbrNotes',
        wbr_comments_on: 'setWbrCommentsOn', wbr_comments_show_resolved: 'setWbrCommentsShowResolved',
        wbr_slides_on: 'setWbrSlidesOn', wbr_grid_cols: 'setWbrGridCols', wbr_grid_rows: 'setWbrGridRows',
        wbr_snap: 'setWbrSnap', wbr_slide_gridlines: 'setWbrSlideGridlines',
        wbr_slide_border_px: 'setWbrSlideBorderPx', wbr_slide_border_colour: 'setWbrSlideBorderColour',
        wbr_slide_title_px: 'setWbrSlideTitlePx', wbr_slide_subtitle_px: 'setWbrSlideSubtitlePx',
    };
    // Changing these changes what is loaded, so the charts are fetched again.
    const WBR_LOAD_SETTINGS = ['wbr_months_after', 'wbr_months_scroll', 'wbr_weeks_after', 'wbr_weeks_scroll'];

    // The popup reopens on the tab last used this visit.
    let wbrSettingsTab = 'wbrTabView';

    function showWbrSettingsTab(id) {
        wbrSettingsTab = id;
        if (id === 'wbrTabBudgets' && typeof WbrBudgets !== 'undefined') WbrBudgets.open();
        document.querySelectorAll('#wbrSettingsModal .settings-pane').forEach(p => p.classList.toggle('active', p.id === id));
        document.querySelectorAll('#wbrSettingsModal .settings-tabs > button').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
    }

    function openWbrSettings() {
        Object.entries(WBR_SETTING_INPUTS).forEach(([name, id]) => {
            const input = document.getElementById(id);
            input.value = settings[name];
            const limits = settingLimits[name];
            if (limits) { input.min = limits[0]; input.max = limits[1]; }
        });
        document.getElementById('wbrSettingsError').classList.remove('show');
        if (typeof WbrBudgets !== 'undefined') WbrBudgets.reset();
        showWbrSettingsTab(wbrSettingsTab);
        document.getElementById('wbrSettingsModal').classList.add('open');
    }

    function closeWbrSettings() {
        document.getElementById('wbrSettingsModal').classList.remove('open');
    }

    async function saveWbrSettings() {
        const next = { ...settings };
        Object.entries(WBR_SETTING_INPUTS).forEach(([name, id]) => {
            next[name] = readSettingInput(document.getElementById(id));
        });
        const errorBox = document.getElementById('wbrSettingsError');
        const fail = message => { errorBox.textContent = message; errorBox.classList.add('show'); };
        for (const [name, value] of Object.entries(next)) {
            const limits = settingLimits[name];
            if (name in WBR_SETTING_INPUTS && limits && !(value >= limits[0] && value <= limits[1])) {
                const label = document.querySelector(`label[for="${WBR_SETTING_INPUTS[name]}"]`);
                return fail(`${label ? label.textContent : name} must be from ${limits[0]} to ${limits[1]}.`);
            }
        }
        if (next.wbr_months_before > next.wbr_months_scroll) return fail('Months Before Current cannot be more than the months available to scroll back.');
        if (next.wbr_weeks_before > next.wbr_weeks_scroll) return fail('Weeks Before Current cannot be more than the weeks available to scroll back.');
        const btn = document.getElementById('wbrSettingsSaveBtn');
        btn.disabled = true;
        try {
            blockSaveIfLoadFailed();
            // Budget edits first: if they fail, nothing else is saved and the popup stays open.
            // Then the charts' budget targets are read again, so a new mapping or figure shows on Save.
            if (typeof WbrBudgets !== 'undefined' && await WbrBudgets.save()) await loadWbrBudgetTargets();
            const resp = await fetch(BASE_URL + '/kpi/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ settings: next }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'save failed');
            const reload = WBR_LOAD_SETTINGS.some(k => Number(resp.settings[k]) !== Number(settings[k]));
            settings = resp.settings;
            applyAppearance();
            wbrShift.m = 0;
            wbrShift.w = 0;
            closeWbrSettings();
            if (reload) loadAll();
            else {
                shownRows().forEach(row => { if (customRowData[row.id]) renderCustomRow(row, customRowData[row.id]); });
                renderLayout();
            }
        } catch (err) {
            fail('Save failed: ' + err.message);
        } finally {
            btn.disabled = false;
        }
    }

    // Every chart on the review: the grid's and the slides' own copies.
    function wbrAllCharts() {
        return Object.values(rowCharts).flat().concat(typeof WbrDeck !== 'undefined' ? WbrDeck.charts() : []);
    }

    async function saveWbrItems() {
        blockSaveIfLoadFailed();
        const resp = await fetch(BASE_URL + '/weekly-business-review/items', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items: wbrItems }),
        });
        const json = await resp.json();
        if (json.status !== 'ok') throw new Error(json.message || 'Save failed');
    }

    // The page changes straight away and is put back if the save fails.
    async function changeWbrItems(items, what) {
        const before = wbrItems;
        wbrItems = items;
        renderLayout();
        try {
            await saveWbrItems();
        } catch (err) {
            if (wbrItems === items) { wbrItems = before; renderLayout(); }
            document.getElementById('statusText').textContent = `${what} failed: ${err.message}`;
            return false;
        }
        return true;
    }

    async function addWbrItem(ref, period) {
        // Slides view: onto the slide showing; a chart may be on any number of slides.
        if (typeof WbrDeck !== 'undefined' && WbrDeck.active()) { WbrDeck.addChart(ref, period); return; }
        const row = findRow(ref);
        if (!row || wbrItems.some(i => i.ref === ref && i.period === period)) return;
        const id = 'w' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        const ok = await changeWbrItems([...wbrItems, { id, ref, period }], 'Add');
        renderWbrPicker();
        if (!ok) return;
        // Already loaded for another of its periods: redraw now the card has its size.
        if (customRowData[ref]) renderCustomRow(row, customRowData[ref]);
        else loadCustomRow(row);
    }

    function removeWbrItem(id) {
        changeWbrItems(wbrItems.filter(i => i.id !== id), 'Remove');
    }

    function moveWbrItem(id, step) {
        const items = [...wbrItems];
        const a = items.findIndex(i => i.id === id), b = a + step;
        if (a < 0 || b < 0 || b >= items.length) return;
        [items[a], items[b]] = [items[b], items[a]];
        changeWbrItems(items, 'Move');
    }

    // Add Chart From KPI: every KPI chart by section, each period on its own.
    function openWbrPicker() {
        document.getElementById('wbrPickerSearch').value = '';
        renderWbrPicker();
        document.getElementById('wbrPickerModal').classList.add('open');
    }

    function closeWbrPicker() {
        document.getElementById('wbrPickerModal').classList.remove('open');
    }

    function renderWbrPicker() {
        const list = document.getElementById('wbrPickerList');
        if (!list) return;
        const q = document.getElementById('wbrPickerSearch').value.trim().toLowerCase();
        const deck = typeof WbrDeck !== 'undefined' && WbrDeck.active();
        const have = new Set(deck ? [] : wbrItems.map(i => i.ref + '|' + i.period));
        const html = SECTIONS.map(section => {
            const rows = layout.rows.filter(r => r.section === section && !r.hidden
                && (!q || `${rowTitle(r)} ${r.subtitle || ''}`.toLowerCase().includes(q)));
            if (!rows.length) return '';
            return `<div class="wbr-picker-section">${escHtml(section)}</div>` + rows.map(r => {
                const buttons = ['m', 'w', 'd'].map(k => {
                    const added = have.has(r.id + '|' + k);
                    return `<button type="button" class="chart-title-btn" ${added ? 'disabled' : ''}
                                    onclick="addWbrItem('${r.id}', '${k}')">
                                <i class="fas ${added ? 'fa-check' : 'fa-plus'}"></i> ${PERIOD_NAMES[k]}
                            </button>`;
                }).join('');
                const sub = r.subtitle ? ` <span class="text-muted">- ${escHtml(r.subtitle)}</span>` : '';
                return `<div class="wbr-picker-row"><span class="wbr-picker-title">${escHtml(rowTitle(r))}${sub}</span>${buttons}</div>`;
            }).join('');
        }).join('');
        list.innerHTML = html || '<div class="text-muted">No charts found.</div>';
    }

    // One slide per chart, in the review's order. The deck is created in the
    // Google account connected in Settings > Google. When it is ready the
    // status line links to it: a link the user clicks is never blocked as a
    // popup, and they stay on this page to see what happened.
    function exportWbrSlides() {
        const status = document.getElementById('statusText');
        const slides = wbrItems.map(item => {
            const row = findRow(item.ref);
            const chart = row && (rowCharts[item.ref] || [])[WBR_PERIOD_INDEX[item.period]];
            if (!chart) return null;
            return { title: item.title ? wbrItemTitle(item, row) : `${rowTitle(row)} - ${PERIOD_NAMES[item.period]}`,
                     subtitle: wbrItemSubtitle(item, row),
                     image: chart.toBase64Image('image/png', 1) };
        }).filter(Boolean);
        if (!slides.length) { status.textContent = 'Load the charts before exporting.'; return; }
        const btn = document.getElementById('slidesBtn');
        const icon = document.getElementById('slidesIcon');
        btn.disabled = true;
        icon.className = 'fas fa-spinner fa-spin';
        status.textContent = `Creating Google Slides (${slides.length} charts)...`;
        const done = () => {
            btn.disabled = false;
            icon.className = 'fas fa-file-powerpoint';
        };
        fetch(BASE_URL + '/weekly-business-review/export-slides', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slides }),
        })
            .then(r => r.json())
            .then(data => {
                done();
                if (data.status !== 'ok') { status.textContent = 'Google Slides: ' + data.message; return; }
                status.innerHTML = `Google Slides created - <a href="${escHtml(data.url)}" target="_blank" rel="noopener">Open Deck</a>`;
            })
            .catch(err => {
                done();
                status.textContent = 'Google Slides failed: ' + err;
            });
    }

    // The section tab showing. Remembered in this browser only, so each person
    // comes back to the tab they last used.
    const ACTIVE_SECTION_KEY = 'kpiActiveSection';
    let activeSection = null;
    let sectionTabNames = [];

    function showSection(i) {
        activeSection = sectionTabNames[i];
        try { localStorage.setItem(ACTIVE_SECTION_KEY, activeSection); } catch (e) {}
        renderLayout();
        redrawActiveSection();
    }

    // Charts on a tab that is not showing are drawn at zero width, so a
    // resize is not enough when the tab is opened: redraw them from the data
    // already loaded, once the tab is visible and has its real width.
    function redrawActiveSection() {
        requestAnimationFrame(() => {
            rowsInTab(activeSection)
                .filter(r => customRowData[r.id])
                .forEach(r => {
                    (rowCharts[r.id] || []).forEach(c => c && c.destroy());
                    delete rowCharts[r.id];
                    renderCustomRow(r, customRowData[r.id]);
                });
        });
    }

    function getRowElement(row) {
        if (!customRowEls[row.id]) customRowEls[row.id] = buildCustomRowElement(row);
        return customRowEls[row.id];
    }

    // Which chart rows this user has collapsed. Saved against their login on
    // the server, so it follows them to any browser and one person hiding a
    // row does not hide it for anyone else.
    const COLLAPSED_KEY = 'kpiCollapsedRows';
    let collapsedSet = new Set();
    function loadCollapsedRows(rows) {
        collapsedSet = new Set(rows);
        // Rows collapsed before this was saved on the server were kept in the
        // browser; carry them over once.
        let local = [];
        try { local = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '[]'); } catch (e) {}
        if (local.length) {
            local.forEach(id => collapsedSet.add(id));
            try { localStorage.removeItem(COLLAPSED_KEY); } catch (e) {}
            saveCollapsedRows();
        }
    }
    function isRowCollapsed(id) { return collapsedSet.has(id); }
    function saveCollapsedRows() {
        fetch(BASE_URL + '/kpi/collapsed-rows', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rows: [...collapsedSet] }),
        }).then(r => r.json()).then(j => {
            if (j.status !== 'ok') console.error('Collapsed rows save failed: ' + j.message);
        }).catch(err => console.error('Collapsed rows save failed', err));
    }

    // Collapse or expand every shown chart row in one section.
    function setSectionRowsCollapsed(section, collapse) {
        rowsInTab(section).forEach(r => {
            if (collapse) collapsedSet.add(r.id); else collapsedSet.delete(r.id);
            const el = customRowEls[r.id];
            if (el) el.classList.toggle('collapsed', collapse);
            if (!collapse) (rowCharts[r.id] || []).forEach(c => c && c.resize());
        });
        saveCollapsedRows();
    }

    function toggleRowCollapsed(id) {
        const el = customRowEls[id];
        if (!el) return;
        const collapsed = el.classList.toggle('collapsed');
        if (collapsed) collapsedSet.add(id); else collapsedSet.delete(id);
        saveCollapsedRows();
        // A chart drawn or resized while hidden has no size; fit it on showing.
        if (!collapsed) (rowCharts[id] || []).forEach(c => c && c.resize());
    }

    function rowTitle(row) {
        return row.title || row.metric;
    }

    // Daily charts showing the 7-day average instead of each day's figure.
    // Each person's own choice, saved against their login on the server like
    // collapsed rows, so it follows them to any browser.
    const ROLLING_KEY = 'kpiRolling7';
    let rollingSet = new Set();
    function loadRollingRows(rows) {
        rollingSet = new Set(rows);
        // Choices made before this was saved on the server were kept in the
        // browser; carry them over once.
        let local = [];
        try { local = JSON.parse(localStorage.getItem(ROLLING_KEY) || '[]'); } catch (e) {}
        if (local.length) {
            local.forEach(id => rollingSet.add(id));
            try { localStorage.removeItem(ROLLING_KEY); } catch (e) {}
            saveRollingRows();
        }
    }
    function rollingOn(id) { return rollingSet.has(id); }
    function saveRollingRows() {
        fetch(BASE_URL + '/kpi/rolling-rows', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rows: [...rollingSet] }),
        }).then(r => r.json()).then(j => {
            if (j.status !== 'ok') console.error('7-day average save failed: ' + j.message);
        }).catch(err => console.error('7-day average save failed', err));
    }

    function setRolling(id, on) {
        if (rollingOn(id) === on) return;
        if (on) rollingSet.add(id); else rollingSet.delete(id);
        saveRollingRows();
        const row = findRow(id);
        if (row) rebuildCustomRow(row, false);
    }

    // Each point as the average of that day and the 6 before it. Blank days
    // are left out of the average; the first 6 days have no full week before
    // them, so they are blank. ownDayOnly: a day blank itself stays blank —
    // today, unless the chart includes the current day, and future days —
    // rather than showing the 6 days before it (the chart's own figures only;
    // last year and the target still run through to the end).
    function rolling7(values, ownDayOnly = false) {
        if (!values) return values;
        return values.map((v, i) => {
            if (i < 6 || (ownDayOnly && (v === null || v === undefined))) return null;
            const week = values.slice(i - 6, i + 1).filter(v => v !== null && v !== undefined);
            return week.length ? week.reduce((a, b) => a + b, 0) / week.length : null;
        });
    }

    function buildCustomRowElement(row) {
        const el = document.createElement('div');
        el.className = 'chart-row';
        el.id = 'row-' + row.id;
        const title = rowTitle(row);
        // The period (monthly / weekly / daily) sits at the right of the title
        // line, apart from the title. The title h2 grows to push it across.
        const card = (period, suffix, dayScroller = false, headExtra = '') => chartCardHtml({
            canvasId: `cv-${row.id}-${suffix}`,
            titleId: `t-${row.id}-${suffix}`,
            title,
            subtitle: row.subtitle || '',
            dayScroller,
            legend: false,
            headExtra: `<span class="chart-period" id="p-${row.id}-${suffix}">${escHtml(period)}</span>${headExtra}`,
            headEnd: `<button type="button" class="chart-zoom-btn" title="Open this chart larger"
                              onclick="openChartZoom('${row.id}', '${suffix}', this)"><i class="fas fa-search-plus"></i></button>`,
        });
        // Daily / 7-day average switch on the daily chart (line charts only).
        const rolling = rollingOn(row.id);
        const rollingToggle = isStacked(row) ? '' : `
            <span class="labels-toggle" role="group" aria-label="Daily or 7-day average"
                  title="Show each day's figure, or the average of that day and the 6 before it">
                <button type="button" class="${rolling ? '' : 'active'}" onclick="setRolling('${row.id}', false)">Daily</button>
                <button type="button" class="${rolling ? 'active' : ''}" onclick="setRolling('${row.id}', true)">7-day avg</button>
            </span>`;
        const editButton = IS_ADMIN ? `
            <button type="button" class="chart-title-btn" onclick="openChartModal('${row.id}')">
                <i class="fas fa-pen"></i> Edit Chart
            </button>
            <button type="button" class="chart-title-btn" onclick="copyChartBelow('${row.id}')">
                <i class="fas fa-copy"></i> Copy Chart Below
            </button>
            <button type="button" class="chart-title-btn" title="Move these charts up the section" onclick="moveRow('${row.id}', -1)">
                <i class="fas fa-arrow-up"></i> Up
            </button>
            <button type="button" class="chart-title-btn" title="Move these charts down the section" onclick="moveRow('${row.id}', 1)">
                <i class="fas fa-arrow-down"></i> Down
            </button>` : '';
        // For everyone, in both views. See setDataLabels for who it changes.
        const labelsToggle = `
            <button type="button" class="chart-title-btn" onclick="openLabelPopup('${row.id}', this)"
                    title="${IS_ADMIN ? 'Data labels on or off, their scale (k / M) and decimals. Sets the default for everyone' : 'Data labels on or off, their scale (k / M) and decimals'}">
                <i class="fas fa-tag"></i> Data Labels
            </button>`;
        // Hide / show is for everyone; the title shows next to it so a
        // collapsed row still says what it is.
        const toggleButton = `
            <button type="button" class="chart-title-btn chart-collapse-btn" onclick="toggleRowCollapsed('${row.id}')">
                <i class="fas fa-chevron-down"></i>
                <span class="chart-collapse-label">${escHtml(title)}</span>
            </button>`;
        // Targets: for everyone, unless Settings > Targets makes it the
        // admin's only (the targets-off rule in the styles hides it).
        const targetsButton = `
            <button type="button" class="chart-title-btn targets-btn" onclick="openTargetModal('${row.id}')">
                <i class="fas fa-bullseye"></i> Targets
            </button>`;
        // My Charts, for everyone. Elsewhere: add (or a note it is already
        // there). On the My Charts tab: remove and reorder instead, and the
        // admin's section buttons are hidden as they act on the section.
        const myChartsButtons = `
            <button type="button" class="chart-title-btn not-my-charts add-my-chart-btn" onclick="addToMyCharts('${row.id}')">
                <i class="fas fa-plus"></i> Add To My Charts
            </button>
            <button type="button" class="chart-title-btn not-my-charts in-my-chart-btn" onclick="showSection(0)" title="Open My Charts">
                <i class="fas fa-check"></i> In My Charts
            </button>
            <span class="my-charts-only" style="gap: 8px;">
                <button type="button" class="chart-title-btn" onclick="removeFromMyCharts('${row.id}')">
                    <i class="fas fa-times"></i> Remove From My Charts
                </button>
                <button type="button" class="chart-title-btn" title="Move up My Charts" onclick="moveMyChart('${row.id}', -1)">
                    <i class="fas fa-arrow-up"></i> Up
                </button>
                <button type="button" class="chart-title-btn" title="Move down My Charts" onclick="moveMyChart('${row.id}', 1)">
                    <i class="fas fa-arrow-down"></i> Down
                </button>
            </span>`;
        if (isRowCollapsed(row.id)) el.classList.add('collapsed');
        el.innerHTML = `
            <div class="chart-row-actions">${toggleButton}${labelsToggle}${targetsButton}${myChartsButtons}${editButton ? `<span class="chart-row-admin not-my-charts">${editButton}</span>` : ''}</div>
            <div class="charts-row">
                ${card('Monthly', 'm')}
                ${card('Weekly', 'w')}
                ${card(isStacked(row) ? 'Daily' : '', 'd', true, rollingToggle)}
            </div>`;
        return el;
    }

    // Rebuild a row's DOM + charts after a config change.
    // refetch=true when the change affects the data (agg, target); otherwise re-render from cache.
    function rebuildCustomRow(row, refetch) {
        // On the review the row's cards sit in the grid, so they are redrawn
        // where they are rather than rebuilt (which would leave two copies).
        if (WBR) {
            const day = document.getElementById(`cv-${row.id}-d`);
            const toggle = day && day.closest('.chart-card').querySelector('.labels-toggle');
            if (toggle) toggle.querySelectorAll('button').forEach((b, i) => b.classList.toggle('active', (i === 1) === rollingOn(row.id)));
            if (refetch || !customRowData[row.id]) loadCustomRow(row);
            else renderCustomRow(row, customRowData[row.id]);
            return;
        }
        dropRowElement(row.id);
        renderLayout();
        if (refetch || !customRowData[row.id]) loadCustomRow(row);
        else renderCustomRow(row, customRowData[row.id]);
    }

    function dropRowElement(id) {
        if (chartZoom && chartZoom.rowId === id) closeChartZoom(false);
        (rowCharts[id] || []).forEach(c => c && c.destroy());
        delete rowCharts[id];
        const el = customRowEls[id];
        if (el) el.remove();
        delete customRowEls[id];
    }

    // ── Chart popup (magnifier) ──
    // The chart card itself moves into the popup, so its labels, targets,
    // notes, hover box and day scroller keep working; a blank box the same
    // size holds its place in the row until it is put back. The size it opens
    // at is in Settings > Appearance. CSV export is only offered here.
    const ZOOM_SIZES = [['s', 'Small', 50, 50], ['m', 'Medium', 70, 65], ['l', 'Large', 85, 80], ['f', 'Full Screen', 98, 94]];
    const PERIOD_NAMES = { m: 'Monthly', w: 'Weekly', d: 'Daily' };
    let chartZoom = null;   // { rowId, k, card, holder, overlay }

    function zoomChart() {
        return chartZoom && (rowCharts[chartZoom.rowId] || [])[{ m: 0, w: 1, d: 2 }[chartZoom.k]];
    }

    function openChartZoom(rowId, k, button) {
        closeChartZoom();
        const card = button.closest('.chart-card');
        if (!card) return;
        const holder = document.createElement('div');
        holder.className = 'chart-zoom-holder';
        holder.style.height = card.offsetHeight + 'px';
        card.replaceWith(holder);
        const overlay = document.createElement('div');
        overlay.className = 'chart-zoom-overlay';
        overlay.innerHTML = `
            <div class="chart-zoom-box" role="dialog" aria-label="Chart">
                <div class="chart-zoom-bar">
                    <span class="labels-toggle" role="group" aria-label="Popup size">
                        ${ZOOM_SIZES.map(([key, name]) => `<button type="button" data-zoom-size="${key}" onclick="setChartZoomSize('${key}')">${name}</button>`).join('')}
                    </span>
                    ${k === 'd' ? `
                    <label class="chart-zoom-days">Days Shown
                        <input type="number" id="zoomDays" min="7" max="${kpiDates.length}" step="1"
                               value="${Math.min(settings.days_window, kpiDates.length)}" onchange="setZoomDays(this.value)">
                    </label>
                    <button type="button" class="chart-title-btn" onclick="setZoomDays(kpiDates.length)">All ${kpiDates.length} Days</button>` : ''}
                    <span class="chart-zoom-gap"></span>
                    <button type="button" class="chart-title-btn" onclick="downloadChartCsv()" title="Download the figures in this chart as a CSV file">
                        <i class="fas fa-file-csv"></i> CSV
                    </button>
                    <button type="button" class="btn-close" title="Close" onclick="closeChartZoom()"></button>
                </div>
            </div>`;
        const box = overlay.querySelector('.chart-zoom-box');
        box.appendChild(card);
        box.insertAdjacentHTML('beforeend', '<div class="chart-zoom-hint">Or Drag The Bottom Right Corner To Resize</div>');
        overlay.addEventListener('mousedown', e => { if (e.target === overlay) closeChartZoom(); });
        document.body.appendChild(overlay);
        // offset: the page's day window position, put back on close.
        chartZoom = { rowId, k, card, holder, overlay, days: null, offset: DayWindow.offset };
        // Settings > Appearance: the size it opens at (Small unless changed).
        setChartZoomSize((ZOOM_SIZES[Number(settings.zoom_size) || 0] || ZOOM_SIZES[0])[0]);
    }

    // Daily chart in the popup: how many days, up to every day loaded. Only
    // while the popup is open; the page's own window comes back on close.
    function setZoomDays(n) {
        if (!chartZoom) return;
        const days = Math.min(Math.max(Math.round(Number(n)) || settings.days_window, 7), kpiDates.length);
        chartZoom.days = days;
        const input = document.getElementById('zoomDays');
        if (input) input.value = days;
        redrawDayWindow();
    }

    function zoomOnDaily() { return !!chartZoom && chartZoom.k === 'd'; }

    // The daily charts the day window moves: only the popup's while a daily
    // chart is open in it, so the page's charts do not move with it.
    function dayWindowCharts() {
        if (zoomOnDaily()) return [zoomChart()];
        const slides = WBR && typeof WbrDeck !== 'undefined' ? WbrDeck.charts() : [];
        return Object.values(rowCharts).map(charts => charts[2]).concat(slides);
    }

    function redrawDayWindow() {
        DayWindow.applyToCharts(dayWindowCharts());
        updateDayTitles();
        DayWindow.updateScrollers();
    }

    function setChartZoomSize(key) {
        const size = ZOOM_SIZES.find(s => s[0] === key);
        if (!chartZoom || !size) return;
        const box = chartZoom.overlay.querySelector('.chart-zoom-box');
        box.style.width = size[2] + 'vw';
        box.style.height = size[3] + 'vh';
        box.querySelectorAll('[data-zoom-size]').forEach(b => b.classList.toggle('active', b.dataset.zoomSize === key));
    }

    // restore = false when the row is being removed: the card goes with the popup.
    function closeChartZoom(restore = true) {
        if (!chartZoom) return;
        const chart = zoomChart();
        const { k, card, holder, overlay, offset } = chartZoom;
        chartZoom = null;
        if (restore && holder.isConnected) holder.replaceWith(card);
        else holder.remove();
        overlay.remove();
        // Back to the page's own day window: its position and number of days.
        if (k === 'd') {
            DayWindow.offset = offset;
            redrawDayWindow();
        }
        if (!restore || !chart) return;
        // The canvas is still drawn at the popup's size (its width / height
        // and its style), which would hold the row's grid column wide. Drop
        // both and fit the card again.
        const fit = () => {
            const canvas = chart.canvas;
            canvas.style.width = '';
            canvas.style.height = '';
            canvas.width = 0;
            canvas.height = 0;
            chart.resize();
        };
        fit();
        requestAnimationFrame(fit);
    }

    document.addEventListener('keydown', e => { if (e.key === 'Escape' && chartZoom) closeChartZoom(); });

    function csvCell(v) {
        const s = String(v ?? '');
        return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }

    // The figures in the popup's chart, as drawn: plain numbers (no £ % k M or
    // commas), rounded as set out below. Daily
    // charts give every loaded day, not just the days on screen; days with no
    // figure yet are blank, not 0.
    function downloadChartCsv() {
        const chart = zoomChart();
        const row = chartZoom && findRow(chartZoom.rowId);
        const d = row && customRowData[row.id];
        if (!chart || !d) return;
        const k = chartZoom.k;
        const stacked = isStacked(row);
        const full = chart.$dayFull;
        const labels = full ? full.labels : chart.data.labels;
        const starts = k === 'm' ? (d.monthly_starts || []).slice(wbrStart(row.id, k))
            : k === 'w' ? (d.weekly_starts || []).slice(wbrStart(row.id, k)) : d.dates;
        // Rounding: a figure from one column takes that column's Decimals
        // (Settings > Column Names), times 100 when its format is "% from a
        // decimal"; a figure worked out from several columns (divided, added,
        // multiplied, totals, 100% shares) takes the chart's own Decimals; a
        // column with no format keeps up to 6 decimals.
        const chartRule = { dec: row.decimals || 0, x100: !(stacked && row.pct) && fmtRow(row).fmt === 'pct100' };
        const columnRule = (source, metric) => {
            const f = metricFormats[metricKey(source || 'snowflake', metric)];
            return f && f.fmt ? { dec: f.decimals || 0, x100: f.fmt === 'pct100' } : { dec: 6, x100: false };
        };
        const ownRule = stacked || rowOwnTarget(row) || row.multiply_by ? chartRule : columnRule(rowSource(row), row.metric);
        const seriesRule = i => {
            const p = stacked ? plotSeries(row)[i] : null;
            const s = p && p.s;
            return !s || s.multiply_by || row.pct ? chartRule : columnRule(s.source, s.metric);
        };
        const sets = chart.data.datasets.map((ds, i) => ({ ds, data: full ? full.data[i] : ds.data,
            rule: stacked && ds.label !== 'Target' ? seriesRule(i) : ownRule }));
        const rolling = !stacked && k === 'd' && rollingOn(row.id);
        const name = ds => ds.label === 'N-1' ? 'Last Year'
            : rolling && ds.label !== 'Target' ? ds.label + ' (7-Day Average)' : ds.label;
        // The chart's own figures first, then last year, then the target.
        const rank = ds => ds.label === 'Target' ? 2 : ds.label === 'N-1' ? 1 : 0;
        const cols = [...sets].sort((a, b) => rank(a.ds) - rank(b.ds)).map(s => ({ name: name(s.ds), data: s.data, rule: s.rule }));
        // Column charts: the column total before the target (not on 100% charts).
        if (stacked && !row.pct) {
            const bars = sets.filter(s => s.ds.type === 'bar');
            const total = labels.map((_, i) => {
                const known = bars.map(s => s.data[i]).filter(v => v !== null && v !== undefined);
                return known.length ? known.reduce((a, b) => a + b, 0) : null;
            });
            const at = cols.findIndex(c => c.name === 'Target');
            cols.splice(at < 0 ? cols.length : at, 0, { name: 'Total', data: total, rule: chartRule });
        }
        const value = (v, rule) => {
            if (v === null || v === undefined || !isFinite(v)) return '';
            const scale = 10 ** rule.dec;
            return String(Math.round((rule.x100 ? v * 100 : v) * scale) / scale);
        };
        const head = k === 'd' ? ['Date'] : [k === 'w' ? 'Week' : 'Month', (k === 'w' ? 'Week' : 'Month') + ' Start'];
        const lines = [[...head, ...cols.map(c => c.name)]];
        labels.forEach((label, i) => {
            const when = k === 'd' ? [starts[i] || label] : [label, starts[i] || ''];
            lines.push([...when, ...cols.map(c => value(c.data[i], c.rule))]);
        });
        const text = lines.map(r => r.map(csvCell).join(',')).join('\r\n');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' }));
        a.download = `${rowTitle(row)} - ${PERIOD_NAMES[k]}.csv`.replace(/[\\/:*?"<>|]+/g, '-');
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }

    // Charts not hidden in Settings > Charts. Hidden ones stay saved but are
    // not drawn or loaded.
    // Charts in a section hidden in Settings > Sections are left out the same way.
    function shownRows() {
        // The review loads only the KPI charts it uses, hidden ones included.
        if (WBR) {
            // The grid's charts and every slide's.
            const refs = new Set(wbrItems.map(i => i.ref)
                .concat(typeof WbrDeck !== 'undefined' ? WbrDeck.refs() : []));
            return layout.rows.filter(r => refs.has(r.id));
        }
        return layout.rows.filter(r => !r.hidden && !hiddenSections.has(r.section));
    }

    // Sections hidden in Settings > Sections: no tab, charts not loaded.
    let hiddenSections = new Set();
    // { section: source id } from Settings > Sections: that section's charts
    // can only use that data source.
    let sectionSources = {};
    // { section: company } from Settings > Sections, for sections that are not
    // Carsupermarket.com. All-charts notes show only on their company's charts.
    let sectionCompanies = {};
    const COMPANIES = { carsupermarket: 'Carsupermarket.com', bcb: 'BCB' };
    const NOTE_COMPANIES = { ...COMPANIES, both: 'Both' };

    function sectionCompany(section) {
        return sectionCompanies[section] || 'carsupermarket';
    }

    function findRow(id) {
        return layout.rows.find(r => r.id === id);
    }

    function toggleSection(name) {
        const sec = document.getElementById(sectionDomId(name));
        const header = sec.querySelector('.category-header');
        const body = sec.querySelector('.category-body');
        const isOpen = body.classList.contains('open');
        header.classList.toggle('open', !isOpen);
        body.classList.toggle('open', !isOpen);
        if (!isOpen) setTimeout(resizeAllCharts, 10);
    }

    function resizeAllCharts() {
        (WBR ? wbrAllCharts() : Object.values(rowCharts).flat()).forEach(c => c && c.resize());
    }

    async function saveLayout(rows) {
        blockSaveIfLoadFailed();
        // Charts are only changed on the KPI page; the review saves its own list.
        if (WBR) throw new Error('Change charts on the KPI page');
        const resp = await fetch(BASE_URL + '/kpi/layout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ layout: { ...layout, rows } }),
        });
        const json = await resp.json();
        if (json.status !== 'ok') throw new Error(json.message || 'Save failed');
    }

    // ── Data loading ─────────────────────────────────────────────────────────
    // Which current (part-elapsed) periods a chart shows. Rows saved before the
    // day option hold a single true/false, meaning month and week.
    function rowCurrent(row) {
        const c = row.include_current;
        if (c && typeof c === 'object') return { m: !!c.m, w: !!c.w, d: !!c.d };
        return { m: !!c, w: !!c, d: false };
    }

    const CURRENT_INPUTS = { m: 'includeCurrentMonthInput', w: 'includeCurrentWeekInput', d: 'includeCurrentDayInput' };

    function setCurrentInputs(cur) {
        Object.entries(CURRENT_INPUTS).forEach(([k, id]) => { document.getElementById(id).checked = !!cur[k]; });
    }

    function readCurrentInputs() {
        const cur = {};
        Object.entries(CURRENT_INPUTS).forEach(([k, id]) => { cur[k] = document.getElementById(id).checked; });
        return cur;
    }

    // Every chart's data request made in the same moment (e.g. all of them on
    // Load Data) is collected and sent as one /kpi/custom-data-batch request,
    // which reads Snowflake once for the lot. The same metric asked for twice
    // with the same options is sent once and shared.
    let metricBatch = null;   // { items: [], waiters: Map(key -> {index, promise, resolve}) }

    // multiplyBy: { source, metric } or null — see Multiplied By in Edit Chart.
    // splitAt: { source, metric, part: 'lower' | 'upper' } - one part of a
    // series split at a second metric (Split At, kpi.py _custom_data).
    function fetchMetricData(source, metric, agg, targetMode, current = {}, weightBy = '', multiplyBy = null, splitAt = null) {
        const item = {
            source, metric, agg,
            target_mode: targetMode,
            current_month: !!current.m,
            current_week: !!current.w,
            current_day: !!current.d,
            weight_by: weightBy || '',
            multiply_source: multiplyBy ? (multiplyBy.source || 'snowflake') : '',
            multiply_metric: multiplyBy ? multiplyBy.metric : '',
            ...(splitAt ? { split_source: splitAt.source || 'snowflake', split_metric: splitAt.metric, split_part: splitAt.part } : {}),
        };
        if (!metricBatch) {
            metricBatch = { items: [], waiters: new Map() };
            setTimeout(sendMetricBatch, 0);
        }
        const key = JSON.stringify(item);
        let w = metricBatch.waiters.get(key);
        if (!w) {
            let resolve;
            const promise = new Promise(r => { resolve = r; });
            w = { index: metricBatch.items.length, promise, resolve };
            metricBatch.items.push(item);
            metricBatch.waiters.set(key, w);
        }
        // Each chart gets its own copy, so one changing it cannot affect another.
        return w.promise.then(d => structuredClone(d));
    }

    function sendMetricBatch() {
        const batch = metricBatch;
        metricBatch = null;
        const settle = results => batch.waiters.forEach(w => w.resolve(results(w.index)));
        fetch(`${BASE_URL}/kpi/custom-data-batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                items: batch.items,
                refresh: loadToken,
                sp_refresh: spRefreshToken,
                // The review loads its own range: back as far as its scroller
                // reaches, forward as far as its window shows.
                months: WBR ? Number(settings.wbr_months_scroll) + 1 : settings.months,
                weeks: WBR ? Number(settings.wbr_weeks_scroll) + 1 : settings.weeks,
                days: settings.days_total,
                months_after: WBR ? settings.wbr_months_after : settings.months_after,
                weeks_after: WBR ? settings.wbr_weeks_after : settings.weeks_after,
                days_after: settings.days_after,
            }),
        })
            .then(r => r.json())
            .then(d => {
                if (d.status !== 'ok') throw new Error(d.message || 'load failed');
                if (d.sp_keys) {
                    spPending = d.sp_pending || [];
                    spKeys = d.sp_keys;
                    spCheckSeconds = d.sp_check_seconds || spCheckSeconds;
                }
                settle(i => d.results[i]);
            })
            .catch(err => settle(() => ({ status: 'error', message: String(err.message || err) })));
    }

    function fetchRowData(row) {
        if (isStacked(row)) return fetchStackedData(row);
        const cur = rowCurrent(row);
        if (!rowOwnTarget(row)) {
            // Calculated: two other targets combined in the page (kpi_targets.js).
            const calc = rowTargetMode(row) === 'calc';
            const d = fetchMetricData(rowSource(row), row.metric, row.agg, calc ? 'none' : rowTargetMode(row), cur,
                                      row.weight_by || '', row.multiply_by || null);
            return calc ? d.then(data => withCalcTarget(row, data)) : d;
        }
        // Each side is its metrics added together. Errors from any metric but
        // the chart's own are named, so it is clear which one failed.
        // From Inputs: each metric fetched with its own target (source picked
        // per metric), then added and divided the same way as the figures.
        const inputs = row.target_mode === 'inputs' ? rowInputTargets(row) : null;
        const fetchSide = parts => Promise.all(parts.map((p, i) =>
            fetchMetricData(p.source || 'snowflake', p.metric, row.agg,
                inputs ? inputs[metricKey(p.source || 'snowflake', p.metric)] : 'none', cur).then(d =>
                d.status === 'ok' || (parts === top && i === 0) ? d : { ...d, message: `${p.metric}: ${d.message}` })))
            .then(d => {
                const combined = combineData(d, plusOps(parts.slice(1)));
                // Keep each metric's own series so the point popup can show
                // the figures the result was worked out from.
                return combined.status === 'ok'
                    ? { ...combined, side_parts: parts.map((p, i) => ({ name: p.metric, source: p.source || 'snowflake', op: i ? (p.op || '+') : '', data: d[i] })) }
                    : combined;
            });
        const top = [{ source: rowSource(row), metric: row.metric }, ...rowPlus(row)];
        // Per Date: the chart's own pasted figures (kpi_targets.js).
        const ownDates = d => (row.target_mode === 'import' ? withRowDateTargets(row, d) : d);
        if (!row.divide_by) {
            return fetchSide(top).then(d => {
                if (d.status !== 'ok' || inputs) return d;
                // Not divided, so the chart's Sum / Average decides, as on a
                // plain chart. Sum: the fixed figure is per day, and a week's
                // target is 7 days' worth, a month's that month's number of
                // days' worth. Average: the same figure on every point. (A
                // divided chart is a rate, so it is always the same figure on
                // every point whatever Sum / Average says: see ratioData.)
                const target = rowTargetFn(row);
                const sum = row.agg === 'sum';
                const daysInMonth = s => { const [y, m] = s.split('-').map(Number); return new Date(Date.UTC(y, m, 0)).getUTCDate(); };
                const times = (list, n) => (list ? list.map((v, i) => (v === null || !sum ? v : v * n(i))) : null);
                return ownDates({ ...d, values_target: target(d.dates),
                                  weekly_values_target: times(target(d.weekly_labels), () => 7),
                                  monthly_values_target: times(target(d.monthly_labels), i => daysInMonth(d.monthly_starts[i])) });
            });
        }
        return Promise.all([fetchSide(top), fetchSide([row.divide_by, ...rowDividePlus(row)])]).then(([num, den]) => {
            if (num.status !== 'ok') return num;
            if (den.status !== 'ok') return den;
            return ownDates(ratioData(row, num, den));
        });
    }

    // One side's figures for a single point: vals[0] ops[0] vals[1] ...,
    // times and divide before plus and minus, as in ordinary maths. Blank
    // (null) values: when blankIsZero, a blank in a plus or minus counts as 0
    // (a day with no calls still has emails); a blank next to a times or
    // divide makes the point blank, as does dividing by 0. The point is blank
    // when every value is.
    function evalTerms(vals, ops, blankIsZero) {
        const blank = v => v === null || v === undefined;
        if (vals.every(blank)) return null;
        if (!blankIsZero && vals.some(blank)) return null;
        // Group into products: each '+' / '-' starts a new term.
        const terms = [];
        let cur = { sign: 1, vals: [vals[0]], ops: [] };
        for (let i = 1; i < vals.length; i++) {
            const op = ops[i - 1] || '+';
            if (op === '+' || op === '-') {
                terms.push(cur);
                cur = { sign: op === '-' ? -1 : 1, vals: [vals[i]], ops: [] };
            } else {
                cur.vals.push(vals[i]);
                cur.ops.push(op);
            }
        }
        terms.push(cur);
        let total = 0;
        for (const t of terms) {
            if (t.vals.length === 1) { total += t.sign * (blank(t.vals[0]) ? 0 : t.vals[0]); continue; }
            if (t.vals.some(blank)) return null;
            let v = t.vals[0];
            for (let j = 0; j < t.ops.length; j++) {
                if (t.ops[j] === '*') v *= t.vals[j + 1];
                else if (t.vals[j + 1] === 0) return null;
                else v /= t.vals[j + 1];
            }
            total += t.sign * v;
        }
        return total;
    }

    // Several metrics' series combined point by point with the operators
    // chosen between them (all '+' on charts saved before operators). Each
    // period's figure is worked out from that period's totals, the same way
    // a divided chart is. Last year is only kept when every metric has it.
    function combineData(parts, ops = []) {
        const bad = parts.find(p => p.status !== 'ok');
        if (bad) return bad;
        if (parts.length === 1) return parts[0];
        const apply = (key, blankIsZero) => {
            const lists = parts.map(p => p[key]);
            if (lists.some(l => !l)) return null;
            return lists[0].map((_, i) => evalTerms(lists.map(l => l[i]), ops, blankIsZero));
        };
        const add = key => apply(key, true);
        // Targets only combine where every metric has one: a target missing
        // for one metric is unknown, not 0.
        const addAll = key => apply(key, false);
        const out = { ...parts[0], stale_date: parts.map(p => p.stale_date).find(Boolean) || parts[0].stale_date };
        ['values', 'values_n1', 'monthly_values', 'monthly_values_n1', 'weekly_values', 'weekly_values_n1']
            .forEach(key => { out[key] = add(key); });
        ['values_target', 'monthly_values_target', 'weekly_values_target']
            .forEach(key => { out[key] = addAll(key); });
        return out;
    }

    // The target line of a chart that keeps its own target: the row's fixed
    // figure on every point, or none. A ratio chart uses it as it is; an
    // added-metrics chart scales it up for weeks and months (fetchRowData).
    function rowTargetFn(row) {
        return row.target_mode === 'fixed' && row.ratio_target !== null && row.ratio_target !== undefined
            ? labels => labels.map(() => row.ratio_target)
            : () => null;
    }

    // A ratio chart's series: each point of the metric divided by the same
    // point of the divide-by metric. Both use the chart's Sum/Average, so a
    // Sum chart's month is the month total over the month total, not a sum
    // of daily ratios.
    function ratioData(row, num, den) {
        const scale = row.ratio_as === 'pct' ? 100 : 1;
        const divide = (a, b) => (a || []).map((v, i) => {
            const d = b ? b[i] : null;
            if (v === null || v === undefined || d === null || d === undefined || d === 0) return null;
            return v / d * scale;
        });
        const target = rowTargetFn(row);
        // From Inputs: a week's target is the week's numerator target over the
        // week's divide-by target, as for the actuals.
        const fromInputs = row.target_mode === 'inputs';
        const hasN1 = !!(num.values_n1 && den.values_n1);
        return {
            ...num,
            stale_date: num.stale_date || den.stale_date,
            calc: { num, den, scale },
            values: divide(num.values, den.values),
            values_n1: hasN1 ? divide(num.values_n1, den.values_n1) : null,
            values_target: fromInputs ? divide(num.values_target, den.values_target) : target(num.dates),
            monthly_values: divide(num.monthly_values, den.monthly_values),
            monthly_values_n1: num.monthly_values_n1 && den.monthly_values_n1
                ? divide(num.monthly_values_n1, den.monthly_values_n1) : null,
            monthly_values_target: fromInputs ? divide(num.monthly_values_target, den.monthly_values_target)
                : target(num.monthly_labels),
            weekly_values: divide(num.weekly_values, den.weekly_values),
            weekly_values_n1: divide(num.weekly_values_n1, den.weekly_values_n1),
            weekly_values_target: fromInputs ? divide(num.weekly_values_target, den.weekly_values_target)
                : target(num.weekly_labels),
        };
    }

    async function loadCustomRow(row) {
        const el = customRowEls[row.id];
        const status = document.getElementById('statusText');
        if (el) el.classList.add('charts-loading');
        status.textContent = `Loading ${rowTitle(row)}...`;
        try {
            const d = await fetchRowData(row);
            if (d.status !== 'ok') throw new Error(d.message || 'load failed');
            renderCustomRow(row, d);
            status.textContent = 'Loaded';
        } catch (err) {
            document.getElementById('statusText').textContent = `${rowTitle(row)}: ${err.message}`;
        } finally {
            if (el) el.classList.remove('charts-loading');
        }
    }

    // Large "Loading..." message in the middle of the page, used for whole-page
    // loads. A single chart reloading keeps its own smaller message instead.
    function setPageLoading(on) {
        document.getElementById('pageContent').classList.toggle('page-is-loading', on);
        if (on) document.getElementById('statusText').textContent = '';
    }

    // New on each Load Data click (blank on a normal page load), so the server
    // re-reads daily_summary's columns once then. A normal load uses the
    // server's cached list (10 minutes), which saves two Snowflake lookups.
    let loadToken = '';

    // Shown at the top of any section with "Autotrader" in its name: the
    // Car/Van advert allowance live from the Autotrader API (cars over it show
    // as Capped), then Published, Capped and Unpublished from this morning's
    // daily_summary figures.
    let atStripHtml = '';
    function showAtAllowance() {
        document.querySelectorAll('.at-strip').forEach(el => { el.innerHTML = atStripHtml; });
    }
    // Today's total allowance from the Autotrader API, for Settings > Autotrader.
    let atApiCapacity = null;

    function loadAtAllowance() {
        const num = v => (v === null || v === undefined) ? '-' : Number(v).toLocaleString('en-GB');
        const stat = (label, value, cls = '') => `<div class="at-stat${cls ? ' ' + cls : ''}">${label} <strong>${value}</strong></div>`;
        fetch(BASE_URL + '/kpi/at-allowance')
            .then(r => r.json())
            .then(d => {
                if (d.status !== 'ok') throw new Error(d.message);
                const allowance = 'capacity' in d ? (d.capacity === null ? 'Unlimited' : num(d.capacity)) : 'Unavailable';
                atApiCapacity = typeof d.capacity === 'number' ? d.capacity : null;
                const c = d.counts || {};
                const day = c.date ? new Date(c.date + 'T00:00:00').toLocaleDateString('en-GB') : null;
                atStripHtml = stat('Advert Allowance', allowance)
                    + stat('Published', num(c.published))
                    + stat('Capped', num(c.capped),
                        c.capped == null ? '' : Number(c.capped) > 0 ? 'at-stat-bad' : 'at-stat-good')
                    + stat('Unpublished', num(c.unpublished))
                    + `<span class="at-strip-note">${day ? 'Published, Capped and Unpublished as at the morning of ' + day : 'Counts unavailable'}</span>`;
            })
            .catch(() => { atStripHtml = stat('Advert Allowance', 'Unavailable'); })
            .finally(showAtAllowance);
    }

    // Set only when the Load Data button is clicked: the server then asks
    // SharePoint straight away whether each spreadsheet has been saved since
    // its copy, instead of waiting for the file's check interval.
    let spRefreshToken = '';

    // SharePoint files (Settings > SharePoint). A file saved since its copy is
    // downloaded in the background; the charts are drawn from the copy and
    // drawn again when the download has finished.
    let spPending = [];          // [{key, name}] being downloaded
    let spKeys = [];             // every KPI file's key
    let spCheckSeconds = 900;    // shortest check interval of those files
    let spPollTimer = null;
    let spIdleTimer = null;
    let spPollStarted = 0;
    let spSavedAt = {};          // key -> copy_saved_at last seen
    const SP_POLL_MS = 4000;
    const SP_POLL_GIVE_UP_MS = 10 * 60 * 1000;

    function spNote() {
        return spPending.length ? 'newer copy of ' + spPending.map(p => p.name).join(', ') + ' loading' : '';
    }

    function spStatus(keys, check) {
        const qs = new URLSearchParams({ keys: keys.join(','), check: check ? 'true' : 'false' });
        return fetch(`${BASE_URL}/api/sharepoint/status?${qs}`).then(r => r.json());
    }

    // Ask every few seconds until the files being downloaded have arrived (or
    // failed), then reload the charts once.
    function startSpPolling() {
        if (spPollTimer || !spPending.length) return;
        spPollStarted = Date.now();
        const tick = () => {
            spStatus(spPending.map(p => p.key), false)
                .then(d => {
                    if (d.status !== 'ok') throw new Error(d.message || 'status failed');
                    const open = d.files.filter(f => f.job && ['queued', 'running'].includes(f.job.status));
                    const failed = d.files.filter(f => f.job && f.job.status === 'failed');
                    d.files.forEach(f => { spSavedAt[f.source_key] = f.copy_saved_at; });
                    if (open.length && Date.now() - spPollStarted < SP_POLL_GIVE_UP_MS) {
                        spPollTimer = setTimeout(tick, SP_POLL_MS);
                        return;
                    }
                    spPollTimer = null;
                    spPending = [];
                    if (failed.length) {
                        document.getElementById('statusText').textContent =
                            'Refresh failed: ' + failed.map(f => `${f.name} - ${f.job.message}`).join(' | ');
                    }
                    if (failed.length < d.files.length) loadAll(false);
                })
                .catch(() => { spPollTimer = setTimeout(tick, SP_POLL_MS * 2); });
        };
        spPollTimer = setTimeout(tick, SP_POLL_MS);
    }

    // While the page is open, run the normal check on the files' own interval,
    // so a spreadsheet saved during the day shows without a reload.
    function scheduleSpIdleCheck() {
        clearTimeout(spIdleTimer);
        if (!spKeys.length) return;
        spIdleTimer = setTimeout(() => {
            if (document.hidden || spPollTimer) { scheduleSpIdleCheck(); return; }
            spStatus(spKeys, true)
                .then(d => {
                    if (d.status !== 'ok') return;
                    const open = d.files.filter(f => f.job && ['queued', 'running'].includes(f.job.status));
                    const changed = d.files.filter(f => spSavedAt[f.source_key] !== undefined
                                                     && f.copy_saved_at !== spSavedAt[f.source_key]);
                    d.files.forEach(f => { if (!open.includes(f)) spSavedAt[f.source_key] = f.copy_saved_at; });
                    if (open.length) {
                        spPending = open.map(f => ({ key: f.source_key, name: f.name }));
                        startSpPolling();
                    } else if (changed.length) {
                        loadAll(false);
                    }
                })
                .catch(() => {})
                .finally(scheduleSpIdleCheck);
        }, Math.max(60, spCheckSeconds) * 1000);
    }

    // Admin View's menu beside Load Data: a reload of just one thing.
    function toggleLoadMenu(e) {
        e.stopPropagation();
        document.getElementById('loadMenuList').classList.toggle('open');
    }
    document.addEventListener('click', () => {
        const list = document.getElementById('loadMenuList');
        if (list) list.classList.remove('open');
    });
    function loadMenu(kind) {
        document.getElementById('loadMenuList').classList.remove('open');
        if (kind === 'page') { location.reload(); return; }
        loadAll(kind === 'all' ? true : kind);
    }

    // fromButton: true (Load Data: Snowflake and SharePoint), false (a normal
    // load, using the server's copies), or one of the menu's 'snowflake' /
    // 'sharepoint' / 'cached'.
    function loadAll(fromButton = false) {
        const token = String(Date.now());
        loadToken = fromButton === true || fromButton === 'snowflake' ? token : '';
        spRefreshToken = fromButton === true || fromButton === 'sharepoint' ? token : '';
        if (!WBR) loadAtAllowance();
        const btn    = document.getElementById('loadBtn');
        const icon   = document.getElementById('loadIcon');
        const status = document.getElementById('statusText');
        const container = document.getElementById('sectionsContainer');
        const rows = shownRows();
        if (!rows.length) {
            btn.disabled = false;
            icon.classList.remove('spin');
            setPageLoading(false);
            status.textContent = !IS_ADMIN ? 'No charts yet.' : WBR ? 'No charts yet. Use Add Chart From KPI.' : 'No charts yet. Use Add Chart.';
            return;
        }
        btn.disabled = true;
        icon.classList.add('spin');
        container.classList.add('charts-loading');
        setPageLoading(true);

        Promise.all(rows.map(row => fetchRowData(row).catch(err => ({ status: 'error', message: String(err) }))))
            .then(results => {
                const errors = [];
                const staleDates = new Set();
                results.forEach((d, i) => {
                    if (d.status === 'ok') {
                        renderCustomRow(rows[i], d);
                        if (d.stale_date) staleDates.add(d.stale_date);
                    } else {
                        errors.push(`${rowTitle(rows[i])}: ${d.message}`);
                    }
                });
                const notes = [];
                if (staleDates.size) notes.push('SharePoint could not be checked - using ' + [...staleDates].join(', '));
                if (spPending.length) notes.push(spNote());
                if (errors.length) notes.push('errors: ' + errors.join(' | '));
                status.textContent = notes.length ? 'Loaded (' + notes.join('; ') + ')' : 'Loaded';
                startSpPolling();
                scheduleSpIdleCheck();
            })
            .catch(err => { status.textContent = 'Failed: ' + err; })
            .finally(() => {
                btn.disabled = false;
                icon.classList.remove('spin');
                container.classList.remove('charts-loading');
                setPageLoading(false);
            });
    }

    // Redraw every row from its last response, e.g. after the window or label
    // angle changes, which need no new data.
    function renderAllFromCache() {
        shownRows().forEach(row => {
            if (customRowData[row.id]) renderCustomRow(row, customRowData[row.id]);
        });
    }

    // ── Value formatting ─────────────────────────────────────────────────────
    // Thousands separated by commas: 1,000 and 1,000,000.
    function withCommas(v, decimals) {
        return v.toLocaleString('en-GB', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }

    // 'km' format: millions as M, thousands as k, smaller values as they are.
    // Picks the unit from the rounded value, so 999,960 at 1 decimal shows as
    // 1.0M rather than 1,000.0k.
    function withKOrM(v, decimals, numFmt) {
        const scale = 10 ** decimals;
        const rounded = x => Math.round(x * scale) / scale;
        if (Math.abs(rounded(v / 1000)) >= 1000) return numFmt(v / 1e6) + 'M';
        if (Math.abs(rounded(v)) >= 1000) return numFmt(v / 1000) + 'k';
        return numFmt(v);
    }

    // '£' before a value, after any minus sign: -£1,200 not £-1,200.
    function withPound(text) {
        return text.startsWith('-') ? '-£' + text.slice(1) : '£' + text;
    }

    // ── Budget targets (review only) ──
    // Settings > Budgets can map a budget line to a chart. On the review, that
    // chart's target becomes the budget: each month's figure as it is; for a
    // Sum chart a day gets its month's figure / days in the month and a week
    // the total of its days; for an Average, % or divided chart a day gets
    // its month's figure and a week the average of its days. A % budget line
    // (stored as 60 for 60%) is divided by 100 on a "from a decimal" chart.
    // Stacked charts keep their own targets. The KPI page never loads these.
    let wbrBudgetTargets = {};   // chart id -> { values: { 'YYYY-MM': number } }

    async function loadWbrBudgetTargets() {
        if (!WBR) return;
        try {
            const resp = await fetch(BASE_URL + '/weekly-business-review/budget-targets').then(r => r.json());
            wbrBudgetTargets = resp.status === 'ok' ? resp.targets || {} : {};
        } catch (err) {
            // Charts then show their KPI targets, as before budgets existed.
            console.warn('Budget targets did not load', err);
            wbrBudgetTargets = {};
        }
    }

    function wbrBudgetData(row, d) {
        const budget = WBR && wbrBudgetTargets[row.id];
        if (!budget) return d;
        const sum = row.agg === 'sum' && !row.pct && !row.divide_by;
        const scale = budget.is_percent && fmtRow(row).fmt === 'pct100' ? 0.01 : 1;
        const monthOf = iso => budget.values[iso.slice(0, 7)];
        const daysIn = iso => Number(lastDayOfMonthIso(iso.slice(0, 8) + '01').slice(8, 10));
        const perDay = iso => {
            const v = monthOf(iso);
            if (v === null || v === undefined) return null;
            return (sum ? v / daysIn(iso) : v) * scale;
        };
        const addDays = (iso, n) => {
            const t = new Date(iso + 'T00:00:00Z');
            t.setUTCDate(t.getUTCDate() + n);
            return t.toISOString().slice(0, 10);
        };
        const week = monday => {
            const days = [0, 1, 2, 3, 4, 5, 6].map(n => perDay(addDays(monday, n)));
            if (days.some(v => v === null)) return null;
            const total = days.reduce((a, b) => a + b, 0);
            return sum ? total : total / 7;
        };
        const month = first => {
            const v = monthOf(first);
            return v === null || v === undefined ? null : v * scale;
        };
        return {
            ...d,
            values_target: d.dates.map(perDay),
            weekly_values_target: (d.weekly_starts || []).map(week),
            monthly_values_target: (d.monthly_starts || []).map(month),
        };
    }

    // A divided chart shown as a Percentage (x 100) is already a %, so the
    // "from a decimal" Label Format would times it by 100 a second time
    // (0.5 showing as 5,000%). Read it as "already a %" instead.
    function fmtRow(row) {
        return row.divide_by && row.ratio_as !== 'dec' && row.fmt === 'pct100' ? { ...row, fmt: 'pct' } : row;
    }

    // labels = false for the hover box, which never takes the Data Labels popup's choices.
    function rowFormatter(row, labels = true) {
        if (labels) {
            const own = labelOptions(row);
            if (own.scale || own.decimals !== null) return labelFormatter(row, own);
        }
        row = fmtRow(row);
        const decimals = row.decimals || 0;
        if (row.fmt === 'pct') return v => withCommas(v, decimals) + '%';
        if (row.fmt === 'pct100') return v => withCommas(v * 100, decimals) + '%';
        if (row.fmt === 'k') return v => withCommas(v / 1000, decimals) + 'k';
        if (row.fmt === 'km') return v => withKOrM(v, decimals, x => withCommas(x, decimals));
        if (row.fmt === 'gbp') return v => withPound(withCommas(v, decimals));
        if (row.fmt === 'gbpkm') return v => withPound(withKOrM(v, decimals, x => withCommas(x, decimals)));
        return v => withCommas(v, decimals);
    }

    // ── Data Labels popup ──
    // The base unit is fixed by the chart's Label Format and cannot be changed
    // here: Number (Number, k, k/M), £ (Pounds, Pounds k/M) or % (both Percent
    // formats). The popup sets only the scale - Auto (k or M by each figure's
    // size), None, k or M (not on % charts) - and the decimals. Blank / null =
    // the chart's own Label Format and Decimals. The admin's choice is saved on
    // the chart (label_scale, label_decimals) as everyone's default; anyone
    // else's is kept in their browser, like Data Labels On / Off.
    const LABEL_OPTIONS_KEY = 'kpiLabelOptions';
    const BASE_UNIT_NAMES = { num: 'Number', gbp: 'Pounds (£)', pct: 'Percent (%)' };
    const LABEL_SCALES = [['', 'Chart Default'], ['auto', 'Auto (k / M By Size)'], ['none', 'None'], ['k', 'Thousands (k)'], ['M', 'Millions (M)']];

    function baseUnit(row) {
        const fmt = fmtRow(row).fmt;
        if (fmt === 'pct' || fmt === 'pct100') return 'pct';
        if (fmt === 'gbp' || fmt === 'gbpkm') return 'gbp';
        return 'num';
    }

    function ownLabelOptions() {
        try { return JSON.parse(localStorage.getItem(LABEL_OPTIONS_KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }

    // { scale: '' | 'auto' | 'none' | 'k' | 'M', decimals: null | 0-4 } for this user.
    function labelOptions(row) {
        const own = IS_ADMIN ? null : ownLabelOptions()[row.id];
        const src = own || { scale: row.label_scale, decimals: row.label_decimals };
        return { scale: baseUnit(row) === 'pct' ? '' : (src.scale || ''),
                 decimals: Number.isInteger(src.decimals) ? src.decimals : null };
    }

    function labelFormatter(row, own) {
        const r = fmtRow(row);
        const d = own.decimals !== null ? own.decimals : (r.decimals || 0);
        if (r.fmt === 'pct') return v => withCommas(v, d) + '%';
        if (r.fmt === 'pct100') return v => withCommas(v * 100, d) + '%';
        // The chart's own scale when only the decimals were changed.
        const scale = own.scale || ({ k: 'k', km: 'auto', gbpkm: 'auto' }[r.fmt] || 'none');
        const num = scale === 'k' ? v => withCommas(v / 1e3, d) + 'k'
            : scale === 'M' ? v => withCommas(v / 1e6, d) + 'M'
            : scale === 'auto' ? v => withKOrM(v, d, x => withCommas(x, d))
            : v => withCommas(v, d);
        return baseUnit(row) === 'gbp' ? v => withPound(num(v)) : num;
    }

    function closeLabelPopup() {
        const pop = document.getElementById('labelPopup');
        if (pop) pop.remove();
    }

    function openLabelPopup(rowId, button) {
        closeLabelPopup();
        const row = findRow(rowId);
        if (!row) return;
        const own = labelOptions(row);
        const unit = baseUnit(row);
        const pop = document.createElement('div');
        pop.id = 'labelPopup';
        pop.className = 'label-popup';
        pop.innerHTML = `
            <div class="label-popup-head">Data Labels
                <button type="button" class="btn-close" aria-label="Close" onclick="closeLabelPopup()"></button></div>
            <div class="labels-toggle mb-2" role="group">
                <button type="button" class="${dataLabelsOn(row) ? 'active' : ''}" onclick="setDataLabels('${row.id}', true); closeLabelPopup()">On</button>
                <button type="button" class="${dataLabelsOn(row) ? '' : 'active'}" onclick="setDataLabels('${row.id}', false); closeLabelPopup()">Off</button>
            </div>
            <div class="label-popup-note">Unit: <strong>${BASE_UNIT_NAMES[unit]}</strong> (set by the chart)</div>
            <label class="label-popup-field">Scale
                <select id="labelPopupScale" ${unit === 'pct' ? 'disabled title="Percent charts have no k / M"' : ''}>
                    ${LABEL_SCALES.map(([v, l]) => `<option value="${v}"${v === own.scale ? ' selected' : ''}>${escHtml(l)}</option>`).join('')}
                </select></label>
            <label class="label-popup-field">Decimals
                <select id="labelPopupDecimals">
                    <option value=""${own.decimals === null ? ' selected' : ''}>Chart Default (${fmtRow(row).decimals || 0})</option>
                    ${[0, 1, 2, 3, 4].map(n => `<option value="${n}"${own.decimals === n ? ' selected' : ''}>${n}</option>`).join('')}
                </select></label>
            <div class="label-popup-note">${IS_ADMIN ? 'Saved on the chart as everyone\'s default.' : 'Saved for you only, in this browser.'}</div>
            <div class="d-flex gap-2 mt-2">
                <button type="button" class="btn btn-primary btn-sm" onclick="applyLabelPopup('${row.id}')">Apply</button>
                <button type="button" class="btn btn-outline-secondary btn-sm" onclick="applyLabelPopup('${row.id}', true)">Reset</button>
            </div>`;
        document.body.appendChild(pop);
        const b = button.getBoundingClientRect();
        pop.style.top = (window.scrollY + b.bottom + 4) + 'px';
        pop.style.left = Math.max(8, Math.min(window.scrollX + b.left, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8)) + 'px';
    }

    // Close the popup on a click anywhere else or Escape.
    document.addEventListener('mousedown', e => {
        const pop = document.getElementById('labelPopup');
        if (pop && !pop.contains(e.target) && !e.target.closest('[onclick^="openLabelPopup"]')) closeLabelPopup();
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLabelPopup(); });

    async function applyLabelPopup(rowId, reset = false) {
        const row = findRow(rowId);
        if (!row) return;
        const scale = reset ? '' : document.getElementById('labelPopupScale').value;
        const decText = reset ? '' : document.getElementById('labelPopupDecimals').value;
        const decimals = decText === '' ? null : parseInt(decText, 10);
        closeLabelPopup();
        if (!IS_ADMIN) {
            const own = ownLabelOptions();
            if (!scale && decimals === null) delete own[rowId]; else own[rowId] = { scale, decimals };
            try { localStorage.setItem(LABEL_OPTIONS_KEY, JSON.stringify(own)); } catch (e) {}
            rebuildCustomRow(row, false);
            return;
        }
        // As Data Labels: redraw straight away, save in the background, put it back if the save fails.
        const before = layout.rows;
        const rows = before.map(r => {
            if (r.id !== rowId) return r;
            const next = { ...r };
            delete next.label_scale; delete next.label_decimals; delete next.label_precision;
            if (scale) next.label_scale = scale;
            if (decimals !== null) next.label_decimals = decimals;
            return next;
        });
        layout.rows = rows;
        rebuildCustomRow(findRow(rowId), false);
        try {
            await saveLayout(rows);
        } catch (err) {
            if (layout.rows === rows) {
                layout.rows = before;
                rebuildCustomRow(findRow(rowId), false);
            }
            document.getElementById('statusText').textContent = 'Data labels change failed: ' + err.message;
        }
    }

    // Hover box: the figure in full, whatever the Label Format and Decimals
    // (no k / M, up to 4 decimals). The % or £ stays, as it says what the
    // figure is; a decimal shown as a % is still shown as a %.
    // Hover Format / Hover Decimals in Edit Chart override this, apart from
    // the labels: a format gives that format at those decimals (0 if unset);
    // decimals alone give the figure in full at exactly those decimals.
    function rowFullFormatter(row, pct = false) {
        const hd = row.hover_decimals;
        if (row.hover_fmt && !pct) return rowFormatter({ ...row, fmt: row.hover_fmt, decimals: hd ?? 0 }, false);
        if (hd !== null && hd !== undefined) {
            const fixed = v => v.toLocaleString('en-GB', { minimumFractionDigits: hd, maximumFractionDigits: hd });
            row = fmtRow(row);
            if (pct || row.fmt === 'pct') return v => fixed(v) + '%';
            if (row.fmt === 'pct100') return v => fixed(v * 100) + '%';
            if (row.fmt === 'gbp' || row.fmt === 'gbpkm') return v => withPound(fixed(v));
            return fixed;
        }
        row = fmtRow(row);
        const full = v => v.toLocaleString('en-GB', { maximumFractionDigits: 4 });
        if (pct || row.fmt === 'pct') return v => full(v) + '%';
        if (row.fmt === 'pct100') return v => full(Math.round(v * 100 * 1e6) / 1e6) + '%';
        if (row.fmt === 'gbp' || row.fmt === 'gbpkm') {
            return v => withPound(v.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
        }
        return full;
    }

    // Axis ticks keep only the decimals they need (up to 3).
    // Hover Format / Hover Decimals from Edit Chart. Always both set (''/null
    // when unset), so clearing them replaces what the saved chart had.
    function hoverFields() {
        const dec = document.getElementById('hoverDecimalsSelect').value;
        return { hover_fmt: document.getElementById('hoverFmtSelect').value,
                 hover_decimals: dec === '' ? null : parseInt(dec, 10) };
    }

    function rowTickFormatter(row) {
        row = fmtRow(row);
        const fmt = v => v.toLocaleString('en-GB', { maximumFractionDigits: 3 });
        if (row.fmt === 'pct') return v => fmt(v) + '%';
        // Rounded so 1.1 x 100 shows as 110%, not 110.00000000000001%.
        if (row.fmt === 'pct100') return v => fmt(Math.round(v * 100 * 1000) / 1000) + '%';
        if (row.fmt === 'k') return v => fmt(v / 1000) + 'k';
        if (row.fmt === 'km') return v => withKOrM(v, 3, fmt);
        if (row.fmt === 'gbp') return v => withPound(fmt(v));
        if (row.fmt === 'gbpkm') return v => withPound(withKOrM(v, 3, fmt));
        return v => fmt(v);
    }

    function renderCustomRow(row, d) {
        customRowData[row.id] = d;
        if (!document.getElementById(`cv-${row.id}-m`)) return;   // row removed meanwhile
        kpiDates = d.dates;
        // The review draws only each month/week chart's window; the full data
        // stays in customRowData.
        const loaded = d;
        if (WBR) { d = wbrSliceData(row, d); updateWbrSliders(); }
        const existing = rowCharts[row.id] || [];
        rowCharts[row.id] = buildRowCharts(row, d, ['m', 'w', 'd'].map((k, i) => ({
            k, canvasId: `cv-${row.id}-${k}`, existing: existing[i], first: k === 'd' ? 0 : wbrStart(row.id, k) })));
        updateDayTitles();
        DayWindow.updateScrollers();
        // Slides draw their own copies of the row's charts (wbr_deck.js).
        if (WBR && typeof WbrDeck !== 'undefined') WbrDeck.rowLoaded(row, loaded);
    }

    // A row's charts, one per target: { k: 'm'|'w'|'d', canvasId, existing
    // (chart to replace), first (index in the loaded data of the first point
    // drawn, for the point popup) }. Returns the charts in the targets' order.
    // The ranges always come from all three periods, so a chart drawn on its
    // own is scaled as it is on the KPI page.
    function buildRowCharts(row, d, targets) {
        if (isStacked(row)) return buildStackedCharts(row, d, targets);
        d = wbrBudgetData(row, d);
        // 7-day average on the daily chart only; the cached data is kept as
        // loaded so switching back needs no reload.
        if (rollingOn(row.id)) {
            d = { ...d, values: rolling7(d.values, true), values_n1: rolling7(d.values_n1),
                  values_target: rolling7(d.values_target) };
        }

        const nulls = arr => arr.map(() => null);
        const dailyLabels = d.dates.map(dateLabel);
        const valuesPast = d.values.map((v, i) => d.dates[i] <= d.today ? v : null);
        const opts = {
            seriesLabel: rowTitle(row),
            fmt: rowFormatter(row),
            fullFmt: rowFullFormatter(row),
            tickFmt: rowTickFormatter(row),
            invert: !!row.invert,
            dataLabels: dataLabelsOn(row),
        };

        const ranges = calcRowRanges(rangeSpecs(d, row.y_min, row.y_max), row.agg,
            sharedAxisValue(row.y_min), sharedAxisValue(row.y_max));

        return targets.map(({ k, canvasId, existing, first }) => {
            const notes = (starts) => ({ rowId: row.id, kind: k, starts, first: first || 0 });
            if (k === 'm') return buildMetricChart(canvasId, d.monthly_labels, d.monthly_values,
                d.monthly_values_n1, d.monthly_values_target || nulls(d.monthly_labels),
                existing, { ...opts, range: ranges[0], notes: notes(d.monthly_starts || []) });
            if (k === 'w') return buildMetricChart(canvasId, d.weekly_labels, d.weekly_values,
                d.weekly_values_n1, d.weekly_values_target || nulls(d.weekly_labels),
                existing, { ...opts, n1Labels: d.weekly_n1_labels, range: ranges[1], notes: notes(d.weekly_starts || []) });
            return buildMetricChart(canvasId, dailyLabels, valuesPast,
                d.values_n1, d.values_target || nulls(d.dates),
                existing, { ...opts, range: ranges[2], day: true, notes: notes(d.dates) });
        });
    }

    // The month/week/day series and axis limits that set a row's ranges. Day
    // range comes from the WHOLE day series, so the axis stays put as the
    // window scrolls; only the plotted slice is windowed.
    function rangeSpecs(d, yMin, yMax) {
        yMin = yMin || {};
        yMax = yMax || {};
        const valuesPast = d.values.map((v, i) => d.dates[i] <= d.today ? v : null);
        return [
            { series: [d.monthly_values, d.monthly_values_n1], min: yMin.m, max: yMax.m },
            { series: [d.weekly_values, d.weekly_values_n1],   min: yMin.w, max: yMax.w },
            { series: [valuesPast, d.values_n1],               min: yMin.d, max: yMax.d },
        ];
    }

    // An Average chart's one axis minimum or maximum (from y_min / y_max). It
    // is stored in all three period slots; the first one set is used, so a
    // chart switched from Sum still picks up whatever was entered.
    function sharedAxisValue(byPeriod) {
        byPeriod = byPeriod || {};
        return ['m', 'w', 'd'].map(k => byPeriod[k]).find(v => v !== null && v !== undefined) ?? null;
    }

    // A typed maximum replaces the worked-out top of the axis. Ignored if it
    // is not above the bottom, which the chart box does not allow anyway.
    function withFixedMax(range, max) {
        if (max === null || max === undefined || max <= range.yMin) return range;
        // Chart.js steps up from the minimum and then adds the maximum as a
        // last tick, so a step that does not divide the gap leaves one uneven
        // gap at the top (600 to 1,400 in 200s, then 1,500). Use a round step
        // that divides it exactly, 4 to 10 gaps, nearest 6. If none does, keep
        // the usual step and lift the top to the next step.
        const span = max - range.yMin;
        const pow = Math.pow(10, Math.floor(Math.log10(span)));
        const exact = [pow / 100, pow / 10, pow, pow * 10].flatMap(p => [1, 2, 2.5, 5].map(m => m * p))
            .map(u => ({ u, n: span / u }))
            .filter(c => c.n >= 4 && c.n <= 10 && Math.abs(c.n - Math.round(c.n)) < 1e-9)
            .sort((a, b) => Math.abs(a.n - 6) - Math.abs(b.n - 6))[0];
        if (exact) return { yMin: range.yMin, yMax: max, unit: exact.u };
        const unit = _niceUnit(span);
        return { yMin: range.yMin, yMax: range.yMin + Math.ceil(span / unit - 1e-9) * unit, unit };
    }

    // Ranges for a row's month/week/day charts, scaled from the real data only
    // — targets do not stretch the axis, so an off-scale target line is clipped
    // (its background is clamped, and its value stays in the tooltip).
    //
    // 'sum': each period has its own scale and its own minimum, because a month
    // total is several times a week total.
    // 'avg' (averages, %, rates): month, week and day are the same kind of
    // number, so all three get ONE scale — one minimum (sharedMin) and one
    // maximum worked out from all three periods' data together, keeping that
    // data central.
    // A typed maximum (spec.max / sharedMax) overrides the worked-out top.
    //
    // The axis is never forced up to 1 when there is data above zero: a small
    // figure such as a decimal ratio (0.004 emails and calls per advert view)
    // would otherwise sit flat along the bottom of a 0 to 1 axis. The 1 only
    // guards an all-zero series.
    function calcRowRanges(specs, aggType = 'sum', sharedMin = null, sharedMax = null) {
        const values = spec => (spec.series || []).flatMap(_cleanVals);
        const range = (vals, min) => offsetRange(vals, min, vals.some(v => v > (min || 0)) ? 0 : 1);
        if (aggType !== 'avg') return specs.map(spec => withFixedMax(range(values(spec), spec.min), spec.max));
        const shared = withFixedMax(range(specs.flatMap(values), sharedMin), sharedMax);
        return specs.map(() => shared);
    }

    // A line round all four sides of the plot area. Chart.js only draws its
    // axis lines (left and bottom), so those are turned off and this draws
    // the whole frame in the same colour instead.
    const PLOT_FRAME_PLUGIN = {
        id: 'plotFrame',
        afterDraw(chart) {
            const { ctx, chartArea: a } = chart;
            if (!a) return;
            ctx.save();
            ctx.strokeStyle = 'rgba(0,0,0,0.1)';
            ctx.lineWidth = 1;
            ctx.strokeRect(Math.round(a.left) + 0.5, Math.round(a.top) + 0.5,
                Math.round(a.right - a.left) - 1, Math.round(a.bottom - a.top) - 1);
            ctx.restore();
        },
    };

    // ── Chart notes: dotted lines + hover popup ──────────────────────────────
    const NOTE_COLOUR = '#7c3aed';

    function addDaysIso(iso, n) {
        const dt = new Date(iso + 'T00:00:00Z');
        dt.setUTCDate(dt.getUTCDate() + n);
        return dt.toISOString().slice(0, 10);
    }

    // Index of the point a note's date falls in: the day itself, the week
    // (Monday to Sunday) or the month containing it. -1 when not on the chart.
    function notePointIndex(kind, starts, date) {
        if (kind === 'd') return starts.indexOf(date);
        if (kind === 'm') return starts.findIndex(s => s.slice(0, 7) === date.slice(0, 7));
        return starts.findIndex(s => s <= date && date < addDaysIso(s, 7));
    }

    // A note picked for one chart shows on that chart; an all-charts note on
    // every chart in its company's sections (no company = Carsupermarket.com).
    function notesForRow(rowId) {
        const row = findRow(rowId);
        const company = sectionCompany(row ? row.section : null);
        return chartNotes.filter(n => n.row_id ? n.row_id === rowId
            : ['both', company].includes(n.company || 'carsupermarket'));
    }

    // meta = { rowId, kind: 'd'|'w'|'m', starts: first date of each point }.
    // The day chart plots a window of its dates, so its index is shifted by
    // the window start and read at draw time, following the scroller.
    function makeNotesPlugin(meta) {
        return {
            id: 'chartNotes',
            afterDatasetsDraw(chart) {
                // Notes can be turned off for the whole Weekly Business Review.
                if (WBR && Number(settings.wbr_notes) === 0) { chart.$notePositions = []; return; }
                const { ctx, chartArea: a, scales: { x } } = chart;
                const offset = meta.kind === 'd' ? DayWindow.startIndex() : 0;
                const count = chart.data.labels.length;
                // Week and month columns: the line sits across the column in
                // proportion to where the note's date falls in the period (a
                // Monday note near the left of a week, the 15th near the middle
                // of a month). Days: the centre. Notes on the same date share a line.
                const slot = count > 1 ? x.getPixelForValue(1) - x.getPixelForValue(0) : x.width;
                const dayMs = 864e5;
                const byPx = new Map();
                notesForRow(meta.rowId).forEach(note => {
                    const n = notePointIndex(meta.kind, meta.starts, note.date);
                    const i = n - offset;
                    if (i < 0 || i >= count) return;
                    let frac = 0.5;
                    const start = meta.starts && meta.starts[n];
                    if (meta.kind !== 'd' && start) {
                        const s = String(start).slice(0, 10);
                        const days = meta.kind === 'w' ? 7 : Number(lastDayOfMonthIso(s).slice(8, 10));
                        const into = Math.round((Date.parse(note.date + 'T00:00:00Z') - Date.parse(s + 'T00:00:00Z')) / dayMs);
                        frac = Math.min(Math.max((into + 0.5) / days, 0), 1);
                    }
                    // Kept inside the column so it never sits on the next one.
                    const px = Math.round(x.getPixelForValue(i) + (frac - 0.5) * slot * 0.9) + 0.5;
                    if (!byPx.has(px)) byPx.set(px, []);
                    byPx.get(px).push(note);
                });
                chart.$notePositions = [];
                if (!byPx.size) return;
                ctx.save();
                ctx.strokeStyle = NOTE_COLOUR;
                ctx.fillStyle = NOTE_COLOUR;
                ctx.lineWidth = 1.5;
                ctx.setLineDash([3, 3]);
                byPx.forEach((notes, px) => {
                    ctx.beginPath();
                    ctx.moveTo(px, a.top);
                    ctx.lineTo(px, a.bottom);
                    ctx.stroke();
                    // Small marker at the top so the line reads as a note.
                    ctx.beginPath();
                    ctx.moveTo(px - 4, a.top);
                    ctx.lineTo(px + 4, a.top);
                    ctx.lineTo(px, a.top + 6);
                    ctx.closePath();
                    ctx.fill();
                    chart.$notePositions.push({ px, notes });
                });
                ctx.restore();
            },
            afterEvent(chart, args) {
                const e = args.event;
                if (e.type === 'mouseout') { hideNoteTip(); return; }
                if (e.type !== 'mousemove') return;
                const a = chart.chartArea;
                const hit = e.y >= a.top && e.y <= a.bottom
                    ? (chart.$notePositions || []).find(p => Math.abs(p.px - e.x) <= 5) : null;
                if (!hit) { hideNoteTip(); return; }
                const rect = chart.canvas.getBoundingClientRect();
                showNoteTip(hit.notes, rect.left + hit.px, rect.top + e.y);
            },
        };
    }

    function showNoteTip(notes, clientX, clientY) {
        const tip = document.getElementById('noteTip');
        tip.innerHTML = [...notes].sort((p, q) => p.date.localeCompare(q.date)).map(n => `
            <div class="note-tip-item">
                <div><span class="note-tip-date">${escHtml(dateLabel(n.date))}</span><span class="note-tip-title">${escHtml(n.title)}</span></div>
                ${n.text ? `<div class="note-tip-text">${escHtml(n.text)}</div>` : ''}
            </div>`).join('');
        tip.style.display = 'block';
        // Right of the line, flipped to the left near the screen edge.
        const w = tip.offsetWidth, h = tip.offsetHeight;
        const left = clientX + 12 + w > window.innerWidth ? clientX - 12 - w : clientX + 12;
        const top = Math.min(Math.max(8, clientY - h / 2), window.innerHeight - h - 8);
        tip.style.left = left + 'px';
        tip.style.top = top + 'px';
    }

    function hideNoteTip() {
        document.getElementById('noteTip').style.display = 'none';
    }

    function redrawAllCharts() {
        Object.values(rowCharts).flat().forEach(c => c && c.update('none'));
    }

    // ── Notes popup (admin) ──
    let noteEditingId = null;

    function openNotesModal() {
        document.getElementById('notesError').classList.remove('show');
        resetNoteForm();
        renderNotesList();
        document.getElementById('notesModal').classList.add('open');
    }

    function closeNotesModal() {
        document.getElementById('notesModal').classList.remove('open');
    }

    function noteRowOptions(selected) {
        const opts = ['<option value="">All charts</option>'].concat(
            layout.rows.map(r => `<option value="${escHtml(r.id)}">${escHtml(rowTitle(r))}</option>`));
        const select = document.getElementById('noteRow');
        select.innerHTML = opts.join('');
        select.value = selected || '';
    }

    function resetNoteForm() {
        noteEditingId = null;
        document.getElementById('noteFormTitle').textContent = 'Add Note';
        document.getElementById('noteSaveBtn').textContent = 'Add Note';
        document.getElementById('noteCancelEditBtn').style.display = 'none';
        document.getElementById('noteDate').value = new Date().toISOString().slice(0, 10);
        document.getElementById('noteTitle').value = '';
        document.getElementById('noteText').value = '';
        document.getElementById('noteCompany').value = 'carsupermarket';
        noteRowOptions('');
    }

    function editNote(id) {
        const note = chartNotes.find(n => n.id === id);
        if (!note) return;
        noteEditingId = id;
        document.getElementById('noteFormTitle').textContent = 'Edit Note';
        document.getElementById('noteSaveBtn').textContent = 'Save Note';
        document.getElementById('noteCancelEditBtn').style.display = '';
        document.getElementById('noteDate').value = note.date;
        document.getElementById('noteTitle').value = note.title;
        document.getElementById('noteText').value = note.text || '';
        document.getElementById('noteCompany').value = note.company || 'carsupermarket';
        noteRowOptions(note.row_id);
    }

    function renderNotesList() {
        const list = document.getElementById('notesList');
        if (!chartNotes.length) {
            list.innerHTML = '<div class="notes-empty">No notes yet.</div>';
            return;
        }
        const scope = n => {
            if (!n.row_id) return `All charts - ${NOTE_COMPANIES[n.company || 'carsupermarket']}`;
            const row = findRow(n.row_id);
            return row ? rowTitle(row) : '(chart removed)';
        };
        list.innerHTML = [...chartNotes].sort((p, q) => q.date.localeCompare(p.date)).map(n => `
            <div class="note-item">
                <div class="note-item-main">
                    <div class="note-item-head">
                        <span class="note-item-date">${escHtml(dateLabel(n.date))} ${escHtml(n.date.slice(0, 4))}</span>${escHtml(n.title)}
                        <span class="note-item-scope">${escHtml(scope(n))}</span>
                    </div>
                    ${n.text ? `<div class="note-item-text">${escHtml(n.text)}</div>` : ''}
                </div>
                <button type="button" class="btn btn-edit" onclick="editNote('${escHtml(n.id)}')">Edit</button>
                <button type="button" class="btn btn-remove-chart" onclick="deleteNote('${escHtml(n.id)}')">Delete</button>
            </div>`).join('');
    }

    async function saveNotes(next) {
        blockSaveIfLoadFailed();
        const resp = await fetch(BASE_URL + '/kpi/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes: next }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
        chartNotes = next;
        renderNotesList();
        redrawAllCharts();
    }

    function showNotesError(msg) {
        const box = document.getElementById('notesError');
        box.textContent = msg;
        box.classList.add('show');
    }

    async function saveNote() {
        document.getElementById('notesError').classList.remove('show');
        const note = {
            id: noteEditingId || 'n' + Date.now().toString(36),
            date: document.getElementById('noteDate').value,
            title: document.getElementById('noteTitle').value.trim(),
            text: document.getElementById('noteText').value.trim(),
            row_id: document.getElementById('noteRow').value || null,
            company: document.getElementById('noteCompany').value,
        };
        if (!note.date) { showNotesError('Choose a date.'); return; }
        if (!note.title) { showNotesError('Enter a title.'); return; }
        const next = noteEditingId
            ? chartNotes.map(n => n.id === noteEditingId ? note : n)
            : [...chartNotes, note];
        const button = document.getElementById('noteSaveBtn');
        button.disabled = true;
        try {
            await saveNotes(next);
            resetNoteForm();
        } catch (err) {
            showNotesError('Save failed: ' + err.message);
        } finally {
            button.disabled = false;
        }
    }

    async function deleteNote(id) {
        const note = chartNotes.find(n => n.id === id);
        if (!note || !confirm(`Delete the note "${note.title}"?`)) return;
        try {
            await saveNotes(chartNotes.filter(n => n.id !== id));
            if (noteEditingId === id) resetNoteForm();
        } catch (err) {
            showNotesError('Delete failed: ' + err.message);
        }
    }

    // ── Point popup and View Vehicles ────────────────────────────────────────
    // Clicking a chart point shows its figures and a View Vehicles button. The
    // vehicles are only fetched when the button is pressed: one query for that
    // day, week or month, using the chart's saved date column and filters.
    let pointState = null;   // { row, start, end, data, sort: { index, dir } }

    function lastDayOfMonthIso(firstIso) {
        const [y, m] = firstIso.split('-').map(Number);
        return new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    }

    function ukDate(iso) {
        return `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}`;
    }

    // meta = { rowId, kind: 'd'|'w'|'m', starts } as for the notes plugin. The
    // day chart plots a window of its dates, so its index is shifted by the
    // window start.
    function openPointModal(meta, chart, localIndex) {
        const row = findRow(meta.rowId);
        if (!row) return;
        const index = localIndex + (meta.kind === 'd' ? DayWindow.startIndex() : 0);
        const start = meta.starts[index];
        if (!start) return;
        const end = meta.kind === 'd' ? start : meta.kind === 'w' ? addDaysIso(start, 6) : lastDayOfMonthIso(start);
        pointState = { row, start, end, data: null, sort: null, filterVals: {} };
        const quickBar = document.getElementById('vehiclesQuick');
        quickBar.classList.remove('show');
        quickBar.innerHTML = '';

        const period = { d: 'daily', w: 'weekly', m: 'monthly' }[meta.kind];
        document.getElementById('pointTitle').textContent = `${rowTitle(row)} - ${period}`;
        document.getElementById('pointPeriod').textContent = start === end
            ? `${chart.data.labels[localIndex]} - ${ukDate(start)}`
            : `${chart.data.labels[localIndex]} - ${ukDate(start)} to ${ukDate(end)}`;

        let fmt = rowFormatter(row);
        let figures;
        if (isStacked(row)) {
            // Each series, then the column total (not on a 100% chart, where
            // every column is 100), then the target.
            if (row.pct) fmt = v => withCommas(v, row.decimals || 0) + '%';
            figures = chart.data.datasets.filter(ds => ds.type === 'bar')
                .map(ds => ({ label: ds.label, v: ds.data[localIndex] }));
            if (!row.pct) {
                const known = figures.map(f => f.v).filter(v => v !== null && v !== undefined && !isNaN(v));
                figures.push({ label: 'Total', v: known.length ? known.reduce((a, b) => a + b, 0) : null });
            }
            const target = chart.data.datasets.find(ds => ds.label === 'Target');
            if (target) figures.push({ label: 'Target', v: target.data[localIndex] });
        } else {
            const name = label => label === 'N-1' || label === 'Target' ? label : 'Value';
            figures = chart.data.datasets
                .map(ds => ({ label: name(ds.label), v: ds.data[localIndex] }))
                .sort((p, q) => ['Value', 'Target', 'N-1'].indexOf(p.label) - ['Value', 'Target', 'N-1'].indexOf(q.label));
        }
        document.getElementById('pointFigures').innerHTML = figures
            .map(f => `<span>${escHtml(f.label)}: <b>${f.v === null || f.v === undefined || isNaN(f.v) ? '-' : escHtml(fmt(f.v))}</b></span>`)
            .join('');

        // A review chart or slide may draw a window of the loaded points.
        renderPointCalc(row, meta.kind, meta.kind === 'd' ? index : index + (meta.first || 0), figures);

        const button = document.getElementById('viewVehiclesBtn');
        button.style.display = row.vehicles ? '' : 'none';
        button.disabled = false;
        document.getElementById('pointStatus').textContent = row.vehicles ? ''
            : 'View Vehicles is not set up for this chart.' + (isUserView() ? '' : ' Choose its date column in Edit Chart.');
        document.getElementById('vehiclesWrap').classList.remove('show');
        document.querySelector('#pointModal .point-modal').classList.remove('has-vehicles');
        document.getElementById('vehiclesTable').innerHTML = '';
        document.getElementById('vehiclesCsvBtn').style.display = 'none';
        document.getElementById('pointModal').classList.add('open');
    }

    // For a calculated chart (added metrics and/or divided by another), lists
    // every figure the point was worked out from, then the result: e.g.
    // "Orders: 120", "Calls + Emails: 400", "120 / 400 = 30%".
    function renderPointCalc(row, kind, index, figures) {
        const box = document.getElementById('pointCalc');
        box.innerHTML = '';
        box.classList.remove('show');
        const d = customRowData[row.id];
        if (!d || isStacked(row) || (!d.calc && !d.side_parts)) return;
        const key = { d: 'values', w: 'weekly_values', m: 'monthly_values' }[kind];
        const blank = v => v === null || v === undefined || isNaN(v);
        const plain = v => blank(v) ? '-' : withCommas(v, Number.isInteger(v) ? 0 : 2);
        // A column's figure in its own format (Settings > Column Names), else plain.
        const partFmt = (p, v) => {
            const f = metricFormats[metricKey(p.source || 'snowflake', p.name)];
            return blank(v) ? '-' : f ? rowFormatter({ fmt: f.fmt, decimals: f.decimals || 0 })(v) : plain(v);
        };
        const at = (data, i) => (data && data[key] ? data[key][i] : null);
        const resultFmt = rowFormatter(row);
        const result = figures.find(f => f.label === 'Value');
        // One table: a column for each column used (a side of several columns
        // also gets its total), then the result. One row per point, or one per
        // day when the daily chart shows the 7-day average.
        // [side, group heading, its total column's heading]
        const sides = d.calc ? [[d.calc.num, 'Top', 'Top Total'], [d.calc.den, 'Divided By', 'Bottom Total']] : [[d, 'Total', 'Total']];
        const cols = [];
        // groups: one heading over each side's columns ("Top", "Divided By") and
        // the result. start: the first column of a group after the first, which
        // gets a line on its left so the groups read apart.
        const groups = [];
        sides.forEach(([sideData, label, totalHead], s) => {
            const parts = sideData.side_parts || [];
            const first = cols.length;
            parts.forEach(p => cols.push({ head: (p.op ? p.op + ' ' : '') + metricName(p.source, p.name), title: p.name,
                                           cell: i => partFmt(p, at(p.data, i)) }));
            if (parts.length > 1) cols.push({ head: totalHead, total: true, cell: i => plain(at(sideData, i)) });
            const span = parts.length + (parts.length > 1 ? 1 : 0);
            if (span) {
                groups.push({ label, span, start: s > 0 });
                if (s > 0) cols[first].start = true;
            }
        });
        const scale100 = d.calc && d.calc.scale === 100;
        cols.push({ head: d.calc ? `Top / Bottom${scale100 ? ' x 100' : ''}` : 'Result', result: true, start: !!d.calc,
                    cell: (i, v) => (blank(v) ? '-' : resultFmt(v)) });
        groups.push({ label: 'Result', span: 1, start: true });
        const rolling = kind === 'd' && rollingOn(row.id);
        const rowsHtml = [];
        const cellHtml = (i, v) => cols.map(c => `<td class="${c.result ? 'calc-res' : c.total ? 'calc-tot' : ''}${c.start ? ' calc-start' : ''}">${escHtml(c.cell(i, v))}</td>`).join('');
        let footer = '';
        if (rolling) {
            // The chart point is the average of this day and the 6 before it.
            const included = [];
            for (let j = Math.max(0, index - 6); j <= index; j++) {
                const v = d.values[j];
                if (blank(v)) continue;
                included.push(v);
                rowsHtml.push(`<tr><td class="calc-date">${escHtml(ukDate(d.dates[j]))}</td>${cellHtml(j, v)}</tr>`);
            }
            const sum = included.reduce((a, b) => a + b, 0);
            footer = `<tr class="calc-foot"><td colspan="${cols.length}">7-day average: ${escHtml(included.length ? resultFmt(sum) : '-')}`
                + ` (sum of ${included.length} day${included.length === 1 ? '' : 's'}) / ${included.length}</td>`
                + `<td class="calc-res">${escHtml(result && !blank(result.v) ? resultFmt(result.v) : '-')}</td></tr>`;
        } else {
            rowsHtml.push(`<tr>${cellHtml(index, result ? result.v : null)}</tr>`);
        }
        // The group row only helps when there are two sides (a divided chart).
        const groupRow = d.calc
            ? `<tr class="calc-groups">${rolling ? '<th></th>' : ''}`
              + groups.map(g => `<th colspan="${g.span}"${g.start ? ' class="calc-start"' : ''}>${escHtml(g.label)}</th>`).join('') + '</tr>'
            : '';
        // The calculation written out: once with the column names, once with this
        // point's numbers, so anyone can see how the figure (often a %) is made.
        // A side of several columns is put in brackets.
        const sideText = (sideData, val) => {
            const parts = sideData.side_parts || [];
            if (!parts.length) return { text: val ? plain(at(sideData, index)) : 'Value', many: false };
            const text = parts.map((p, n) => (n ? ` ${p.op || '+'} ` : (p.op && p.op !== '+' ? p.op + ' ' : ''))
                + (val ? partFmt(p, at(p.data, index)) : metricName(p.source, p.name))).join('');
            return { text, many: parts.length > 1 };
        };
        const wrap = s => (s.many ? `(${s.text})` : s.text);
        const resultText = result && !blank(result.v) ? resultFmt(result.v) : '-';
        let words, numbers;
        if (d.calc) {
            const top = sideText(d.calc.num, false), bottom = sideText(d.calc.den, false);
            const topN = sideText(d.calc.num, true), bottomN = sideText(d.calc.den, true);
            const x100 = scale100 ? ' x 100' : '';
            words = `${wrap(top)} / ${wrap(bottom)}${x100}`;
            const sums = topN.many || bottomN.many
                ? `${wrap(topN)} / ${wrap(bottomN)} = ${plain(at(d.calc.num, index))} / ${plain(at(d.calc.den, index))}`
                : `${topN.text} / ${bottomN.text}`;
            numbers = `${sums}${x100} = ${resultText}`;
        } else {
            words = sideText(d, false).text;
            numbers = `${sideText(d, true).text} = ${resultText}`;
        }
        // No figure on either side (e.g. today, before the data is loaded): say so
        // rather than a line of dashes.
        const sideBlank = sideData => blank(at(sideData, index))
            && (sideData.side_parts || []).every(p => blank(at(p.data, index)));
        if (!rolling && (d.calc ? sideBlank(d.calc.num) && sideBlank(d.calc.den) : sideBlank(d))) {
            numbers = 'No figures for this point yet';
        }
        const howHtml = `<div class="calc-how"><div class="calc-how-title">How This Is Worked Out</div>`
            + `<div><span class="calc-how-label">Calculation:</span> ${escHtml(words)}</div>`
            + (rolling
                ? `<div>Worked out for each day below, then the chart shows the average of the last 7 days.</div>`
                : `<div><span class="calc-how-label">For this point:</span> <b>${escHtml(numbers)}</b></div>`)
            + `</div>`;
        box.innerHTML = howHtml + `<table><thead>${groupRow}<tr>${rolling ? '<th class="calc-date">Date</th>' : ''}`
            + cols.map(c => `<th${c.start ? ' class="calc-start"' : ''}${c.title ? ` title="${escHtml(c.title)}"` : ''}>${escHtml(c.head)}</th>`).join('')
            + `</tr></thead><tbody>${rowsHtml.join('')}</tbody>${footer ? `<tfoot>${footer}</tfoot>` : ''}</table>`;
        box.classList.add('show');
    }

    function closePointModal() {
        pointState = null;
        document.getElementById('pointModal').classList.remove('open');
    }

    // Clicking the dark area outside the box closes it, like Escape.
    document.getElementById('pointModal').addEventListener('click', e => {
        if (e.target.id === 'pointModal') closePointModal();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && pointState) closePointModal();
    });

    async function loadPointVehicles() {
        if (!pointState) return;
        const state = pointState;
        const button = document.getElementById('viewVehiclesBtn');
        const status = document.getElementById('pointStatus');
        button.disabled = true;
        status.innerHTML = '<i class="fas fa-sync-alt spin"></i> Loading vehicles...';
        try {
            const params = new URLSearchParams({ row_id: state.row.id, start: state.start, end: state.end });
            const resp = await fetch(`${BASE_URL}/kpi/vehicles?${params}`).then(r => r.json());
            if (pointState !== state) return;   // popup closed or moved on meanwhile
            if (resp.status !== 'ok') throw new Error(resp.message || 'load failed');
            state.data = resp;
            const notes = [`${resp.rows.length.toLocaleString('en-GB')} vehicle${resp.rows.length === 1 ? '' : 's'}`];
            if (resp.truncated) notes.push(`only the first ${resp.row_limit.toLocaleString('en-GB')} shown`);
            if (resp.missing_columns.length) notes.push(`not in the table any more: ${resp.missing_columns.join(', ')}`);
            // When the chart figure is an average, a ratio or a %, or the list is
            // a weekly/monthly snapshot (one day, not the whole period), the rows
            // do not add up to the point - so say the list is for inspection and
            // does not reconcile to it. A plain count over the full range does.
            const snapshot = (resp.date_mode || (state.row.vehicles || {}).date_mode) === 'snapshot';
            const notSum = state.row.agg === 'avg' || state.row.pct || !!state.row.divide_by;
            let inspect = '';
            if (notSum) inspect = 'For inspection - the chart figure is an average, so these rows do not add up to it.';
            else if (snapshot && state.start !== state.end) inspect = `For inspection - a single-day snapshot (${ukDate(state.end)}), not every day in the period.`;
            status.innerHTML = escHtml(notes.join(' - '))
                + (inspect ? `<span class="point-inspect">${escHtml(inspect)}</span>` : '');
            renderQuickFilters();
            renderVehiclesTable();
            document.querySelector('#pointModal .point-modal').classList.add('has-vehicles');
            document.getElementById('vehiclesWrap').classList.add('show');
            document.getElementById('vehiclesCsvBtn').style.display = resp.rows.length ? '' : 'none';
        } catch (err) {
            if (pointState === state) status.textContent = 'Could not load the vehicles: ' + err.message;
        } finally {
            if (pointState === state) button.disabled = false;
        }
    }

    const VEHICLE_NUMBER_TYPES = ['NUMBER', 'FLOAT', 'DECIMAL', 'NUMERIC', 'INT', 'INTEGER', 'BIGINT', 'DOUBLE', 'REAL'];

    // How a column's values show in the list, set per column in Settings.
    const VEH_FORMATS = [
        ['number', 'Number'], ['gbp', '£'], ['pct', '%'], ['text', 'Text'], ['date', 'Date'], ['datetime', 'Date & time'],
        ['link', 'Hyperlink'],
    ];
    // Formats that hold a number: right-aligned, take decimals, can have a total.
    const VEH_NUMBER_FORMATS = ['number', 'gbp', 'pct'];

    // Starting format for a column with none saved, from its Snowflake type.
    // IDs and years are numbers in Snowflake but read wrong with commas, so
    // they start as text.
    function defaultVehFormat(column, type) {
        if (/(^|_)(URL|LINK)$/.test(column)) return 'link';
        if (/(^|_)(ID|YEAR|NUMBER)$/.test(column)) return 'text';
        if (type === 'DATE') return 'date';
        if (type && type.startsWith('TIMESTAMP')) return 'datetime';
        if (VEHICLE_NUMBER_TYPES.includes(type)) return 'number';
        return 'text';
    }

    function colFormat(col) {
        return col.format || defaultVehFormat(col.column, col.type);
    }

    // { html, num } for one cell. A value that does not suit the chosen
    // format (e.g. text in a Number column) shows as it is.
    function vehicleCell(value, col) {
        if (value === null || value === undefined) return { html: '' };
        if (typeof value === 'boolean') return { html: value ? 'Yes' : 'No' };
        const format = colFormat(col);
        const decimals = Number.isInteger(col.decimals) ? col.decimals : (Number.isInteger(value) ? 0 : 2);
        if (typeof value === 'number' && format === 'number') return { html: escHtml(withCommas(value, decimals)), num: true };
        // Minus before the £ sign: -£1,234.
        if (typeof value === 'number' && format === 'gbp') {
            return { html: escHtml((value < 0 ? '-£' : '£') + withCommas(Math.abs(value), decimals)), num: true };
        }
        if (typeof value === 'number' && format === 'pct') return { html: escHtml(withCommas(value * 100, decimals) + '%'), num: true };
        const text = String(value);
        // Hyperlink: just an icon that opens the address in a new tab; the full
        // address shows on hover. Anything not starting http(s) shows as text.
        if (format === 'link' && /^https?:\/\//i.test(text)) {
            return { html: `<a class="veh-link" href="${escHtml(text)}" target="_blank" rel="noopener" title="${escHtml(text)}">`
                + '<i class="fas fa-external-link-alt"></i></a>', link: true };
        }
        if (format === 'date' || format === 'datetime') {
            if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text)) {
                return { html: format === 'datetime' ? `${ukDate(text)} ${text.slice(11, 16)}` : ukDate(text) };
            }
            if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return { html: ukDate(text) };
        }
        if (/^https?:\/\//i.test(text)) {
            return { html: `<a href="${escHtml(text)}" target="_blank" rel="noopener">Open</a>` };
        }
        return { html: escHtml(text), num: typeof value === 'number' };
    }

    // Click a heading to sort by it; click again to reverse. Blanks go last.
    function sortVehicles(index) {
        if (!pointState || !pointState.data) return;
        const sort = pointState.sort;
        pointState.sort = { index, dir: sort && sort.index === index ? -sort.dir : 1 };
        renderVehiclesTable();
    }

    // ── Quick filters above the list (set per table in Settings > View Vehicles) ──
    // A text or true/false column gets a drop-down of the values found in the
    // loaded list; a number or date column gets from / to boxes. They only narrow
    // the rows already loaded, so the totals row and the CSV follow them.
    function quickFilterKind(f) {
        if (VEHICLE_NUMBER_TYPES.includes(f.type)) return 'num';
        if (VEH_DATE_TYPES.includes(f.type)) return 'date';
        return 'select';
    }

    function renderQuickFilters() {
        const bar = document.getElementById('vehiclesQuick');
        const filters = pointState.data.quick_filters || [];
        if (!filters.length) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
        bar.innerHTML = filters.map((f, n) => {
            const kind = quickFilterKind(f);
            if (kind === 'select') {
                const values = [...new Set(pointState.data.rows.map(r => r[f.index])
                    .filter(v => v !== null && v !== undefined && v !== ''))]
                    .map(v => typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v))
                    .sort((a, b) => a.localeCompare(b, 'en-GB', { numeric: true }));
                return `<div class="veh-multi-field">${escHtml(f.label)}
                    <details class="veh-multi"><summary id="vehMultiSum${n}">All</summary>
                    <div class="veh-multi-list">
                    <a href="#" onclick="clearQuickSelect(${n}); return false;">Clear</a>
                    ${values.map(v => `<label><input type="checkbox" value="${escHtml(v)}"
                        onchange="toggleQuickSelect(${n}, this)"> ${escHtml(v)}</label>`).join('')}
                    </div></details></div>`;
            }
            const t = kind === 'num' ? 'number' : 'date';
            const step = kind === 'num' ? ' step="any"' : '';
            return `<label>${escHtml(f.label)}<span class="veh-quick-range">
                <input type="${t}"${step} title="From" oninput="setQuickFilter(${n}, 'min', this.value)">
                <input type="${t}"${step} title="To" oninput="setQuickFilter(${n}, 'max', this.value)"></span></label>`;
        }).join('') + '<span class="veh-quick-count" id="vehQuickCount"></span>';
        bar.classList.add('show');
    }

    // Tick-list filter: selected = the ticked values; none ticked means all.
    function toggleQuickSelect(n, box) {
        const f = pointState.data.quick_filters[n];
        const cur = pointState.filterVals[f.column] || {};
        const sel = new Set(cur.sel || []);
        if (box.checked) sel.add(box.value); else sel.delete(box.value);
        pointState.filterVals[f.column] = { ...cur, sel: [...sel] };
        showQuickSelectSummary(n);
        renderVehiclesTable();
    }

    function clearQuickSelect(n) {
        const f = pointState.data.quick_filters[n];
        pointState.filterVals[f.column] = { ...pointState.filterVals[f.column], sel: [] };
        document.querySelectorAll(`#vehMultiSum${n}`).forEach(s =>
            s.parentElement.querySelectorAll('input[type=checkbox]').forEach(b => { b.checked = false; }));
        showQuickSelectSummary(n);
        renderVehiclesTable();
    }

    function showQuickSelectSummary(n) {
        const f = pointState.data.quick_filters[n];
        const sel = (pointState.filterVals[f.column] || {}).sel || [];
        document.getElementById(`vehMultiSum${n}`).textContent =
            !sel.length ? 'All' : sel.length === 1 ? sel[0] : `${sel.length} selected`;
    }

    // Clicking elsewhere closes an open tick-list.
    document.addEventListener('click', e => {
        document.querySelectorAll('.veh-multi[open]').forEach(d => { if (!d.contains(e.target)) d.removeAttribute('open'); });
    });

    function setQuickFilter(n, key, value) {
        const f = pointState.data.quick_filters[n];
        pointState.filterVals[f.column] = { ...pointState.filterVals[f.column], [key]: value };
        renderVehiclesTable();
    }

    // Rows that pass every quick filter that has a value.
    function filteredVehicleRows() {
        const { data, filterVals } = pointState;
        const active = (data.quick_filters || []).filter(f => {
            const v = filterVals[f.column];
            return v && ((v.sel && v.sel.length) || v.min || v.max);
        });
        if (!active.length) return data.rows;
        return data.rows.filter(r => active.every(f => {
            const v = filterVals[f.column], cell = r[f.index], kind = quickFilterKind(f);
            if (kind === 'select') {
                const text = typeof cell === 'boolean' ? (cell ? 'Yes' : 'No') : String(cell === null || cell === undefined ? '' : cell);
                return v.sel.includes(text);
            }
            if (cell === null || cell === undefined) return false;
            if (kind === 'num') {
                return (!v.min || cell >= parseFloat(v.min)) && (!v.max || cell <= parseFloat(v.max));
            }
            const day = String(cell).slice(0, 10);   // ISO date, so text order is date order
            return (!v.min || day >= v.min) && (!v.max || day <= v.max);
        }));
    }

    // The loaded vehicles in the order the table shows them.
    function sortedVehicleRows() {
        const { sort } = pointState;
        const filtered = filteredVehicleRows();
        if (!sort) return filtered;
        return [...filtered].sort((p, q) => {
            const a = p[sort.index], b = q[sort.index];
            if (a === null || a === undefined) return 1;
            if (b === null || b === undefined) return -1;
            return (a < b ? -1 : a > b ? 1 : 0) * sort.dir;
        });
    }

    function csvEscape(v) {
        if (v === null || v === undefined) return '';
        const s = String(v);
        return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }

    // The list as shown (same columns, headings and sort), with the raw values
    // rather than the display formatting, so numbers stay numbers in Excel.
    function downloadVehiclesCsv() {
        if (!pointState || !pointState.data) return;
        const { data, row, start, end } = pointState;
        const header = data.columns.map(c => csvEscape(c.label)).join(',');
        // Only the shown columns: a row may also carry its grey-out flag at the end.
        // Cells blanked on grey rows in the list are blank in the file as well.
        const isGrey = r => !!(data.grey_flag && r[data.columns.length]);
        const lines = sortedVehicleRows().map(r => data.columns.map((c, i) =>
            csvEscape(c.blank_grey && isGrey(r) ? null : r[i])).join(','));
        // Byte order mark first so Excel reads the file as UTF-8 (e.g. £ signs).
        const blob = new Blob(['﻿' + [header, ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        const slug = rowTitle(row).replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
        const period = start === end ? start : `${start}_to_${end}`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `vehicles_${slug}_${period}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    function renderVehiclesTable() {
        const { data, sort } = pointState;
        const rows = sortedVehicleRows();
        const numCols = data.columns.map(c => VEH_NUMBER_FORMATS.includes(colFormat(c)));
        const head = data.columns.map((c, i) => {
            const mark = sort && sort.index === i ? (sort.dir === 1 ? '&#9650;' : '&#9660;') : '';
            return `<th${numCols[i] ? ' class="num"' : ''} onclick="sortVehicles(${i})" title="${escHtml(c.column)}">${escHtml(c.label)}<span class="sort-mark">${mark}</span></th>`;
        }).join('');
        // A row matched by the Settings grey-out rule (e.g. a cancelled deal)
        // carries true as one extra value after its shown columns.
        // Columns set to "Blank on grey rows" show nothing on those rows, e.g. the
        // price and price position of a cancelled deal, which are read from the
        // vehicle record and so show a later price, not the one at the order.
        const isGrey = r => !!(data.grey_flag && r[data.columns.length]);
        const body = rows.map(r => (isGrey(r) ? '<tr class="grey-row">' : '<tr>') + data.columns.map((c, i) => {
            const cell = c.blank_grey && isGrey(r) ? { html: '' } : vehicleCell(r[i], c);
            const cls = cell.num ? 'num' : cell.link ? 'link' : '';
            return `<td${cls ? ` class="${cls}"` : ''}>${cell.html}</td>`;
        }).join('') + '</tr>').join('');
        document.getElementById('vehiclesTable').innerHTML =
            `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>${vehiclesSummaryRow(data.columns, rows, isGrey)}`;
        const count = document.getElementById('vehQuickCount');
        if (count) count.textContent = `${rows.length.toLocaleString('en-GB')} of ${data.rows.length.toLocaleString('en-GB')} vehicles`;
    }

    // Row under the list with each column's Total or Average (set per column in
    // Settings), over every vehicle loaded. Blanks are left out of an average,
    // and so are cells blanked on grey rows, so the figure matches the chart.
    // The first cell says what the row is when that column has no figure itself.
    function vehiclesSummaryRow(columns, rows, isGrey) {
        if (!columns.some(c => c.summary === 'sum' || c.summary === 'avg') || !rows.length) return '';
        const cells = columns.map((c, i) => {
            if (c.summary !== 'sum' && c.summary !== 'avg') return { html: '' };
            const values = rows.filter(r => !(c.blank_grey && isGrey(r)))
                .map(r => r[i]).filter(v => typeof v === 'number');
            if (!values.length) return { html: '', num: true };
            const total = values.reduce((a, b) => a + b, 0);
            const cell = vehicleCell(c.summary === 'sum' ? total : total / values.length, c);
            const kind = c.summary === 'sum' ? 'Total' : 'Average';
            return { ...cell, title: `${kind} of ${values.length.toLocaleString('en-GB')} vehicle${values.length === 1 ? '' : 's'}` };
        });
        if (!cells[0].html) cells[0] = { html: 'Total / average' };
        return '<tfoot><tr>' + cells.map(cell =>
            `<td${cell.num ? ' class="num"' : ''}${cell.title ? ` title="${escHtml(cell.title)}"` : ''}>${cell.html}</td>`
        ).join('') + '</tr></tfoot>';
    }

    // Live table and columns for the View Vehicles setup in Edit Chart and Settings.
    async function fetchVehicleColumns(table) {
        const params = table ? '?' + new URLSearchParams({ table }) : '';
        const resp = await fetch(`${BASE_URL}/kpi/vehicle-columns${params}`).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'could not read the vehicle columns');
        return resp;
    }

    // ── Line chart builder ────────────────────────────────────────────────────
    // lyData may be null (no N-1 dataset). targetData of all-nulls = full-green chart.
    // opts.day: the arrays are the whole day history; only the current window
    // is plotted (see makeDayChart in charts.js).
    function buildMetricChart(canvasId, labels, data, lyData, targetData, existingChart, opts) {
        const { seriesLabel, n1Labels, invert, fmt, tickFmt, day } = opts;
        const fullFmt = opts.fullFmt || fmt;   // hover box: the figure in full
        const { yMin, yMax, unit } = opts.range;
        const ctx = document.getElementById(canvasId).getContext('2d');
        if (existingChart) existingChart.destroy();

        const datasets = [];
        if (hasValues(lyData)) {
            datasets.push({
                label: 'N-1',
                data: lyData,
                fill: false,
                borderColor: settings.n1_line_colour || N1_COLOUR,
                borderWidth: settings.n1_line_px || 1.5,
                borderDash: [5, 4],
                pointRadius: 0,
                tension: 0,
                spanGaps: true,
                order: 3,
            });
        }
        if (hasValues(targetData)) {
            datasets.push({
                label: 'Target',
                data: targetData,
                fill: false,
                borderColor: settings.target_line_colour || '#e53e3e',
                borderWidth: settings.target_line_px || 1.5,
                borderDash: [],
                pointRadius: 0,
                tension: 0,
                spanGaps: true,
                order: 2,
            });
        }
        datasets.push({
            label: seriesLabel,
            data: data,
            fill: false,
            borderColor: settings.main_line_colour || '#0d3d6e',
            borderWidth: settings.main_line_px || 2.5,
            pointRadius: p => p.dataset.data[p.dataIndex] !== null ? 3 : 0,
            pointBackgroundColor: settings.main_line_colour || '#0d3d6e',
            pointBorderColor: '#fff',
            pointBorderWidth: 1.5,
            tension: 0,
            spanGaps: false,
            order: 1,
        });

        const build = (chartLabels, chartDatasets, chartTarget) => {
            const angle = Number(settings.label_angle) || 0;
            const below = opts.dataLabels && Number(settings.label_below) === 1;
            const chart = new Chart(ctx, {
                type: 'line',
                data: { labels: chartLabels, datasets: chartDatasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    // Clicking anywhere over a point opens its popup (see openPointModal).
                    onClick: (evt, elements, chart) => {
                        if (opts.notes && elements.length) openPointModal(opts.notes, chart, elements[0].index);
                    },
                    // Tilted labels above the highest points need room at the top.
                    layout: { padding: { top: !below && angle ? 22 : 4 } },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: c => {
                                    const v = c.parsed.y;
                                    if (v === null || isNaN(v)) return null;
                                    if (c.dataset.label === 'N-1' && n1Labels) {
                                        return `N-1 (${n1Labels[c.dataIndex]}): ${fullFmt(v)}`;
                                    }
                                    if (c.dataset.label === 'N-1' || c.dataset.label === 'Target') {
                                        return c.dataset.label + ': ' + fullFmt(v);
                                    }
                                    // The chart's own line: the title is already above the
                                    // chart, so the figure only.
                                    return fullFmt(v);
                                },
                                // The axis is scaled from the data, so a target outside
                                // it is clipped and Chart.js drops it from the tooltip.
                                // State it here so the value is still reachable.
                                afterBody: items => {
                                    if (!items.length) return;
                                    const t = (items[0].chart.$bgTarget || [])[items[0].dataIndex];
                                    if (t === null || t === undefined || isNaN(t)) return;
                                    if (t <= yMax && t >= yMin) return;
                                    return `Target: ${fullFmt(t)} (${t > yMax ? 'above' : 'below'} chart range)`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            border: { display: false },
                            grid: { color: 'rgba(0,0,0,0.06)' },
                            ticks: { font: { size: 10 }, maxRotation: 45, minRotation: 30 }
                        },
                        y: {
                            min: yMin,
                            max: yMax,
                            border: { display: false },
                            grid: { color: 'rgba(0,0,0,0.06)' },
                            ticks: { font: { size: 10 }, stepSize: unit, callback: v => tickFmt(v) }
                        }
                    }
                },
                plugins: [
                    makeBgPlugin(invert, chartTarget),
                    PLOT_FRAME_PLUGIN,
                    ...(opts.dataLabels ? [makeDataLabelPlugin(canvasId + 'Labels', seriesLabel, Number(settings.line_label_font_px) || 10, fmt, () => settings.label_angle,
                        () => Number(settings.label_below) === 1)] : []),
                    ...(opts.notes ? [makeNotesPlugin(opts.notes)] : [])
                ]
            });
            chart.$bgTarget = chartTarget;
            return chart;
        };

        return day ? makeDayChart(labels, datasets, targetData, build) : build(labels, datasets, targetData);
    }

    // ── Stacked column charts ───────────────────────────────────────────────
    // chart_type 'stacked': one segment per series (row.series), month / week /
    // day like every chart, no N-1. Each series is fetched with the same
    // /kpi/custom-data call as a line chart and the page stacks them.
    const isStacked = row => row.chart_type === 'stacked';
    // A series past the theme's last colour: grey, never a repeated colour, so
    // two series never look alike.
    const SERIES_SPARE_COLOUR = '#9aa0a6';

    function themeById(id) {
        return themes.find(t => t.id === id) || themes[0] || { id: '', name: '', colours: [] };
    }

    // Each series' colour: its own if set, else the theme's colour in the same
    // position, so a series keeps its colour when others are added after it.
    function seriesColours(row) {
        const theme = themeById(row.theme);
        return (row.series || []).map((s, i) => s.colour || theme.colours[i] || SERIES_SPARE_COLOUR);
    }

    // A colour mixed halfway towards white: the default for the part of a
    // split series above its split point.
    function lighterColour(hex) {
        const n = parseInt(String(hex).slice(1), 16);
        const mix = c => Math.round(c + (255 - c) * 0.5);
        return '#' + [n >> 16 & 255, n >> 8 & 255, n & 255].map(c => mix(c).toString(16).padStart(2, '0')).join('');
    }
    const hasSplit = s => !!(s.split_at && s.split_at.metric);
    // Name of the part of a split series above its split point.
    const splitPartName = s => (s.split_at.label || '').trim() || `Above ${metricName(s.split_at.source, s.split_at.metric)}`;

    // The segments a stacked chart draws: one per series, except a series with
    // Split At, which is two (the part up to the split metric, then the part
    // above it, stacked straight on top). Theme colours still go by the
    // series' own position, so splitting one does not recolour the others.
    function plotSeries(row) {
        const colours = seriesColours(row);
        const out = [];
        (row.series || []).forEach((s, i) => {
            if (!hasSplit(s)) { out.push({ s, colour: colours[i], name: seriesLabel(s), split: null }); return; }
            const at = { source: s.split_at.source || 'snowflake', metric: s.split_at.metric };
            out.push({ s, colour: colours[i], name: seriesLabel(s), split: { ...at, part: 'lower' } });
            out.push({ s: { ...s, within_below: false }, colour: s.split_at.colour || lighterColour(colours[i]),
                       name: splitPartName(s), split: { ...at, part: 'upper' } });
        });
        return out;
    }

    // The shared column name wins, so renaming it in Settings changes every
    // chart; a label saved on the series before names were shared is next.
    function seriesLabel(s) {
        // A multiplied series is a new metric: its own name, never the column's.
        const dk = derivedKey(s);
        if (dk) return derivedNames[dk] || derivedDefaultName(s);
        return metricNames[metricKey(s.source || 'snowflake', s.metric)] || s.label || s.metric;
    }

    // What the Legend Name box holds for a series: its derived name when
    // multiplied, else the shared column name ('' when there is none).
    function seriesBoxName(s) {
        const dk = derivedKey(s);
        if (dk) return derivedNames[dk] || '';
        return metricNames[metricKey(s.source || 'snowflake', s.metric)] || '';
    }

    function fetchStackedData(row) {
        const cur = rowCurrent(row);
        const plotted = plotSeries(row);
        return Promise.all(plotted.map(p => fetchMetricData(p.s.source || 'snowflake', p.s.metric, row.agg, 'none', cur,
                                                            '', p.s.multiply_by || null, p.split)))
            .then(results => {
                const bad = results.findIndex(r => r.status !== 'ok');
                if (bad >= 0) return { ...results[bad], message: `${plotted[bad].name}: ${results[bad].message}` };
                const d = {
                    ...results[0],
                    stale_date: results.map(r => r.stale_date).find(Boolean) || null,
                    stacked: results.map(r => ({ m: r.monthly_values, w: r.weekly_values, d: r.values })),
                };
                // Per Date: the chart's own figures (kpi:row:<id>). A 100%
                // chart's weeks and months take the average day, like Average.
                return row.target_mode === 'import' ? withRowDateTargets(row.pct ? { ...row, agg: 'avg' } : row, d) : d;
            });
    }

    // Target line for a stacked chart: one figure per day, against the column
    // total. Weeks and months add it up on Sum charts (7 days, days in the
    // month); Average charts and 100% charts use the figure as it is.
    function stackedTargets(row, d, periods) {
        if (row.target_mode === 'import') {
            return { m: d.monthly_values_target || null, w: d.weekly_values_target || null, d: d.values_target || null };
        }
        const t = row.target_mode === 'fixed' ? row.stack_target : null;
        if (t === null || t === undefined) return { m: null, w: null, d: null };
        const same = labels => labels.map(() => t);
        if (row.pct || row.agg === 'avg') return { m: same(periods.m.labels), w: same(periods.w.labels), d: same(periods.d.labels) };
        const daysIn = first => Number(lastDayOfMonthIso(first).slice(8, 10));
        return {
            m: (d.monthly_starts || []).map(f => t * daysIn(f)),
            w: periods.w.labels.map(() => t * 7),
            d: same(periods.d.labels),
        };
    }

    // Targets and return as buildRowCharts.
    function buildStackedCharts(row, d, targets) {
        const side = row.column_layout === 'side_by_side';
        const pct = !!row.pct && !side;
        const decimals = row.decimals || 0;
        // A series ticked "within the series below" shows only what is left
        // after taking that series off, so the two stack to its full figure.
        const taken = (arr, below) => arr.map((v, i) => v === null || v === undefined || below[i] === null || below[i] === undefined ? v : v - below[i]);
        // One entry per segment drawn (a split series is two); d.stacked is in
        // the same order. "Within below" takes off the whole series below,
        // both parts if it is split.
        const plotted = plotSeries(row);
        const add = (a, b) => a.map((v, i) => v === null || v === undefined ? b[i] : (b[i] === null || b[i] === undefined ? v : v + b[i]));
        const belowWhole = (pick, j) => {
            const k = plotted[j].split && plotted[j].split.part === 'upper' ? j - 1 : j;
            return k < j ? add(pick(d.stacked[k]), pick(d.stacked[j])) : pick(d.stacked[j]);
        };
        const adjusted = (key, pick) => plotted.map((p, i) => {
            if (!p.s.within_below || i === 0) return pick(d.stacked[i]);
            return taken(pick(d.stacked[i]), belowWhole(pick, i - 1));
        });
        const fmt = pct ? (v => withCommas(v, decimals) + '%') : rowFormatter(row);
        const tickFmt = pct ? (v => v.toLocaleString('en-GB', { maximumFractionDigits: 1 }) + '%') : rowTickFormatter(row);
        // Days after today have no figures yet; their columns stay empty.
        const pastOnly = arr => (arr || []).map((v, i) => d.dates[i] <= d.today ? v : null);
        const periods = {
            m: { labels: d.monthly_labels, series: adjusted('m', s => s.m), starts: d.monthly_starts || [] },
            w: { labels: d.weekly_labels, series: adjusted('w', s => s.w), starts: d.weekly_starts || [] },
            d: { labels: d.dates.map(dateLabel), series: adjusted('d', s => s.d).map(pastOnly), starts: d.dates },
        };
        // Series on the right-hand axis are lines: not in the columns, totals or 100%.
        const onRight = plotted.map(p => p.s.axis === 'secondary');
        Object.values(periods).forEach(p => {
            p.totals = p.labels.map((_, i) => {
                const known = p.series.filter((_s, j) => !onRight[j]).map(s => s[i]).filter(v => v !== null && v !== undefined);
                return known.length ? known.reduce((a, b) => a + b, 0) : null;
            });
            // 100%: each segment becomes its share of the column.
            if (pct) p.series = p.series.map((s, j) => onRight[j] ? s : s.map((v, i) =>
                v === null || v === undefined || !p.totals[i] ? null : v / p.totals[i] * 100));
        });
        const rightRanges = onRight.some(Boolean)
            ? calcRowRanges(['m', 'w', 'd'].map(k => ({ series: periods[k].series.filter((_s, j) => onRight[j]) })), 'sum')
            : null;
        const targets_ = stackedTargets(row, d, periods);
        const ranges = pct
            ? ['m', 'w', 'd'].map(() => ({ yMin: 0, yMax: 100, unit: 20 }))
            : calcRowRanges(['m', 'w', 'd'].map(k => ({ series: side ? periods[k].series.filter((_s, j) => !onRight[j]) : [periods[k].totals], min: (row.y_min || {})[k], max: (row.y_max || {})[k] })),
                row.agg, sharedAxisValue(row.y_min), sharedAxisValue(row.y_max));
        const opts = { fmt, fullFmt: rowFullFormatter(row, pct), tickFmt, pct, side, colours: plotted.map(p => p.colour), names: plotted.map(p => p.name),
                       splits: plotted.map(p => (p.split ? p.split.part : null)),
                       splitNames: plotted.map(p => (p.split ? ((p.s.split_at.lower_label || '').trim() || metricName(p.split.source, p.split.metric)) : null)),
                       transparency: themeById(row.theme).transparency || 0,
                       // A 100% chart has no column totals, so its figures are the segment values.
                       segLabels: !!row.seg_labels || !!row.pct, segPos: row.seg_label_pos || 'center', dataLabels: dataLabelsOn(row) };
        const at = { m: 0, w: 1, d: 2 };
        return targets.map(({ k, canvasId, existing, first }) => buildStackedChart(canvasId, periods[k], targets_[k], existing,
            { ...opts, range: ranges[at[k]], rightRange: rightRanges && rightRanges[at[k]], onRight, day: k === 'd',
              notes: { rowId: row.id, kind: k, starts: periods[k].starts, first: first || 0 } }));
    }

    // Dark text on light segments, white on dark ones (text never takes the
    // series colour itself). transparency (0-100 %) lightens the colour
    // towards the white background behind it before deciding.
    function textOn(hex, transparency = 0) {
        const n = parseInt(String(hex).slice(1), 16);
        const t = transparency / 100;
        const mix = c => c * (1 - t) + 255 * t;
        const lum = 0.299 * mix((n >> 16) & 255) + 0.587 * mix((n >> 8) & 255) + 0.114 * mix(n & 255);
        return lum > 150 ? '#0f172a' : '#ffffff';
    }

    // '#rrggbb' as rgba() with a theme's transparency %.
    function withTransparency(hex, transparency) {
        if (!transparency) return hex;
        const n = parseInt(String(hex).slice(1), 16);
        return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${1 - transparency / 100})`;
    }

    // Labels on a stacked chart: each column's total above it (not on a 100%
    // chart), and, when the chart asks for it, each segment's value inside it
    // where the segment is tall enough to hold the text. Series hidden by
    // clicking the legend are left out of the total.
    function makeStackLabelsPlugin(opts) {
        return {
            id: 'stackLabels',
            afterDatasetsDraw(chart) {
                if (!opts.dataLabels) return;
                const { ctx } = chart;
                const bars = chart.data.datasets
                    .map((ds, i) => ({ ds, meta: chart.getDatasetMeta(i), visible: chart.isDatasetVisible(i) }))
                    .filter(b => b.ds.type === 'bar' && b.visible);
                if (!bars.length) return;
                // Sizes and rules from KPI Settings > Labels.
                const segPx = Number(settings.stack_seg_font_px) || 9;
                const totalPx = Number(settings.stack_total_font_px) || 10;
                const segMin = Number(settings.stack_seg_min_px ?? 12);
                const dirMode = Number(settings.stack_label_dir) || 0;        // 0 auto, 1 across, 2 on their side
                const overflow = Number(settings.stack_label_overflow) || 0;  // 0 hide that one, 1 hide all, 2 show anyway
                const SEG_FONT = `600 ${segPx}px DM Sans, system-ui, sans-serif`;
                const TOTAL_FONT = `bold ${totalPx}px DM Sans, system-ui, sans-serif`;
                // A total may use the column's whole slot, gap included.
                const slot = chart.scales.x.width / Math.max(1, chart.data.labels.length);
                const minCol = Number(settings.stack_min_col_px) || 0;
                if (minCol && slot < minCol) return;
                // Work out every label first, with whether it fits across and
                // whether it fits on its side. One direction is then chosen for
                // the whole chart, so labels never mix across and on their side.
                const labels = [];
                const plotTop = chart.chartArea.top;
                ctx.save();
                // Inside a segment: centred in it either way.
                const inside = (text, el, colour) => {
                    // Only the part of the segment inside the plot counts: with an axis that
                    // starts above 0 the bar's base is off the bottom of the chart.
                    const top = Math.max(Math.min(el.y, el.base), chart.chartArea.top);
                    const bottom = Math.min(Math.max(el.y, el.base), chart.chartArea.bottom);
                    const w = ctx.measureText(text).width, h = bottom - top, bw = el.width || slot;
                    if (h < segMin) return;   // too short to hold a value at all
                    // Where in the segment the value sits: centre, or tucked against its top or bottom edge.
                    const mid = (top + bottom) / 2;
                    const pos = opts.segPos || 'center', pad = 3;
                    const yAcross = pos === 'end' ? top + segPx / 2 + pad : pos === 'start' ? bottom - segPx / 2 - pad : mid;
                    const yUp = pos === 'end' ? top + w / 2 + pad : pos === 'start' ? bottom - w / 2 - pad : mid;
                    labels.push({ text, x: el.x, y: mid, yAcross, yUp, font: ctx.font, colour, inside: true,
                                  across: h >= segPx + 3 && w <= bw - 2, up: bw >= segPx + 2 && w <= h - 4 });
                };
                // Above a column (room up to the top of the plot).
                const above = (text, x, top, width) => {
                    const w = ctx.measureText(text).width;
                    labels.push({ text, x, y: top - 3, font: ctx.font, colour: '#0f172a', inside: false,
                                  across: w <= width && top - 3 - totalPx >= plotTop - 2, up: top - 3 - w >= plotTop });
                };
                for (let i = 0; i < chart.data.labels.length; i++) {
                    if (opts.segLabels) {
                        ctx.font = SEG_FONT;
                        bars.forEach((b, n) => {
                            const v = b.ds.data[i], el = b.meta.data[i];
                            if (v === null || v === undefined || !el) return;
                            // Split At: one value for the whole series (both parts),
                            // centred over both; the upper part has none of its own.
                            const lower = bars[n - 1], upper = bars[n + 1];
                            if (b.ds.$part === 'upper' && lower && lower.ds.$part === 'lower') return;
                            if (b.ds.$part === 'lower' && upper && upper.ds.$part === 'upper' && upper.meta.data[i]) {
                                const uv = upper.ds.data[i];
                                const both = { x: el.x, width: el.width, base: el.base, y: upper.meta.data[i].y };
                                inside(opts.fmt(v + (uv === null || uv === undefined ? 0 : uv)), both, textOn(b.ds.$hex, opts.transparency));
                                return;
                            }
                            inside(opts.fmt(v), el, textOn(b.ds.$hex, opts.transparency));
                        });
                    }
                    if (opts.pct) continue;
                    // Side by side: each column's own value above it, unless
                    // Values In Segments already put it inside.
                    if (opts.side) {
                        if (opts.segLabels) continue;
                        ctx.font = TOTAL_FONT;
                        bars.forEach(b => {
                            const v = b.ds.data[i], el = b.meta.data[i];
                            if (v === null || v === undefined || !el) return;
                            above(opts.fmt(v), el.x, Math.min(el.y, el.base), (el.width || slot) + 4);
                        });
                        continue;
                    }
                    const known = bars.filter(b => b.ds.data[i] !== null && b.ds.data[i] !== undefined && b.meta.data[i]);
                    if (!known.length) continue;
                    const total = known.reduce((a, b) => a + b.ds.data[i], 0);
                    ctx.font = TOTAL_FONT;
                    above(opts.fmt(total), known[0].meta.data[i].x, Math.min(...known.map(b => b.meta.data[i].y)), slot - 2);
                }
                // One direction for the whole chart.
                // Auto turns them all on their side only when that helps: some
                // label fits on its side but not across.
                const rotate = dirMode === 2 || (dirMode === 0 && labels.some(l => !l.across && l.up));
                labels.forEach(l => {
                    l.rotate = rotate;
                    if (l.inside) l.y = rotate ? l.yUp : l.yAcross;
                    l.fits = rotate ? l.up : l.across;
                    l.base = l.inside || rotate ? 'middle' : 'bottom';
                    l.align = l.inside || !rotate ? 'center' : 'left';
                });
                const shown = overflow === 2 ? labels
                    : overflow === 1 && labels.some(l => !l.fits) ? []
                    : labels.filter(l => l.fits);
                shown.forEach(l => {
                    ctx.font = l.font;
                    ctx.textBaseline = l.base;
                    ctx.textAlign = l.align;
                    ctx.fillStyle = l.colour;
                    if (!l.rotate) { ctx.fillText(l.text, l.x, l.y); return; }
                    ctx.save();
                    ctx.translate(l.x, l.y);
                    ctx.rotate(-Math.PI / 2);
                    ctx.fillText(l.text, 0, 0);
                    ctx.restore();
                });
                ctx.restore();
            },
        };
    }

    // p = { labels, series: [[values per point], ...] }. Series are stacked
    // bottom-up in list order, with no borders between the segments.
    function buildStackedChart(canvasId, p, target, existingChart, opts) {
        const { yMin, yMax, unit } = opts.range;
        const ctx = document.getElementById(canvasId).getContext('2d');
        if (existingChart) existingChart.destroy();
        const datasets = p.series.map((data, i) => opts.onRight[i] ? {
            type: 'line',
            label: opts.names[i],
            data,
            yAxisID: 'y2',
            borderColor: opts.colours[i],
            backgroundColor: opts.colours[i],
            $hex: opts.colours[i],
            borderWidth: 2,
            pointRadius: 2,
            fill: false,
            tension: 0,
            spanGaps: true,
            order: 1,
        } : ({
            type: 'bar',
            label: opts.names[i],
            data,
            backgroundColor: withTransparency(opts.colours[i], opts.transparency),
            $hex: opts.colours[i],   // solid colour, for the segment text
            // Split At: 'lower' / 'upper' part of one series (the upper part is
            // always the next dataset), and the name of the lower part's limit.
            $part: (opts.splits || [])[i] || null,
            $limitName: (opts.splitNames || [])[i] || null,
            borderWidth: 0,
            borderSkipped: false,
            stack: opts.side ? 'series' + i : 'total',
            order: 2,
            categoryPercentage: 0.85,
            barPercentage: 0.85,
        }));
        if (hasValues(target)) {
            datasets.push({
                type: 'line', label: 'Target', data: target, stack: 'target', order: 1,
                borderColor: settings.target_line_colour || '#e53e3e', borderWidth: settings.target_line_px || 1.5,
                pointRadius: 0, fill: false, tension: 0, spanGaps: true,
            });
        }
        const build = (labels, sets) => new Chart(ctx, {
            type: 'bar',
            data: { labels, datasets: sets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                onClick: (evt, elements, chart) => {
                    if (opts.notes && elements.length) openPointModal(opts.notes, chart, elements[0].index);
                },
                // The room for the totals above the tallest column is added
                // under the legend (legendGap), not above it, where the
                // padding would only push the legend down.
                layout: { padding: { top: 4 } },
                plugins: {
                    legendGap: { gap: opts.pct || !opts.dataLabels ? 8 : 20 },
                    legend: {
                        display: true, position: 'top', align: 'start',
                        // Size from Settings > Appearance; the colour box follows it.
                        labels: { boxWidth: legendFontPx(), boxHeight: legendFontPx(), padding: 8, font: { size: legendFontPx() } },
                    },
                    tooltip: {
                        callbacks: {
                            label: c => {
                                const v = c.parsed.y;
                                if (v === null || isNaN(v)) return null;
                                // A split series hovers as one: its total, then the two parts.
                                const ds = c.dataset, sets = c.chart.data.datasets;
                                const lowerVisible = j => j >= 0 && c.chart.isDatasetVisible(j);
                                if (ds.$part === 'upper' && lowerVisible(c.datasetIndex - 1)) return null;
                                if (ds.$part === 'lower' && c.chart.isDatasetVisible(c.datasetIndex + 1)) {
                                    const up = sets[c.datasetIndex + 1].data[c.dataIndex];
                                    const upper = up === null || up === undefined || isNaN(up) ? 0 : up;
                                    return [`${ds.label}: ${opts.fullFmt(v + upper)}`,
                                            `    ${ds.$limitName}: ${opts.fullFmt(v)}`,
                                            `    ${sets[c.datasetIndex + 1].label}: ${opts.fullFmt(upper)}`];
                                }
                                return `${ds.label}: ${opts.fullFmt(v)}`;
                            },
                            footer: items => {
                                if (opts.pct || opts.side) return '';
                                const segs = items.filter(i => i.dataset.type === 'bar' && i.parsed.y !== null && !isNaN(i.parsed.y));
                                return segs.length ? 'Total: ' + opts.fullFmt(segs.reduce((a, i) => a + i.parsed.y, 0)) : '';
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        stacked: true,
                        border: { display: false },
                        grid: { display: false },
                        ticks: { font: { size: 10 }, maxRotation: 45, minRotation: 30 },
                    },
                    ...(opts.rightRange ? { y2: {
                        position: 'right',
                        min: opts.rightRange.yMin,
                        max: opts.rightRange.yMax,
                        border: { display: false },
                        grid: { drawOnChartArea: false },
                        ticks: { font: { size: 10 }, stepSize: opts.rightRange.unit, callback: v => opts.fmt(v) },
                    } } : {}),
                    y: {
                        stacked: !opts.side,
                        min: yMin,
                        max: yMax,
                        border: { display: false },
                        grid: { color: 'rgba(0,0,0,0.06)' },
                        ticks: { font: { size: 10 }, stepSize: unit, callback: v => opts.tickFmt(v) },
                    },
                },
            },
            plugins: [PLOT_FRAME_PLUGIN, LEGEND_GAP_PLUGIN, makeStackLabelsPlugin(opts), ...(opts.notes ? [makeNotesPlugin(opts.notes)] : [])],
        });
        return opts.day ? makeDayChart(p.labels, datasets, target, build) : build(p.labels, datasets);
    }

    // Space between the legend and the plot (options.plugins.legendGap.gap,
    // px). Chart.js has no setting for it, so the legend's measured height is
    // made taller by the gap.
    const LEGEND_GAP_PLUGIN = {
        id: 'legendGap',
        beforeInit(chart, args, options) {
            const legend = chart.legend;
            if (!legend) return;
            const fit = legend.fit;
            legend.fit = function () {
                fit.call(this);
                this.height += options.gap || 0;
            };
        },
    };

    // Day chart period label. Line charts have none: their Daily / 7-Day Avg
    // switch already says it. Stacked charts have no switch, so they show
    // "Daily". No date range: the axis labels show which days are on screen.
    function updateDayTitles() {
        layout.rows.forEach(row => {
            const period = document.getElementById(`p-${row.id}-d`);
            if (period) period.textContent = isStacked(row) ? 'Daily' : '';
        });
    }

    // ── Add / Edit Chart popup (admin only) ───────────────────────────────────
    // One popup for every chart setting except the target, which has its own
    // Targets popup (kpi_targets.js) so other users can set it too.

    let chartModalState = null;   // { rowId (null = adding), plus, autoTitle, ... }

    function selectedMetric() {
        return metricsByKey[document.getElementById('metricSelect').value] || null;
    }

    function setRadio(name, value) {
        document.querySelectorAll(`input[name="${name}"]`).forEach(r => { r.checked = r.value === value; });
    }

    function radioValue(name, fallback) {
        const el = document.querySelector(`input[name="${name}"]:checked`);
        return el ? el.value : fallback;
    }

    // One source's metrics as an <optgroup> for the metric dropdowns.
    function metricGroupHtml(source) {
        const items = metricsCatalogue.filter(m => m.source === source);
        if (!items.length) return '';
        const options = items.map(m =>
            `<option value="${escHtml(metricKey(m.source, m.column))}">${escHtml(metricOptionText(m.source, m.column))}</option>`).join('');
        return `<optgroup label="${escHtml(sourceLabel(source))}">${options}</optgroup>`;
    }

    // While the chart popup is open: the data source its section is locked
    // to (Settings > Sections), or null for any.
    let metricSourceLock = null;

    // Every source in the metric lists, in order: the built-in ones, then
    // each Settings > Data Sources table in the order added. Only the locked
    // one while the chart popup is on a locked section.
    function metricSources(all = false) {
        const sources = ['snowflake', 'sharepoint', 'autotrader'];
        metricsCatalogue.forEach(m => { if (!sources.includes(m.source)) sources.push(m.source); });
        return metricSourceLock && !all ? sources.filter(s => s === metricSourceLock) : sources;
    }

    // Section changed in the chart popup: the metric lists follow its lock.
    function onChartSectionChange() {
        fillPositionOptions();
        const lock = sectionSources[document.getElementById('sectionSelect').value] || null;
        if (lock === metricSourceLock) return;
        metricSourceLock = lock;
        const refill = (id, blank) => {
            const el = document.getElementById(id);
            const value = el.value;
            el.innerHTML = blank + allMetricGroups();
            el.value = value;
        };
        refill('metricSelect', '');
        refill('divideSelect', '<option value="">(nothing - plain chart)</option>');
        refill('multiplySelect', '<option value="">(nothing)</option>');
        renderPlusLists();
        renderSeriesList();
        onMetricChange();
    }

    function sourceLabel(source) {
        const builtIn = { snowflake: 'Snowflake - daily_summary', sharepoint: 'SharePoint - Daily Spend and Enquiries',
                          autotrader: 'Autotrader Costs' }[source];
        if (builtIn) return builtIn;
        const m = metricsCatalogue.find(c => c.source === source);
        return m && m.source_name ? m.source_name : source;
    }

    // Every source's group of options, built by group(source).
    function allMetricGroups(group = metricGroupHtml) {
        return metricSources().map(group).join('');
    }

    // Edit Chart's metric search: hides the options that do not contain the
    // typed text (in the name or the raw column), and empty groups with them.
    function filterMetricSelect(text) {
        const needle = String(text || '').trim().toLowerCase();
        const select = document.getElementById('metricSelect');
        select.querySelectorAll('optgroup').forEach(g => {
            let shown = 0;
            g.querySelectorAll('option').forEach(o => {
                const hit = !needle || o.textContent.toLowerCase().includes(needle) || o.selected;
                o.hidden = !hit;
                if (hit) shown++;
            });
            g.hidden = !shown;
        });
    }

    function rowFormula(row) {
        return formulaText([row.metric, ...rowPlus(row).map(p => p.metric)],
            row.divide_by ? [row.divide_by.metric, ...rowDividePlus(row).map(p => p.metric)] : [],
            plusOps(rowPlus(row)), plusOps(rowDividePlus(row)));
    }

    const keyToMetric = key => {
        const cut = key.indexOf('|');
        return { source: key.slice(0, cut), metric: key.slice(cut + 1) };
    };

    // The added metrics as chosen in the popup, blanks left out. Divide-side
    // ones only count while a divide-by metric is chosen.
    function selectedPlus(side) {
        if (!chartModalState || (side === 'divide' && !selectedDivide())) return [];
        const ops = chartModalState.ops[side];
        // op is only stored when it is not '+', so charts using only plus
        // save exactly as they did before operators could be picked.
        return chartModalState.plus[side]
            .map((key, i) => (key ? { ...keyToMetric(key), ...(ops[i] && ops[i] !== '+' ? { op: ops[i] } : {}) } : null))
            .filter(Boolean);
    }

    // A ratio chart, or one with added metrics: its target is on the chart.
    function selectedOwnTarget() {
        return !!selectedDivide() || selectedPlus('metric').length > 0;
    }

    // "[operator] [metric] [x]" rows under Metric and Divided by. The
    // operator is plus, minus, times or divided by; times and divide are
    // worked out before plus and minus. Sum and Average charts alike: on an
    // Average chart it works on each period's averages.
    function renderPlusLists() {
        if (!chartModalState) return;
        ['metric', 'divide'].forEach(side => {
            const on = side === 'metric' || !!selectedDivide();
            const listEl = document.getElementById(side + 'PlusList');
            const keys = chartModalState.plus[side];
            const ops = chartModalState.ops[side];
            listEl.innerHTML = on ? keys.map((key, i) => {
                const missing = key && !metricsByKey[key]
                    ? `<option value="${escHtml(key)}">${escHtml(keyToMetric(key).metric)}</option>` : '';
                const opOptions = PLUS_OPS.map(([v, label]) =>
                    `<option value="${v}" ${(ops[i] || '+') === v ? 'selected' : ''}>${escHtml(label)}</option>`).join('');
                return `<div class="plus-row">
                    <select class="plus-op" title="How this metric joins the ones before it: + plus, - minus, * times, / divided by" onchange="setPlusOp('${side}', ${i}, this.value)">${opOptions}</select>
                    <select onchange="setPlus('${side}', ${i}, this.value)">
                        <option value="">(choose a metric)</option>${missing}${allMetricGroups()}
                    </select>
                    <button type="button" class="btn btn-sm" title="Remove" onclick="removePlus('${side}', ${i})"><i class="fas fa-times"></i></button>
                </div>`;
            }).join('') : '';
            listEl.querySelectorAll('select:not(.plus-op)').forEach((s, i) => { s.value = keys[i]; });
            document.getElementById(side + 'PlusBtn').style.display = on ? '' : 'none';
        });
        // The whole sum, shown once there is more than the one metric. Times
        // and divide come before plus and minus within each side.
        const m = selectedMetric() || {};
        const divide = selectedDivide();
        const top = selectedPlus('metric'), bottom = selectedPlus('divide');
        document.getElementById('formulaPreview').textContent = top.length || divide
            ? 'Formula: ' + formulaText([m.column || '', ...top.map(p => p.metric)],
                divide ? [divide.metric, ...bottom.map(p => p.metric)] : [], plusOps(top), plusOps(bottom))
            : '';
    }

    function addPlus(side) {
        chartModalState.plus[side].push('');
        chartModalState.ops[side].push('+');
        renderPlusLists();
    }

    function setPlus(side, i, key) {
        chartModalState.plus[side][i] = key;
        onDivideChange();
    }

    function setPlusOp(side, i, op) {
        chartModalState.ops[side][i] = op;
        onDivideChange();
    }

    function removePlus(side, i) {
        chartModalState.plus[side].splice(i, 1);
        chartModalState.ops[side].splice(i, 1);
        onDivideChange();
    }

    function openChartModal(rowId) {
        const row = rowId ? findRow(rowId) : null;
        if (rowId && !row) return;
        chartModalState = { rowId: row ? row.id : null };
        document.getElementById('chartModalTitle').textContent = row ? 'Edit Chart' : 'Add Chart';
        document.getElementById('chartModalError').classList.remove('show');
        document.getElementById('removeChartBtn').style.display = row ? '' : 'none';
        // Cleared first so the last chart's weighting is not carried over.
        document.getElementById('weightSelect').innerHTML = '';
        // The section the chart opens in (as set on sectionSelect below) sets
        // which data sources the metric lists offer.
        const openSection = row ? row.section
            : (activeSection && SECTIONS.includes(activeSection) ? activeSection : SECTIONS[0]);
        metricSourceLock = sectionSources[openSection] || null;

        // Metric list. Can be changed on an existing chart (e.g. a copy pointed
        // at another metric); the chart then uses the new metric's targets.
        const metricSelect = document.getElementById('metricSelect');
        metricSelect.innerHTML = allMetricGroups();
        document.getElementById('metricSearch').value = '';
        if (row && !metricsByKey[metricKey(rowSource(row), row.metric)]) {
            // The column has gone from the list; keep it showing for this chart.
            metricSelect.insertAdjacentHTML('afterbegin',
                `<option value="${escHtml(metricKey(rowSource(row), row.metric))}">${escHtml(row.metric)}</option>`);
        }

        // Divide-by list: the same metrics, plus "nothing" for a plain chart.
        // Unlike the metric it can change later — a ratio chart's target is
        // stored on the chart, not on either metric.
        const divideSelect = document.getElementById('divideSelect');
        divideSelect.innerHTML = '<option value="">(nothing - plain chart)</option>' + allMetricGroups();
        const divideKey = row && row.divide_by
            ? metricKey(row.divide_by.source || 'snowflake', row.divide_by.metric) : '';
        if (divideKey && !metricsByKey[divideKey]) {
            divideSelect.insertAdjacentHTML('beforeend',
                `<option value="${escHtml(divideKey)}">${escHtml(row.divide_by.metric)}</option>`);
        }
        divideSelect.value = divideKey;

        // Multiplied-by list: the same metrics, plus "nothing".
        const multiplySelect = document.getElementById('multiplySelect');
        multiplySelect.innerHTML = '<option value="">(nothing)</option>' + allMetricGroups();
        const multiplyKey = row && row.multiply_by
            ? metricKey(row.multiply_by.source || 'snowflake', row.multiply_by.metric) : '';
        if (multiplyKey && !metricsByKey[multiplyKey]) {
            multiplySelect.insertAdjacentHTML('beforeend',
                `<option value="${escHtml(multiplyKey)}">${escHtml(row.multiply_by.metric)}</option>`);
        }
        multiplySelect.value = multiplyKey;
        document.getElementById('ratioAsSelect').value = row && row.ratio_as === 'dec' ? 'dec' : 'pct';
        chartModalState.plus = {
            metric: row ? rowPlus(row).map(p => metricKey(p.source || 'snowflake', p.metric)) : [],
            divide: row ? rowDividePlus(row).map(p => metricKey(p.source || 'snowflake', p.metric)) : [],
        };
        chartModalState.ops = {
            metric: row ? plusOps(rowPlus(row)) : [],
            divide: row ? plusOps(rowDividePlus(row)) : [],
        };
        chartModalState.wasDivide = !!divideKey;
        // The title the popup would fill in; a title still matching it follows
        // the divide-by and added metric choices, a hand-typed one is left alone.
        chartModalState.autoTitle = row ? rowFormula(row) : null;
        renderPlusLists();

        document.getElementById('sectionSelect').innerHTML =
            SECTIONS.map(s => `<option value="${escHtml(s)}">${escHtml(s)}</option>`).join('');

        // A new chart starts with the hover box in full; an existing one
        // overwrites these below.
        document.getElementById('hoverFmtSelect').value = '';
        document.getElementById('hoverDecimalsSelect').value = '';
        if (row) {
            metricSelect.value = metricKey(rowSource(row), row.metric);
            document.getElementById('metricDesc').textContent = rowMetricInfo(row).description || '';
            document.getElementById('titleInput').value = rowTitle(row);
            document.getElementById('subtitleInput').value = row.subtitle || '';
            document.getElementById('sectionSelect').value = row.section;
            setRadio('aggRadio', row.agg);
            setCurrentInputs(rowCurrent(row));
            document.getElementById('invertSelect').value = row.invert ? '1' : '0';
            document.getElementById('fmtSelect').value = row.fmt || 'num';
            document.getElementById('decimalsSelect').value = String(row.decimals || 0);
            document.getElementById('hoverFmtSelect').value = row.hover_fmt || '';
            document.getElementById('hoverDecimalsSelect').value = row.hover_decimals ?? '';
            fillAxisInputs(Y_MIN_INPUTS, 'yMinAll', row.y_min);
            fillAxisInputs(Y_MAX_INPUTS, 'yMaxAll', row.y_max);
            showYMinInputs();
            fillPositionOptions();
            updateRatioUi();
            updateWeightUi(row.weight_by || '');
        } else {
            document.getElementById('subtitleInput').value = '';
            fillAxisInputs(Y_MIN_INPUTS, 'yMinAll', null);
            fillAxisInputs(Y_MAX_INPUTS, 'yMaxAll', null);
            // A new chart goes into the section tab it was added from.
            if (activeSection && SECTIONS.includes(activeSection)) {
                document.getElementById('sectionSelect').value = activeSection;
            }
            fillPositionOptions();
            onMetricChange();
        }
        openVehSetup(row);
        openChartTypeSetup(row);
        // Reopens on the tab used last, for going through several charts
        // changing the same thing. A new chart starts on Title & Placement.
        showChartTab(row ? chartTab : 'title');
        document.getElementById('chartModal').classList.add('open');
    }

    // ── View Vehicles setup in the Edit Chart popup ──
    // vehDraft is the chart's setting as edited:
    // { table, date_column ('' = off), filters, date_mode: 'range'|'snapshot',
    //   columns: [names] or absent = every column set for the table in Settings }.
    // The chosen table's columns load after the popup opens; until then the
    // saved values still show, so saving early keeps them.
    let vehDraft = { table: '', date_column: '', filters: [], date_mode: 'range' };
    let vehColumns = null;   // [{ column, type }] of the vehicle table, or null while loading
    let vehViewColumns = null;   // the table's Settings columns [{ column, label, ... }], or null while loading
    let vehTable = '';

    // Operators offered for each kind of column, as [operator, label]. The
    // kind comes from the column's Snowflake type, so a true/false column only
    // offers is true / is false and a filter cannot be set up wrongly.
    const EMPTY_OPS = [['is empty', 'is empty'], ['is not empty', 'is not empty']];
    const VEH_FILTER_OPS = {
        bool: [['is true', 'is true'], ['is false', 'is false'], ...EMPTY_OPS],
        num: [['=', 'equals'], ['!=', 'does not equal'], ['>', 'greater than'], ['>=', 'at least'],
              ['<', 'less than'], ['<=', 'at most'], ['in', 'is one of'], ['not in', 'is not one of'], ...EMPTY_OPS],
        date: [['=', 'on'], ['!=', 'not on'], ['<', 'before'], ['<=', 'on or before'],
               ['>', 'after'], ['>=', 'on or after'], ...EMPTY_OPS],
        text: [['=', 'equals'], ['!=', 'does not equal'], ['contains', 'contains'],
               ['in', 'is one of'], ['not in', 'is not one of'], ...EMPTY_OPS],
    };
    const VEH_NO_VALUE_OPS = ['is true', 'is false', 'is empty', 'is not empty'];
    const VEH_DATE_TYPES = ['DATE', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ', 'TIMESTAMP'];

    // 'bool' | 'num' | 'date' | 'text', or null when the column's type is not
    // known (columns still loading, or the column has gone from the table).
    function vehColumnKind(column) {
        const col = (vehColumns || []).find(c => c.column === column);
        if (!col) return null;
        if (col.type === 'BOOLEAN') return 'bool';
        if (VEHICLE_NUMBER_TYPES.includes(col.type)) return 'num';
        if (VEH_DATE_TYPES.includes(col.type)) return 'date';
        return 'text';
    }

    // Unknown type: every operator, so a saved filter still shows as it is.
    function vehOpsFor(kind) {
        if (kind) return VEH_FILTER_OPS[kind];
        const seen = new Set();
        return Object.values(VEH_FILTER_OPS).flat().filter(([op]) => !seen.has(op) && seen.add(op));
    }

    function openVehSetup(row) {
        vehDraft = row && row.vehicles
            ? JSON.parse(JSON.stringify({ filters: [], date_mode: 'range', ...row.vehicles }))
            : { table: '', date_column: '', filters: [], date_mode: 'range' };
        vehColumns = null;
        // Other charts with View Vehicles set up, to copy their whole setup.
        const sources = layout.rows.filter(r => r.vehicles && (!row || r.id !== row.id));
        document.getElementById('vehCopyField').style.display = sources.length ? '' : 'none';
        document.getElementById('vehCopySelect').innerHTML = '<option value="">(choose a chart)</option>'
            + sources.map(r => {
                const count = (r.vehicles.filters || []).length;
                return `<option value="${escHtml(r.id)}">${escHtml(rowTitle(r))} (${count} filter${count === 1 ? '' : 's'})</option>`;
            }).join('');
        renderVehSetup();
        loadVehChartColumns(vehDraft.table);
    }

    // Loads the columns of the chart's chosen table (empty table = the server's
    // default or first configured one), fills the table dropdown, and re-renders.
    function loadVehChartColumns(table) {
        vehColumns = null;
        vehViewColumns = null;
        document.getElementById('vehTableHint').textContent = 'Loading the table columns...';
        renderVehSetup();
        return fetchVehicleColumns(table).then(resp => {
            if (!chartModalState) return;
            vehColumns = resp.columns;
            vehViewColumns = (resp.view && resp.view.columns) || [];
            vehTable = resp.table;
            vehDraft.table = vehDraft.table || resp.table;
            const tables = resp.tables.length ? resp.tables : [resp.table];
            const list = tables.includes(vehDraft.table) ? tables : [vehDraft.table, ...tables];
            const select = document.getElementById('vehChartTableSelect');
            select.innerHTML = list.map(t => `<option value="${escHtml(t)}">${escHtml(t)}</option>`).join('');
            select.value = vehDraft.table;
            document.getElementById('vehTableHint').textContent =
                'Column headings and formats are set per table in Settings. The clicked day, week or month is matched on the date column.';
            renderVehSetup();
        }).catch(err => {
            document.getElementById('vehTableHint').textContent = 'Could not read the vehicle table: ' + err.message;
        });
    }

    // A different table has different columns, so its list starts all ticked.
    function onVehChartTableChange() {
        vehDraft.table = document.getElementById('vehChartTableSelect').value;
        delete vehDraft.columns;
        loadVehChartColumns(vehDraft.table);
    }

    // The Columns Shown tick list: the table's Settings columns in their
    // Settings order. A ticked column that has since gone from Settings is
    // still listed, so it is not dropped without being seen.
    function renderVehColumnList() {
        const list = document.getElementById('vehColumnList');
        if (!vehViewColumns) { list.innerHTML = '<span class="modal-hint">Loading...</span>'; return; }
        const chosen = vehDraft.columns;
        const names = vehViewColumns.map(c => c.column);
        const gone = (chosen || []).filter(n => !names.includes(n)).map(n => ({ column: n, label: `${n} (not in Settings)` }));
        const all = [...vehViewColumns, ...gone];
        list.innerHTML = all.length ? all.map(c => `<label title="${escHtml(c.column)}">
                <input type="checkbox" value="${escHtml(c.column)}" ${!chosen || chosen.includes(c.column) ? 'checked' : ''}
                       onchange="onVehColumnTick()"> ${escHtml(c.label || c.column)}</label>`).join('')
            : '<span class="modal-hint">No columns are set for this table in Settings yet.</span>';
    }

    function onVehColumnTick() {
        vehDraft.columns = [...document.querySelectorAll('#vehColumnList input:checked')].map(el => el.value);
    }

    function setAllVehColumns(on) {
        if (on) delete vehDraft.columns;
        else vehDraft.columns = [];
        renderVehColumnList();
    }

    // Options for a column select, keeping a saved column that is not in the
    // list (still loading, or gone from the table) so it is not lost.
    function vehColumnOptions(columns, selected) {
        const names = columns.map(c => c.column);
        let html = columns.map(c =>
            `<option value="${escHtml(c.column)}">${escHtml(c.column)}</option>`).join('');
        if (selected && !names.includes(selected)) {
            const note = vehColumns ? ' (not in the table)' : '';
            html = `<option value="${escHtml(selected)}">${escHtml(selected)}${note}</option>` + html;
        }
        return html;
    }

    function renderVehSetup() {
        const all = vehColumns || [];
        const dateSelect = document.getElementById('vehDateSelect');
        dateSelect.innerHTML = '<option value="">(off - no View Vehicles button)</option>'
            + vehColumnOptions(all.filter(c => VEH_DATE_TYPES.includes(c.type)), vehDraft.date_column);
        dateSelect.value = vehDraft.date_column || '';
        document.getElementById('vehDateMode').value = vehDraft.date_mode || 'range';
        document.getElementById('vehDateModeField').style.display = vehDraft.date_column ? '' : 'none';
        document.getElementById('vehFiltersField').style.display = vehDraft.date_column ? '' : 'none';
        document.getElementById('vehColumnsField').style.display = vehDraft.date_column ? '' : 'none';
        renderVehColumnList();

        vehDraft.filters.forEach(fitFilterToKind);
        document.getElementById('vehFilterList').innerHTML = vehDraft.filters.map((f, i) => {
            const kind = vehColumnKind(f.column);
            const noValue = VEH_NO_VALUE_OPS.includes(f.op);
            const ops = vehOpsFor(kind).map(([op, label]) =>
                `<option value="${escHtml(op)}" ${op === f.op ? 'selected' : ''}>${escHtml(label)}</option>`).join('');
            // A list ("is one of") is typed as text whatever the column holds.
            const list = f.op === 'in' || f.op === 'not in';
            const inputType = !list && kind === 'num' ? 'number' : !list && kind === 'date' ? 'date' : 'text';
            const placeholder = noValue ? '(no value)' : list ? 'a, b, c' : '';
            return `
                <div class="veh-filter">
                    <select onchange="onVehFilterColumn(${i}, this.value)">
                        <option value="">(choose a column)</option>${vehColumnOptions(all, f.column)}
                    </select>
                    <select onchange="vehDraft.filters[${i}].op = this.value; renderVehSetup()">${ops}</select>
                    <input type="${inputType}" ${inputType === 'number' ? 'step="any"' : ''} maxlength="500"
                           value="${escHtml(noValue ? '' : f.value || '')}" placeholder="${placeholder}"
                           ${noValue ? 'disabled' : ''}
                           oninput="vehDraft.filters[${i}].value = this.value">
                    <button type="button" class="btn btn-remove-chart" title="Remove filter"
                            onclick="removeVehFilter(${i})"><i class="fas fa-trash"></i></button>
                </div>`;
        }).join('');
        // Selected values set after the HTML so a saved column shows chosen.
        document.querySelectorAll('#vehFilterList .veh-filter').forEach((el, i) => {
            el.querySelector('select').value = vehDraft.filters[i].column || '';
        });
    }

    // A filter saved with an operator its column's kind does not offer, e.g.
    // INTEREST_FREE "equals FALSE" from before operators followed the type:
    // a true/false comparison becomes is true / is false, anything else falls
    // back to the kind's first operator. Saved when the chart is next saved.
    function fitFilterToKind(f) {
        const kind = vehColumnKind(f.column);
        if (!kind) return;
        const ops = VEH_FILTER_OPS[kind].map(([op]) => op);
        if (ops.includes(f.op)) return;
        const flag = { true: true, yes: true, 1: true, false: false, no: false, 0: false }[String(f.value || '').trim().toLowerCase()];
        if (kind === 'bool' && (f.op === '=' || f.op === '!=') && flag !== undefined) {
            f.op = (flag === (f.op === '=')) ? 'is true' : 'is false';
        } else {
            f.op = ops[0];
        }
        f.value = '';
    }

    // A new column keeps the operator if its kind offers it; otherwise the
    // filter starts again on that kind's first operator with no value.
    function onVehFilterColumn(i, column) {
        const f = vehDraft.filters[i];
        f.column = column;
        const ops = vehOpsFor(vehColumnKind(column)).map(([op]) => op);
        if (!ops.includes(f.op)) {
            f.op = ops[0];
            f.value = '';
        }
        renderVehSetup();
    }

    // Replaces this chart's whole View Vehicles setup (table, date column, dates
    // and filters) with another chart's. Only the popup changes; Save stores it
    // as usual.
    function copyVehSetup(rowId) {
        const select = document.getElementById('vehCopySelect');
        const source = rowId ? findRow(rowId) : null;
        select.value = '';
        if (!source || !source.vehicles) return;
        if ((vehDraft.date_column || vehDraft.filters.length)
            && !confirm(`Replace this chart's View Vehicles setup with that from ${rowTitle(source)}?`)) return;
        vehDraft = JSON.parse(JSON.stringify({ filters: [], date_mode: 'range', ...source.vehicles }));
        loadVehChartColumns(vehDraft.table);
    }

    function onVehDateChange() {
        vehDraft.date_column = document.getElementById('vehDateSelect').value;
        renderVehSetup();
    }

    function addVehFilter() {
        vehDraft.filters.push({ column: '', op: '=', value: '' });
        renderVehSetup();
    }

    function removeVehFilter(i) {
        vehDraft.filters.splice(i, 1);
        renderVehSetup();
    }

    // The chart's vehicles setting to save, or null when View Vehicles is off.
    function readVehSetup() {
        if (!vehDraft.date_column) return null;
        const filters = vehDraft.filters.map(f => ({
            column: f.column,
            op: f.op,
            value: VEH_NO_VALUE_OPS.includes(f.op) ? '' : String(f.value || '').trim(),
        }));
        filters.forEach(f => {
            if (!f.column) throw new Error('Choose a column for each View Vehicles filter, or remove it.');
            if (!VEH_NO_VALUE_OPS.includes(f.op) && !f.value) throw new Error(`Enter a value for the ${f.column} filter.`);
        });
        // Every Settings column ticked is saved as no list, so columns added in
        // Settings later show on this chart too.
        let columns = vehDraft.columns;
        if (columns && vehViewColumns && vehViewColumns.every(c => columns.includes(c.column))
            && columns.every(n => vehViewColumns.some(c => c.column === n))) columns = undefined;
        if (columns && !columns.length) throw new Error('Tick at least one View Vehicles column.');
        return { table: vehDraft.table || undefined, date_column: vehDraft.date_column,
                 filters, date_mode: vehDraft.date_mode === 'snapshot' ? 'snapshot' : 'range', columns };
    }

    // Adding: a new metric resets the defaults to suit it. Editing: the
    // chart's other settings are kept; only the description and an
    // auto-filled title follow the new metric.
    function onMetricChange() {
        const m = selectedMetric() || {};
        document.getElementById('metricDesc').textContent = m.description || '';
        if (chartModalState && chartModalState.rowId) {
            onDivideChange();
            return;
        }
        const suggested = m.suggested_agg || 'sum';
        setRadio('aggRadio', suggested);
        // Averages and rates read fine part-way through a period; totals do not.
        setCurrentInputs({ m: suggested === 'avg', w: suggested === 'avg', d: suggested === 'avg' });
        showYMinInputs();
        document.getElementById('fmtSelect').value = 'num';
        document.getElementById('decimalsSelect').value = suggested === 'avg' ? '1' : '0';
        document.getElementById('invertSelect').value = '0';
        fillPositionOptions();
        chartModalState.autoTitle = null;   // title follows the metric again
        onDivideChange();
    }

    function selectedMultiply() {
        const key = document.getElementById('multiplySelect').value;
        if (!key) return null;
        const cut = key.indexOf('|');
        return { source: key.slice(0, cut), metric: key.slice(cut + 1) };
    }

    function selectedDivide() {
        const key = document.getElementById('divideSelect').value;
        if (!key) return null;
        const cut = key.indexOf('|');
        return { source: key.slice(0, cut), metric: key.slice(cut + 1) };
    }

    // Shows the ratio settings when a divide-by metric is chosen.
    function updateRatioUi() {
        document.getElementById('ratioAsField').style.display = selectedDivide() ? '' : 'none';
    }

    function onDivideChange() {
        if (!chartModalState) return;
        const m = selectedMetric() || {};
        const divide = selectedDivide();
        const autoTitle = formulaText([m.column || '', ...selectedPlus('metric').map(p => p.metric)],
            divide ? [divide.metric, ...selectedPlus('divide').map(p => p.metric)] : [],
            plusOps(selectedPlus('metric')), plusOps(selectedPlus('divide')));
        const titleInput = document.getElementById('titleInput');
        // Only replace a title the popup filled in, never one typed by hand.
        if (chartModalState.autoTitle === null || titleInput.value === chartModalState.autoTitle) {
            titleInput.value = autoTitle;
            chartModalState.autoTitle = autoTitle;
        }
        if (divide && !chartModalState.wasDivide) onRatioAsChange();
        chartModalState.wasDivide = !!divide;
        renderPlusLists();
        updateRatioUi();
        updateWeightUi();
        updateYMaxPlaceholders();
    }

    function onRatioAsChange() {
        const pct = document.getElementById('ratioAsSelect').value === 'pct';
        document.getElementById('fmtSelect').value = pct ? 'pct' : 'num';
        document.getElementById('decimalsSelect').value = pct ? '1' : '2';
        updateYMaxPlaceholders();
    }

    // Positions 1..n within the chosen section. A chart staying in its own
    // section keeps its place; one arriving goes last.
    function fillPositionOptions() {
        const section = document.getElementById('sectionSelect').value;
        const rowId = chartModalState && chartModalState.rowId;
        const others = layout.rows.filter(r => r.section === section && r.id !== rowId);
        const select = document.getElementById('positionSelect');
        select.innerHTML = Array.from({ length: others.length + 1 }, (_, i) =>
            `<option value="${i}">${i + 1}${i === others.length ? ' (last)' : ''}</option>`).join('');
        const row = rowId ? findRow(rowId) : null;
        const current = row && row.section === section
            ? layout.rows.filter(r => r.section === section).findIndex(r => r.id === rowId)
            : others.length;
        select.value = String(current);
    }

    // Rows with `row` placed at `position` (0-based) within `section`.
    function placeRow(rows, row, section, position) {
        const rest = rows.filter(r => r.id !== row.id);
        const inSection = rest.filter(r => r.section === section);
        const placed = { ...row, section };
        if (position < inSection.length) {
            rest.splice(rest.indexOf(inSection[position]), 0, placed);
        } else {
            const last = inSection[inSection.length - 1];
            rest.splice(last ? rest.indexOf(last) + 1 : rest.length, 0, placed);
        }
        return rest;
    }

    function closeChartModal() {
        chartModalState = null;
        metricSourceLock = null;
        document.getElementById('chartModal').classList.remove('open');
    }

    // The chart box is only on the page for the admin. Without this check the
    // first line below throws for everyone else and the rest of the script
    // never runs (that broke the Data Labels switch for other users).
    if (IS_ADMIN) {
        // Return saves the chart box. Skipped when a button has focus, so Return
        // still presses that button (Cancel, Remove), in the per-date box (Return
        // starts a new line there), and while a save is already running.
        document.getElementById('chartModal').addEventListener('keydown', e => {
            if (e.key !== 'Enter' || e.target.tagName === 'BUTTON' || e.target.tagName === 'TEXTAREA') return;
            e.preventDefault();
            if (!document.getElementById('chartSaveBtn').disabled) saveChartModal();
        });
    }

    // The Targets popup is on the page for everyone (kpi_targets.js).
    // Return saves it, except in the per-date box or on a button.
    document.getElementById('targetModal').addEventListener('keydown', e => {
        if (e.key !== 'Enter' || e.target.tagName === 'BUTTON' || e.target.tagName === 'TEXTAREA') return;
        e.preventDefault();
        if (!document.getElementById('targetSaveBtn').disabled) saveTargetModal();
    });
    // A different target source shows its own fields.
    document.querySelectorAll('#targetModal input[name="targetMode"]')
        .forEach(r => r.addEventListener('change', updateTargetFields));

    function showChartError(message) {
        const box = document.getElementById('chartModalError');
        box.textContent = message;
        box.classList.add('show');
    }

    // One min/max pair for Average charts (they share one scale), three for Sum.
    function showYMinInputs() {
        const avg = radioValue('aggRadio', 'sum') === 'avg';
        document.getElementById('yMinShared').style.display = avg ? '' : 'none';
        document.getElementById('yMinPerPeriod').style.display = avg ? 'none' : '';
        renderPlusLists();
        updateWeightUi();
        updateYMaxPlaceholders();
    }

    // "Weight weeks and months by": only for a plain (not divided) Average
    // chart on daily_summary. The list is every daily_summary metric; the
    // current choice is kept in the list even if its column has gone.
    function updateWeightUi(selected) {
        const select = document.getElementById('weightSelect');
        const current = selected !== undefined ? selected : select.value;
        const m = selectedMetric() || {};
        const show = radioValue('aggRadio', 'sum') === 'avg' && m.source === 'snowflake' && !selectedOwnTarget()
            && chartTypeValue() === 'line';
        document.getElementById('weightField').style.display = show ? '' : 'none';
        const columns = metricsCatalogue.filter(c => c.source === 'snowflake').map(c => c.column);
        if (current && !columns.includes(current)) columns.unshift(current);
        select.innerHTML = '<option value="">(none - plain average of the daily figures)</option>'
            + columns.map(c => `<option value="${escHtml(c)}">${escHtml(c)}</option>`).join('');
        select.value = current || '';
    }

    // The chart's weight_by to save, or '' for none.
    function readWeightBy() {
        return document.getElementById('weightField').style.display === 'none' ? ''
            : document.getElementById('weightSelect').value;
    }

    function fillAxisInputs(inputs, sharedId, byPeriod) {
        const vals = byPeriod || {};
        Object.entries(inputs).forEach(([key, id]) => {
            document.getElementById(id).value = vals[key] === null || vals[key] === undefined ? '' : vals[key];
        });
        const shared = sharedAxisValue(byPeriod);
        document.getElementById(sharedId).value = shared === null ? '' : shared;
    }

    function readAxisInput(id) {
        const raw = document.getElementById(id).value.trim();
        if (raw === '') return null;
        const n = Number(raw);
        if (isNaN(n)) throw new Error('Axis minimums and maximums must be numbers.');
        return n;
    }

    // An Average chart's single value is stored in all three period slots.
    function readAxis(inputs, sharedId) {
        if (radioValue('aggRadio', 'sum') === 'avg') {
            const v = readAxisInput(sharedId);
            return { m: v, w: v, d: v };
        }
        const out = {};
        for (const [key, id] of Object.entries(inputs)) out[key] = readAxisInput(id);
        return out;
    }

    const divideId = div => div ? metricKey(div.source || 'snowflake', div.metric) : '';
    const plusId = list => list.map(p => (p.op || '+') + divideId(p)).join(',');

    // Grey placeholder in each blank maximum box: the top the chart would get
    // from the minimum as typed. Only shown when the loaded data still matches
    // the box (same metric, divide-by and Sum/Average); otherwise "Auto".
    function updateYMaxPlaceholders() {
        const ids = { ...Y_MAX_INPUTS, all: 'yMaxAll' };
        Object.values(ids).forEach(id => { document.getElementById(id).placeholder = 'Auto'; });
        if (!chartModalState || !chartModalState.rowId || chartTypeValue() === 'stacked') return;
        const row = findRow(chartModalState.rowId);
        const d = row && customRowData[row.id];
        const m = selectedMetric();
        const agg = radioValue('aggRadio', 'sum');
        if (!d || !m || m.column !== row.metric || m.source !== rowSource(row)
            || divideId(selectedDivide()) !== divideId(row.divide_by) || agg !== row.agg
            || plusId(selectedPlus('metric')) !== plusId(rowPlus(row))
            || plusId(selectedPlus('divide')) !== plusId(rowDividePlus(row))
            || (row.divide_by && (document.getElementById('ratioAsSelect').value === 'pct') !== (row.ratio_as === 'pct'))) return;

        let yMin;
        try { yMin = readAxis(Y_MIN_INPUTS, 'yMinAll'); } catch (err) { return; }
        const ranges = calcRowRanges(rangeSpecs(d, yMin, null), agg, sharedAxisValue(yMin), null);
        const show = (id, range) => { document.getElementById(id).placeholder = 'Auto: ' + range.yMax; };
        if (agg === 'avg') show('yMaxAll', ranges[0]);
        else ['m', 'w', 'd'].forEach((k, i) => show(Y_MAX_INPUTS[k], ranges[i]));
    }

    // ── Chart type in the Edit Chart popup ───────────────────────────────────
    // Line charts use the metric / divide-by / weight fields; stacked charts
    // use the series list, colour theme and the 100% / segment label options.
    // Both share the title, section, Sum/Average, format, axis, target (fixed
    // only on a stacked chart, against the column total) and View Vehicles.
    let seriesDraft = [];   // [{ source, metric, label, colour (null = theme) }]

    // ── Edit Chart tabs ──
    let chartTab = 'title';

    function showChartTab(tab) {
        // Columns is only there on a stacked chart.
        if (tab === 'columns' && chartTypeValue() !== 'stacked') tab = 'data';
        chartTab = tab;
        document.querySelectorAll('#chartModal .chart-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
        document.querySelectorAll('#chartModal .chart-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === tab));
    }

    function chartTypeValue() {
        return document.getElementById('chartTypeSelect').value;
    }

    function openChartTypeSetup(row) {
        const stacked = !!row && isStacked(row);
        document.getElementById('chartTypeSelect').value = stacked ? 'stacked' : 'line';
        seriesDraft = stacked ? JSON.parse(JSON.stringify(row.series || [])) : [];
        // The name box shows the shared column name.
        seriesDraft.forEach(s => { s.label = seriesBoxName(s); });
        document.getElementById('themeSelect').innerHTML = themes.map(t =>
            `<option value="${escHtml(t.id)}">${escHtml(t.name)}</option>`).join('');
        document.getElementById('themeSelect').value = stacked && row.theme ? row.theme : (themes[0] ? themes[0].id : '');
        document.getElementById('columnLayoutSelect').value = stacked && row.column_layout === 'side_by_side' ? 'side_by_side' : 'stacked';
        document.getElementById('pctInput').checked = stacked && !!row.pct;
        document.getElementById('segLabelsInput').checked = stacked && (!!row.seg_labels || !!row.pct);
        document.getElementById('segLabelPosSelect').value = stacked && row.seg_label_pos ? row.seg_label_pos : 'center';
        onPctChange();
        applyChartTypeUi();
    }

    // A 100% chart always shows its segment values (it has no column totals),
    // so the box is ticked and fixed while 100% is on.
    function onPctChange() {
        // 100% needs stacked columns: side by side switches it off.
        const side = document.getElementById('columnLayoutSelect').value === 'side_by_side';
        const pctBox = document.getElementById('pctInput');
        if (side) pctBox.checked = false;
        pctBox.disabled = side;
        const pct = pctBox.checked;
        const seg = document.getElementById('segLabelsInput');
        if (pct) seg.checked = true;
        seg.disabled = pct;
    }

    // Switching type keeps everything typed; a new stacked chart starts with
    // the metric that was chosen for the line chart as its first series.
    function onChartTypeChange() {
        if (chartTypeValue() === 'stacked' && !seriesDraft.length) {
            const m = selectedMetric();
            if (m) seriesDraft.push({ source: m.source, metric: m.column, label: '', colour: null });
        }
        applyChartTypeUi();
    }

    function applyChartTypeUi() {
        const stacked = chartTypeValue() === 'stacked';
        document.querySelectorAll('#chartModal .stacked-only').forEach(el => { el.style.display = stacked ? '' : 'none'; });
        document.querySelectorAll('#chartModal .line-only').forEach(el => { el.style.display = stacked ? 'none' : ''; });
        if (!stacked && chartTab === 'columns') showChartTab('data');
        if (stacked) {
            ['ratioAsField', 'weightField'].forEach(id => { document.getElementById(id).style.display = 'none'; });
            renderSeriesList();
        } else {
            // Back to a line chart: the line chart's own rules for these fields.
            updateRatioUi();
            updateWeightUi();
        }
        updateYMaxPlaceholders();
    }

    // Options for a series' metric list: every metric, grouped by source,
    // keeping a saved one that has gone from the list.
    function seriesMetricOptions(s, blank = '(choose a metric)') {
        const key = s.metric ? metricKey(s.source || 'snowflake', s.metric) : '';
        const group = source => {
            const items = metricsCatalogue.filter(m => m.source === source);
            if (!items.length) return '';
            return `<optgroup label="${escHtml(sourceLabel(source))}">`
                + items.map(m => {
                    const k = metricKey(m.source, m.column);
                    return `<option value="${escHtml(k)}" ${k === key ? 'selected' : ''}>${escHtml(metricOptionText(m.source, m.column))}</option>`;
                }).join('') + '</optgroup>';
        };
        const missing = key && !metricsByKey[key] ? `<option value="${escHtml(key)}" selected>${escHtml(s.metric)}</option>` : '';
        return `<option value="">${blank}</option>` + missing + allMetricGroups(group);
    }

    function renderSeriesList() {
        const theme = themeById(document.getElementById('themeSelect').value);
        const last = seriesDraft.length - 1;
        // Headings over the boxes, so it is clear the second list multiplies the first.
        const head = seriesDraft.length ? `<div class="series-row series-head">
                <span>Metric</span><span>Multiplied By</span><span>Legend Name</span>
                <span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>` : '';
        document.getElementById('seriesList').innerHTML = head + seriesDraft.map((s, i) => {
            const themeColour = theme.colours[i] || SERIES_SPARE_COLOUR;
            const colour = s.colour || themeColour;
            return `
                <div class="series-row">
                    <select onchange="onSeriesMetric(${i}, this.value)">${seriesMetricOptions(s)}</select>
                    <select title="Multiplied By: each day of the metric times the same day of this one"
                            class="${s.multiply_by && s.multiply_by.metric ? 'series-multiplied' : ''}"
                            onchange="onSeriesMultiply(${i}, this.value)">${seriesMetricOptions(s.multiply_by || {}, '(Not Multiplied)')}</select>
                    <input type="text" maxlength="60" value="${escHtml(s.label || '')}"
                           placeholder="${escHtml(derivedKey(s) ? derivedDefaultName(s) : (s.metric || 'Legend name'))}"
                           title="${derivedKey(s)
                               ? 'Name of this multiplied metric. Saved under Settings > Column Names > Derived Column Names; the column itself keeps its own name'
                               : 'Name in the legend. Shared: every chart using this column takes it (Settings > Column Names)'}"
                           class="${derivedKey(s) ? 'series-multiplied' : ''}"
                           oninput="seriesDraft[${i}].label = this.value">
                    <input type="color" value="${escHtml(colour)}" class="${s.colour ? 'series-own' : ''}"
                           title="${s.colour ? 'Own colour - Reset goes back to the theme' : 'Theme colour - pick one to override it'}"
                           onchange="seriesDraft[${i}].colour = this.value; renderSeriesList()">
                    <button type="button" class="btn ${s.axis === 'secondary' ? 'btn-primary' : 'btn-edit'}"
                            title="${s.axis === 'secondary' ? 'On the right-hand axis, drawn as a line - click to put it back in the columns' : 'Put on a second axis on the right, drawn as a line'}"
                            onclick="seriesDraft[${i}].axis = seriesDraft[${i}].axis === 'secondary' ? 'primary' : 'secondary'; renderSeriesList()">
                        <i class="fas fa-arrows-alt-h"></i></button>
                    <button type="button" class="btn ${s.within_below ? 'btn-primary' : 'btn-edit'}"
                            title="${i === 0 ? 'Nothing below to subtract' : 'Subtract the series below from this one, for when the series below is part of this one (this column then shows only the rest)'}"
                            onclick="seriesDraft[${i}].within_below = !seriesDraft[${i}].within_below; renderSeriesList()"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-minus"></i></button>
                    <button type="button" class="btn btn-edit" title="Back to the theme colour" onclick="seriesDraft[${i}].colour = null; renderSeriesList()"
                            ${s.colour ? '' : 'disabled'}><i class="fas fa-undo"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down the stack" onclick="moveSeries(${i}, -1)"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move up the stack" onclick="moveSeries(${i}, 1)"
                            ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="Remove series" onclick="seriesDraft.splice(${i}, 1); renderSeriesList()">
                        <i class="fas fa-trash"></i></button>
                </div>${splitRow(s, i, colour)}`;
        }).join('') || '<div class="point-status" style="padding:2px 0 6px;">No series yet.</div>';
    }

    // Under each series: Split At. Chosen, the series is drawn in two parts -
    // up to that metric's figure each day, and the part above it stacked on
    // top in its own (lighter) colour, with its own legend name.
    function splitRow(s, i, colour) {
        const on = hasSplit(s);
        const own = on && s.split_at.colour;
        const upperColour = own || lighterColour(colour);
        const at = on ? metricName(s.split_at.source, s.split_at.metric) : '';
        const name = seriesLabel(s) || 'this series';
        // Laid out like the bar: the top part above the bottom part.
        return `
                <div class="series-split">
                    <div class="series-split-head">
                        <span class="series-split-tag">Split ${escHtml(name)} At</span>
                        <select onchange="onSeriesSplit(${i}, this.value)">${seriesMetricOptions(s.split_at || {}, '(Not Split)')}</select>
                    </div>
                    ${on ? `
                    <div class="series-split-help">Each day, ${escHtml(name)} up to ${escHtml(at)} is the bottom part; anything above it is the top part.</div>
                    <div class="series-split-grid">
                        <span class="series-split-col">Part</span><span class="series-split-col">Colour</span>
                        <span class="series-split-col">Name (Legend And Hover)</span><span></span>
                        <span class="series-split-tag">Top</span>
                        <input type="color" value="${escHtml(upperColour)}" class="${own ? 'series-own' : ''}"
                               title="${own ? 'Own colour - the reset button goes back to a lighter shade of the series colour' : 'A lighter shade of the series colour - pick one to override it'}"
                               onchange="seriesDraft[${i}].split_at.colour = this.value; renderSeriesList()">
                        <input type="text" maxlength="60" value="${escHtml(s.split_at.label || '')}" placeholder="${escHtml('Above ' + at)}"
                               title="Name for the part above ${escHtml(at)}, e.g. Tactical"
                               oninput="seriesDraft[${i}].split_at.label = this.value">
                        <button type="button" class="btn btn-edit" title="Back to a lighter shade of the series colour"
                                onclick="seriesDraft[${i}].split_at.colour = null; renderSeriesList()" ${own ? '' : 'disabled'}><i class="fas fa-undo"></i></button>
                        <span class="series-split-tag">Bottom</span>
                        <span class="series-split-swatch" style="background:${escHtml(colour)}" title="The series colour, set on the row above"></span>
                        <input type="text" maxlength="60" value="${escHtml(s.split_at.lower_label || '')}" placeholder="${escHtml(at)}"
                               title="Name for the part up to ${escHtml(at)}, e.g. Base + Flex"
                               oninput="seriesDraft[${i}].split_at.lower_label = this.value">
                        <span></span>
                    </div>` : ''}
                </div>`;
    }

    function onSeriesSplit(i, key) {
        const cut = key.indexOf('|');
        const old = seriesDraft[i].split_at || {};
        seriesDraft[i].split_at = key
            ? { source: cut > 0 ? key.slice(0, cut) : 'snowflake', metric: cut > 0 ? key.slice(cut + 1) : key,
                label: old.label || '', lower_label: old.lower_label || '', colour: old.colour || null }
            : null;
        renderSeriesList();
    }

    function onSeriesMetric(i, key) {
        const cut = key.indexOf('|');
        seriesDraft[i].source = cut > 0 ? key.slice(0, cut) : 'snowflake';
        seriesDraft[i].metric = cut > 0 ? key.slice(cut + 1) : '';
        seriesDraft[i].label = seriesBoxName(seriesDraft[i]);
        renderSeriesList();
    }

    function onSeriesMultiply(i, key) {
        const cut = key.indexOf('|');
        seriesDraft[i].multiply_by = key ? { source: cut > 0 ? key.slice(0, cut) : 'snowflake', metric: cut > 0 ? key.slice(cut + 1) : key } : null;
        // The box now names a different thing (the derived metric, or the column).
        seriesDraft[i].label = seriesBoxName(seriesDraft[i]);
        renderSeriesList();
    }

    function addSeries() {
        seriesDraft.push({ source: 'snowflake', metric: '', label: '', colour: null });
        renderSeriesList();
    }

    function moveSeries(i, step) {
        const j = i + step;
        if (j < 0 || j >= seriesDraft.length) return;
        [seriesDraft[i], seriesDraft[j]] = [seriesDraft[j], seriesDraft[i]];
        renderSeriesList();
    }

    async function saveStackedChart() {
        const existing = chartModalState.rowId ? findRow(chartModalState.rowId) : null;
        document.getElementById('chartModalError').classList.remove('show');
        const series = seriesDraft.filter(s => s.metric).map(s => ({
            source: s.source || 'snowflake', metric: s.metric, label: String(s.label || '').trim(), colour: s.colour || null,
            ...(s.multiply_by && s.multiply_by.metric ? { multiply_by: s.multiply_by } : {}),
            ...(s.within_below ? { within_below: true } : {}),
            ...(s.axis === 'secondary' ? { axis: 'secondary' } : {}),
            ...(hasSplit(s) ? { split_at: { source: s.split_at.source || 'snowflake', metric: s.split_at.metric,
                                            label: String(s.split_at.label || '').trim(),
                                            lower_label: String(s.split_at.lower_label || '').trim(),
                                            colour: s.split_at.colour || null } } : {}),
        }));
        if (!series.length) { showChartError('Add at least one series.'); return; }
        if (series.length !== seriesDraft.length) { showChartError('Choose a metric for each series, or remove it.'); return; }
        // Names typed here go to the shared column names (a cleared box
        // removes that column's name); the series keeps no name of its own.
        // A multiplied series' name goes to the derived names instead, so the
        // column it was made from keeps its own name.
        const names = { ...metricNames };
        const derived = { ...derivedNames };
        series.forEach(s => {
            const dk = derivedKey(s);
            if (dk) {
                if (s.label) derived[dk] = s.label; else delete derived[dk];
            } else {
                const key = metricKey(s.source, s.metric);
                if (s.label) names[key] = s.label; else delete names[key];
            }
            s.label = '';
        });
        let yMin, yMax;
        try {
            yMin = readAxis(Y_MIN_INPUTS, 'yMinAll');
            yMax = readAxis(Y_MAX_INPUTS, 'yMaxAll');
        } catch (err) { showChartError(err.message); return; }
        if (['m', 'w', 'd'].some(k => yMax[k] !== null && yMax[k] <= (yMin[k] || 0))) {
            showChartError('Each axis maximum must be above its minimum (0 when the minimum is blank).');
            return;
        }
        // The target is set in the Targets popup; a stacked chart keeps its
        // target, and a line chart turned stacked starts with none.
        const keepTarget = existing && isStacked(existing) && ['fixed', 'import'].includes(existing.target_mode);
        const mode = keepTarget ? existing.target_mode : 'none';
        const target = existing && isStacked(existing) ? (existing.stack_target ?? null) : null;
        let vehicles;
        try { vehicles = readVehSetup(); } catch (err) { showChartError(err.message); return; }

        const title = document.getElementById('titleInput').value.trim()
            || series.map(s => names[metricKey(s.source, s.metric)] || s.metric).join(' / ').slice(0, 100);
        const updated = {
            ...(existing || { id: 'c' + Date.now().toString(36), type: 'custom' }),
            chart_type: 'stacked',
            // The first series stands in as the chart's metric for anything
            // that reads one (notes, titles).
            source: series[0].source,
            metric: series[0].metric,
            series,
            theme: document.getElementById('themeSelect').value || null,
            column_layout: document.getElementById('columnLayoutSelect').value,
            pct: document.getElementById('pctInput').checked,
            seg_labels: document.getElementById('segLabelsInput').checked,
            seg_label_pos: document.getElementById('segLabelPosSelect').value,
            title,
            subtitle: document.getElementById('subtitleInput').value.trim(),
            agg: radioValue('aggRadio', 'sum'),
            include_current: readCurrentInputs(),
            fmt: document.getElementById('fmtSelect').value,
            decimals: parseInt(document.getElementById('decimalsSelect').value, 10),
            ...hoverFields(),
            invert: document.getElementById('invertSelect').value === '1',
            target_mode: mode,
            stack_target: target,
            y_min: yMin,
            y_max: yMax,
        };
        ['divide_by', 'divide_plus', 'metric_plus', 'ratio_as', 'ratio_target', 'weight_by', 'vehicles',
         'input_targets', 'target_calc'].forEach(k => delete updated[k]);
        if (vehicles) updated.vehicles = vehicles;
        const rows = placeRow(layout.rows, updated,
            document.getElementById('sectionSelect').value,
            parseInt(document.getElementById('positionSelect').value, 10));

        const button = document.getElementById('chartSaveBtn');
        button.disabled = true;
        try {
            const plainChanged = await saveMetricNames(names);
            const derivedChanged = await saveDerivedNames(derived);
            const namesChanged = plainChanged || derivedChanged;
            await saveLayout(rows);
            layout.rows = rows;
            const row = findRow(updated.id);
            closeChartModal();
            activeSection = row.section;
            if (existing) rebuildCustomRow(row, true);
            else { renderLayout(); loadCustomRow(row); showNewRow(row.id); }
            // Other stacked charts on the same columns take the new names.
            if (namesChanged) redrawStackedFromCache(row.id);
        } catch (err) {
            showChartError('Save failed: ' + err.message);
        } finally {
            button.disabled = false;
        }
    }

    async function saveChartModal() {
        if (!chartModalState) return;
        if (chartTypeValue() === 'stacked') return saveStackedChart();
        const existing = chartModalState.rowId ? findRow(chartModalState.rowId) : null;
        const m = selectedMetric();
        const source = m ? m.source : (existing && rowSource(existing));
        const metric = m ? m.column : (existing && existing.metric);
        document.getElementById('chartModalError').classList.remove('show');
        if (!metric) { showChartError('Choose a metric.'); return; }

        let yMin, yMax;
        try {
            yMin = readAxis(Y_MIN_INPUTS, 'yMinAll');
            yMax = readAxis(Y_MAX_INPUTS, 'yMaxAll');
        } catch (err) { showChartError(err.message); return; }
        if (['m', 'w', 'd'].some(k => yMax[k] !== null && yMax[k] <= (yMin[k] || 0))) {
            showChartError('Each axis maximum must be above its minimum (0 when the minimum is blank).');
            return;
        }
        const divide = selectedDivide();
        const metricPlus = selectedPlus('metric');
        const dividePlus = selectedPlus('divide');
        const own = selectedOwnTarget();
        const multiply = selectedMultiply();
        if (multiply && (divide || metricPlus.length)) {
            showChartError('Multiplied By can only be used on a plain chart. Remove Divided By and the added metrics, or set Multiplied By to nothing.');
            return;
        }
        // The target is set in the Targets popup, so the chart keeps its own.
        // A new chart starts on the metric's Snowflake target column when it
        // has one. A change here that leaves the chart unable to use its
        // target option (e.g. Divided By added to a chart on Calculated)
        // turns the target off, and says so.
        const info = metricsByKey[metricKey(source, metric)] || {};
        const wasOwn = !!existing && !isStacked(existing) && rowOwnTarget(existing);
        let mode = existing && !isStacked(existing) ? rowTargetMode(existing) : (info.has_target && !own ? 'snowflake' : 'none');
        // A Snowflake target with no target column already drew nothing.
        if (mode === 'snowflake' && !own && !info.has_target) mode = 'none';
        const allowed = own ? ['none', 'fixed', 'import', 'inputs'] : ['none', 'fixed', 'import', 'calc', ...(info.has_target ? ['snowflake'] : [])];
        // Fixed and per-date figures are the metric's on a plain chart and
        // the chart's own otherwise, so they do not carry across that change.
        const kindChanged = !!existing && !isStacked(existing) && wasOwn !== own && ['fixed', 'import'].includes(mode);
        const targetOff = mode !== 'none' && (!allowed.includes(mode) || kindChanged);
        if (targetOff) mode = 'none';
        let vehicles;
        try { vehicles = readVehSetup(); } catch (err) { showChartError(err.message); return; }

        const updated = {
            ...(existing || { id: 'c' + Date.now().toString(36), type: 'custom' }),
            source,
            metric,
            title: document.getElementById('titleInput').value.trim() || metric,
            subtitle: document.getElementById('subtitleInput').value.trim(),
            agg: radioValue('aggRadio', 'sum'),
            include_current: readCurrentInputs(),
            fmt: document.getElementById('fmtSelect').value,
            decimals: parseInt(document.getElementById('decimalsSelect').value, 10),
            ...hoverFields(),
            invert: document.getElementById('invertSelect').value === '1',
            target_mode: mode,
            y_min: yMin,
            y_max: yMax,
        };
        // A ratio chart keeps its target on the row; the metric's stored
        // fixed figure is left as it was.
        delete updated.divide_by;
        delete updated.ratio_as;
        delete updated.metric_plus;
        delete updated.divide_plus;
        if (divide) {
            updated.divide_by = divide;
            updated.ratio_as = document.getElementById('ratioAsSelect').value;
            if (dividePlus.length) updated.divide_plus = dividePlus;
        }
        if (metricPlus.length) updated.metric_plus = metricPlus;
        delete updated.multiply_by;
        if (multiply) updated.multiply_by = multiply;
        if (!own) delete updated.ratio_target;
        if (own && mode === 'inputs') updated.input_targets = rowInputTargets(updated);
        else delete updated.input_targets;
        if (mode !== 'calc') delete updated.target_calc;
        delete updated.vehicles;
        if (vehicles) updated.vehicles = vehicles;
        ['chart_type', 'series', 'theme', 'pct', 'seg_labels', 'seg_label_pos', 'column_layout', 'stack_target'].forEach(k => delete updated[k]);
        delete updated.weight_by;
        const weightBy = readWeightBy();
        if (weightBy) updated.weight_by = weightBy;
        const rows = placeRow(layout.rows, updated,
            document.getElementById('sectionSelect').value,
            parseInt(document.getElementById('positionSelect').value, 10));

        const button = document.getElementById('chartSaveBtn');
        button.disabled = true;
        try {
            // The page only changes once the server has the new layout.
            await saveLayout(rows);
            layout.rows = rows;
            const row = findRow(updated.id);
            closeChartModal();
            // Moved to another section: follow it to that tab.
            activeSection = row.section;

            const ratioKey = r => JSON.stringify([r.divide_by || null, r.ratio_as || null, r.ratio_target ?? null,
                rowPlus(r), rowDividePlus(r), r.input_targets || null, r.target_calc || null]);
            const needsData = !existing || existing.metric !== row.metric || rowSource(existing) !== row.source
                || existing.agg !== row.agg || JSON.stringify(rowCurrent(existing)) !== JSON.stringify(rowCurrent(row)) || rowTargetMode(existing) !== row.target_mode
                || ratioKey(existing) !== ratioKey(row) || (existing.weight_by || '') !== (row.weight_by || '')
                || JSON.stringify(existing.multiply_by || null) !== JSON.stringify(row.multiply_by || null);
            if (existing) rebuildCustomRow(row, needsData);
            else {
                renderLayout();
                loadCustomRow(row);
                showNewRow(row.id);
            }
            // A reload already started says "Loading..." until it finishes.
            if (targetOff) {
                document.getElementById('statusText').textContent =
                    `${rowTitle(row)}: its target no longer fits the chart, so it was turned off. Set it again with Targets.`;
            } else if (!needsData) {
                document.getElementById('statusText').textContent = 'Chart saved';
            }
        } catch (err) {
            showChartError('Save failed: ' + err.message);
        } finally {
            button.disabled = false;
        }
    }

    // Duplicate a chart row, settings and all, directly under the original in
    // the same section. Fixed and per-date targets belong to the metric, so
    // the copy shares them; a ratio chart's fixed target is copied with the
    // row, its per-date figures (stored under the chart's id) are copied too.
    // Figures drawn on a row's charts (line point values, stacked column
    // totals and segment values). The admin's choice is saved on the row and
    // is everyone's default; a row saved before this setting existed shows
    // them. Anyone else can change it for themselves, kept in their browser
    // only, the same way collapsed rows are.
    const DATA_LABELS_KEY = 'kpiDataLabels';
    function ownDataLabels() {
        try { return JSON.parse(localStorage.getItem(DATA_LABELS_KEY) || '{}') || {}; }
        catch (e) { return {}; }
    }
    function dataLabelsOn(row) {
        const own = IS_ADMIN ? undefined : ownDataLabels()[row.id];
        return typeof own === 'boolean' ? own : row.data_labels !== false;
    }

    async function setDataLabels(rowId, on) {
        const row = findRow(rowId);
        if (!row || dataLabelsOn(row) === on) return;   // already showing that
        if (!IS_ADMIN) {
            const own = ownDataLabels();
            own[rowId] = on;
            try { localStorage.setItem(DATA_LABELS_KEY, JSON.stringify(own)); } catch (e) {}
            rebuildCustomRow(row, false);
            return;
        }
        // Redraw straight away and save in the background: waiting for the save
        // first made the switch feel slow. Put it back if the save fails.
        const before = layout.rows;
        const rows = before.map(r => r.id === rowId ? { ...r, data_labels: on } : r);
        layout.rows = rows;
        rebuildCustomRow(findRow(rowId), false);
        try {
            await saveLayout(rows);
        } catch (err) {
            if (layout.rows === rows) {
                layout.rows = before;
                rebuildCustomRow(findRow(rowId), false);
            }
            document.getElementById('statusText').textContent = 'Data labels change failed: ' + err.message;
        }
    }

    // Swap a chart row with the next shown row above (-1) or below (1) it in
    // its section; hidden rows are stepped over. Nothing happens at either
    // end. The page moves first and is put back if the save fails.
    let moveSaving = false;
    async function moveRow(rowId, step) {
        if (moveSaving) return;
        const row = findRow(rowId);
        if (!row) return;
        const visible = layout.rows.filter(r => r.section === row.section && !r.hidden);
        const other = visible[visible.indexOf(row) + step];
        if (!other) return;
        const before = layout.rows;
        const rows = [...before];
        const a = rows.indexOf(row), b = rows.indexOf(other);
        [rows[a], rows[b]] = [rows[b], rows[a]];
        layout.rows = rows;
        renderLayout();
        moveSaving = true;
        try {
            await saveLayout(rows);
        } catch (err) {
            if (layout.rows === rows) { layout.rows = before; renderLayout(); }
            document.getElementById('statusText').textContent = 'Move failed: ' + err.message;
        } finally {
            moveSaving = false;
        }
    }

    async function copyChartBelow(rowId) {
        const row = findRow(rowId);
        if (!row) return;
        const copy = {
            ...JSON.parse(JSON.stringify(row)),
            id: 'c' + Date.now().toString(36),
            // The server rejects titles over 100 characters.
            title: `${rowTitle(row).slice(0, 93)} (copy)`,
        };
        const rows = [...layout.rows];
        rows.splice(rows.indexOf(row) + 1, 0, copy);
        try {
            // A ratio or added-metrics chart's per-date figures are under its
            // own id, so the copy needs its own set.
            if (!isStacked(row) && rowOwnTarget(row)) {
                const dates = await fetchTargetDates({ row_id: row.id });
                if (dates.length) await saveTargetDates({ row_id: copy.id }, dates.map(([date, value]) => ({ date, value })));
            }
            await saveLayout(rows);
        } catch (err) {
            document.getElementById('statusText').textContent = 'Copy failed: ' + err.message;
            return;
        }
        layout.rows = rows;
        renderLayout();
        loadCustomRow(copy);
        showNewRow(copy.id);
    }

    // A chart just added or copied: scroll it to the middle of the screen and
    // flash it so it is clear where it went. After a frame so the row has its height.
    function showNewRow(id) {
        requestAnimationFrame(() => {
            const el = customRowEls[id];
            if (!el) return;
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            el.classList.remove('row-flash');
            void el.offsetWidth;
            el.classList.add('row-flash');
            el.addEventListener('animationend', () => el.classList.remove('row-flash'), { once: true });
        });
    }

    async function removeChart() {
        if (!chartModalState || !chartModalState.rowId) return;
        const row = findRow(chartModalState.rowId);
        if (!row || !confirm(`Remove the ${rowTitle(row)} charts?`)) return;
        const rows = layout.rows.filter(r => r.id !== row.id);
        try {
            await saveLayout(rows);
        } catch (err) {
            showChartError('Remove failed: ' + err.message);
            return;
        }
        layout.rows = rows;
        dropRowElement(row.id);
        delete customRowData[row.id];
        closeChartModal();
        renderLayout();
        document.getElementById('statusText').textContent = 'Chart removed';
    }

    // ── Settings modal (admin only) ───────────────────────────────────────────
    const SETTING_INPUTS = {
        months: 'setMonths', weeks: 'setWeeks', days_window: 'setDaysWindow',
        days_total: 'setDaysTotal', label_angle: 'setLabelAngle', label_below: 'setLabelBelow',
        months_after: 'setMonthsAfter', weeks_after: 'setWeeksAfter', days_after: 'setDaysAfter',
        section_font_px: 'setSectionFont', title_font_px: 'setTitleFont',
        subtitle_font_px: 'setSubtitleFont', period_font_px: 'setPeriodFont',
        toolbar_font_px: 'setToolbarFont', at_font_px: 'setAtFont', at_table_font_px: 'setAtTableFont',
        legend_font_px: 'setLegendFont', line_label_font_px: 'setLineLabelFont',
        zoom_size: 'setZoomSize',
        stack_label_dir: 'setStackLabelDir', stack_label_overflow: 'setStackLabelOverflow',
        stack_seg_font_px: 'setStackSegFont', stack_total_font_px: 'setStackTotalFont',
        stack_seg_min_px: 'setStackSegMin', stack_min_col_px: 'setStackMinCol',
        main_line_px: 'setMainLinePx', n1_line_px: 'setN1LinePx', target_line_px: 'setTargetLinePx',
        main_line_colour: 'setMainLineColour', n1_line_colour: 'setN1LineColour',
        target_line_colour: 'setTargetLineColour',
        targets_for_all: 'setTargetsForAll',
    };
    // A setting's value from its box: a colour as #rrggbb, a thickness to the
    // half pixel, everything else a whole number.
    function readSettingInput(el) {
        if (el.type === 'color') return el.value;
        return el.step === '1' ? parseInt(el.value, 10) : parseFloat(el.value);
    }

    // Chart legend text size (Settings > Appearance). Read when a chart is
    // built, so saving the setting redraws with it.
    function legendFontPx() { return settings.legend_font_px || 10; }

    // Section heading, chart title and subtitle sizes (Settings > Appearance),
    // applied to the whole page through CSS variables.
    function applyAppearance() {
        const root = document.documentElement.style;
        if (settings.section_font_px) root.setProperty('--kpi-section-font', settings.section_font_px + 'px');
        if (settings.title_font_px) root.setProperty('--kpi-title-font', settings.title_font_px + 'px');
        if (settings.subtitle_font_px) root.setProperty('--kpi-subtitle-font', settings.subtitle_font_px + 'px');
        if (settings.period_font_px) root.setProperty('--kpi-period-font', settings.period_font_px + 'px');
        if (settings.toolbar_font_px) root.setProperty('--kpi-toolbar-font', settings.toolbar_font_px + 'px');
        if (settings.at_font_px) root.setProperty('--kpi-at-font', settings.at_font_px + 'px');
        if (settings.at_table_font_px) root.setProperty('--kpi-at-table-font', settings.at_table_font_px + 'px');
        if (settings.wbr_columns) root.setProperty('--wbr-columns', settings.wbr_columns);
        // Settings > Targets: Targets button for every user or the admin only.
        // The admin sees it either way, except in User View when it is off.
        document.body.classList.toggle('targets-off', Number(settings.targets_for_all) === 0);
        document.body.classList.toggle('not-admin', !IS_ADMIN);
    }

    // The section tabs pin just under the top bar, so they need its height.
    // Watched rather than read once: the Top Bar Size setting and window
    // width both change it.
    (function trackTopBarHeight() {
        const bar = document.querySelector('.kpi-top-bar');
        const set = () => document.documentElement.style.setProperty(
            '--kpi-top-bar-h', bar.getBoundingClientRect().height + 'px');
        new ResizeObserver(set).observe(bar);
        set();
    })();

    // ── Autotrader allowance and costs (Settings > Autotrader) ───────────────
    // Edited as a copy; Save sends the whole list, sorted by date on the server.
    let atDraft = [];
    let atPpcDraft = [];
    const AT_FIELDS = ['base', 'flex_pct', 'tactical', 'me_cost', 'finance_cost', 'tactical_cost', 'connect_cost'];
    // Set when Apply Invoices saves the rows itself, so the charts reload
    // when the box is closed or saved.
    let atRowsSaved = false;
    const AT_FIELD_LABELS = { base: 'Base Slots', flex_pct: 'Flex %', tactical: 'Tactical Slots',
        me_cost: 'Market Extension', finance_cost: 'Finance Calculator', tactical_cost: 'Tactical',
        connect_cost: 'Connect' };
    function loadAtDraft() {
        atDraft = atPeriods.map(p => ({ ...p }));
        atPpcDraft = atPpcPeriods.map(p => ({ ...p }));
        renderAtPpcDraft();
        document.getElementById('atApiTotal').textContent =
            atApiCapacity === null ? 'not available' : atApiCapacity.toLocaleString('en-GB');
        document.getElementById('atEmailMessages').innerHTML = '';
        document.getElementById('atInvoiceMessages').innerHTML = '';
        document.getElementById('atTacticalMessages').innerHTML = '';
        // Everything starts collapsed: the two lists and every year and month of rows.
        document.querySelectorAll('[data-at-box]').forEach(h => setAtBox(h, false));
        atCollapseAll(true);
        renderAtDraft();
        loadAtInvoices();
    }

    // Flex and the totals worked out the same way as the server (kpi.py
    // _at_setting_values): flex rounded half up to a whole slot.
    function atWorked(p) {
        const base = Number(p.base) || 0;
        const flex = Math.floor(base * (Number(p.flex_pct) || 0) / 100 + 0.5);
        const totalSlots = base + flex + (Number(p.tactical) || 0);
        const monthTotal = ['me_cost', 'finance_cost', 'tactical_cost', 'connect_cost']
            .reduce((t, f) => t + (Number(p[f]) || 0), 0);
        // Per slot per day: estimate costs are a month's, invoice costs are
        // for the row's From to To days.
        const days = p.source === 'invoice' && p.from && p.to ? atDayCount(p.from, p.to)
            : p.from ? atDaysInMonth(p.from) : null;
        const tactical = Number(p.tactical) || 0;
        // Base + flex slots carry Market Extension, Finance Calculator and Connect;
        // tactical is costed on its own.
        const baseFlexSlotDay = days && base + flex && p.me_cost !== null && p.me_cost !== undefined
            ? (Number(p.me_cost) + (Number(p.finance_cost) || 0) + (Number(p.connect_cost) || 0)) / days / (base + flex) : null;
        const tacticalSlotDay = days && tactical && p.tactical_cost !== null && p.tactical_cost !== undefined
            ? Number(p.tactical_cost) / days / tactical : null;
        return { flex, totalSlots, monthTotal, baseFlexSlotDay, tacticalSlotDay };
    }

    const atFmt = (v, dp = 0) => (v === null || v === undefined ? '-'
        : Number(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp }));
    // Money is kept to the penny, as Autotrader's own figures are.
    const atPence = v => Math.round(Number(v) * 100) / 100;
    const atDateUk = iso => (iso ? iso.split('-').reverse().join('/') : '-');
    // 'YYYY-MM-DD' plus n days, worked in UTC so clock changes cannot shift it.
    const atAddDays = (iso, n) => {
        const d = new Date(iso + 'T00:00:00Z');
        d.setUTCDate(d.getUTCDate() + n);
        return d.toISOString().slice(0, 10);
    };

    // The last day each row covers: its To, or the day before the next row
    // starts, or null when it runs on (the last row with no To).
    function atRowEnds(rows) {
        const sorted = [...rows].filter(p => p.from).sort((a, b) => a.from.localeCompare(b.from));
        return sorted.map((p, k) => {
            const next = sorted[k + 1];
            const untilNext = next ? atAddDays(next.from, -1) : null;
            const end = p.to && (!untilNext || p.to < untilNext) ? p.to : untilNext;
            return { row: p, end };
        });
    }

    // Apply Invoices To Rows: the invoice rows (one per change, with From and
    // To) replace every invoice row in the list, and every email row for the
    // days the invoices cover. An email row that runs on past the last
    // invoiced day is kept, from the day after it. Saved with Save.
    async function applyAtInvoiceRows() {
        let resp;
        try {
            resp = await fetch(BASE_URL + '/kpi/at-invoices/rows').then(atReadJson);
            if (resp.status !== 'ok') throw new Error(resp.message || 'failed');
        } catch (err) {
            atMessages('atInvoiceMessages', [{ kind: 'err', text: 'Apply Invoices: ' + err.message }]);
            return;
        }
        if (!resp.rows.length) {
            atMessages('atInvoiceMessages', [{ kind: 'warn', text: 'No invoices imported yet, so there is nothing to apply.' }]);
            return;
        }
        const last = resp.coverage.to;
        const kept = [];
        let dropped = 0;
        atRowEnds(atDraft).forEach(({ row, end }) => {
            if (row.source === 'invoice') return;                  // replaced by the new invoice rows
            if (row.from > last) { kept.push(row); return; }
            if (end === null || end > last) { kept.push({ ...row, from: atAddDays(last, 1) }); return; }
            dropped++;
        });
        // A blank From (a row still being filled in) is kept as it is.
        atDraft = [...resp.rows.map(r => ({ ...r })), ...kept, ...atDraft.filter(p => !p.from)];
        // PPC Cost rows: one per invoiced month at its cost per click. They
        // replace the PPC rows starting on or before the last invoiced day; a
        // replaced row that ran on past it is kept from the day after, as the
        // price until the next invoice. Rows starting later are kept.
        let ppc = atPpcPeriods.map(p => ({ ...p }));
        if (resp.ppc_rows.length) {
            const sortedPpc = [...ppc].sort((a, b) => a.from.localeCompare(b.from));
            const keptPpc = sortedPpc.filter(p => p.from > last);
            const runsOn = sortedPpc.filter(p => p.from <= last).pop();
            if (runsOn && !keptPpc.some(p => p.from === atAddDays(last, 1))) keptPpc.push({ ...runsOn, from: atAddDays(last, 1) });
            ppc = [...resp.ppc_rows.map(p => ({ ...p })), ...keptPpc];
        }
        const done = `Added ${resp.rows.length} invoice rows covering ${atDateUk(resp.coverage.from)} to ${atDateUk(last)}`
            + (dropped ? `, replacing ${dropped} email row${dropped === 1 ? '' : 's'} for those days` : '')
            + (resp.ppc_rows.length ? `, and ${resp.ppc_rows.length} PPC cost per click row${resp.ppc_rows.length === 1 ? '' : 's'}` : '');
        const notes = resp.ppc_missing.length ? [{ kind: 'warn', text: 'No PPC clicks stored for '
            + resp.ppc_missing.join(', ') + ' (imported before clicks were read): import those PDFs again, then Apply.' }] : [];
        // Saved straight away, like an imported invoice, so closing the box
        // without Save does not lose them. PPC rows being edited by hand are
        // replaced by what is saved here.
        try {
            await atPostPeriods(atRowsPayload(), ppc);
            atPpcDraft = atPpcPeriods.map(p => ({ ...p }));
            renderAtPpcDraft();
            atRowsSaved = true;
            atMessages('atInvoiceMessages', [{ kind: 'ok', text: done + '. Saved.' }, ...notes]);
        } catch (err) {
            atMessages('atInvoiceMessages', [{ kind: 'err', text: done + ', but NOT saved: ' + err.message
                + ' Fix that row, then press Save.' }, ...notes]);
        }
        renderAtDraft();
    }

    const atDaysInMonth = iso => { const [y, m] = iso.split('-').map(Number); return new Date(Date.UTC(y, m, 0)).getUTCDate(); };
    const atMonthEnd = iso => iso.slice(0, 8) + String(atDaysInMonth(iso)).padStart(2, '0');
    const atDayCount = (a, b) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 864e5) + 1;
    const AT_PRODUCT_COSTS = ['me_cost', 'finance_cost', 'tactical_cost', 'connect_cost'];
    const atMonthLabel = iso => new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' });

    // Each row cut into one piece per calendar month it covers, worked out the
    // same way as the charts (kpi.py _at_setting_values): a month's cost / the
    // days in that month, per day. The last row with no To is shown to the end
    // of its month. Returns pieces in date order:
    // {i, key, first, start, end, days, cost, slotCost, slotDays, perSlotDay}.
    function atPieces() {
        const index = new Map(atDraft.map((p, i) => [p, i]));
        const pieces = [];
        atRowEnds(atDraft).forEach(({ row, end }) => {
            const i = index.get(row);
            const last = end || atMonthEnd(row.from);
            if (last < row.from) return;
            const w = atWorked(row);
            // Invoice rows hold the cost for their own From to To days; email
            // rows hold a month's cost.
            const rowDays = row.source === 'invoice' && row.to ? atDayCount(row.from, row.to) : null;
            for (let start = row.from, first = true; start <= last; first = false) {
                const pieceEnd = atMonthEnd(start) < last ? atMonthEnd(start) : last;
                const dim = rowDays || atDaysInMonth(start);
                const days = atDayCount(start, pieceEnd);
                // Connect is shared over the slots like every other product.
                const slotPerDay = AT_PRODUCT_COSTS.reduce((t, f) => t + (Number(row[f]) || 0), 0) / dim;
                const totalPerDay = slotPerDay;
                // Each product's cost for these days, for the month totals.
                const products = Object.fromEntries(AT_PRODUCT_COSTS.map(f => [f, (Number(row[f]) || 0) / dim * days]));
                pieces.push({ i, key: first ? `r${i}` : `r${i}-${start.slice(0, 7)}`, first, start, end: pieceEnd, days,
                              cost: totalPerDay * days, slotCost: slotPerDay * days, slotDays: w.totalSlots * days,
                              perSlotDay: w.totalSlots ? slotPerDay / w.totalSlots : null, products });
                start = atAddDays(pieceEnd, 1);
            }
        });
        return pieces;
    }

    // Running total within each month, and each month's total: cost, days
    // covered (of the days in the month), and cost per slot per day for the
    // whole month = slot cost / slot-days (not an average of the rows).
    function atFigures() {
        const pieces = atPieces();
        const months = new Map();
        pieces.forEach(p => {
            const m = p.start.slice(0, 7);
            const t = months.get(m) || { cost: 0, slotCost: 0, slotDays: 0, days: 0, dim: atDaysInMonth(p.start), last: null,
                                         products: Object.fromEntries(AT_PRODUCT_COSTS.map(f => [f, 0])) };
            t.cost += p.cost; t.slotCost += p.slotCost; t.slotDays += p.slotDays; t.days += p.days;
            AT_PRODUCT_COSTS.forEach(f => { t.products[f] += p.products[f]; });
            p.running = t.cost;
            t.last = p.key;
            months.set(m, t);
        });
        // Each year's totals, added up from its months the same way.
        const years = new Map();
        months.forEach((t, m) => {
            const y = m.slice(0, 4);
            const yt = years.get(y) || { cost: 0, slotCost: 0, slotDays: 0, days: 0,
                                         dim: atDayCount(`${y}-01-01`, `${y}-12-31`), last: null,
                                         products: Object.fromEntries(AT_PRODUCT_COSTS.map(f => [f, 0])) };
            yt.cost += t.cost; yt.slotCost += t.slotCost; yt.slotDays += t.slotDays; yt.days += t.days;
            AT_PRODUCT_COSTS.forEach(f => { yt.products[f] += t.products[f]; });
            yt.last = m;
            years.set(y, yt);
        });
        return { pieces, months, years };
    }

    // Download Full CSV: one line per row per calendar month it covers, the
    // same figures as View Cost Breakdown, unrounded.
    function downloadAtBreakdownCsv() {
        // Money to the penny.
        const cell = v => { const s = v === null || v === undefined ? '' : typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(2) : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
        const head = ['From', 'To', 'Source', 'Month', 'Base Slots', 'Flex %', 'Flex Slots', 'Tactical Slots', 'Total Slots',
            'Market Extension', 'Finance Calculator', 'Connect', 'Base + Flex Per Slot Per Day',
            'Tactical', 'Tactical Per Slot Per Day', 'Cost Per Slot Per Day', 'Days In This Month', 'Market Extension For These Days', 'Finance Calculator For These Days',
            'Connect For These Days', 'Tactical For These Days', 'Cost', 'Month Running Total'];
        const { pieces } = atFigures();
        const lines = pieces.map(p => {
            const r = atDraft[p.i], w = atWorked(r);
            return [r.from, r.to || '', r.source === 'invoice' ? 'Invoice' : 'Estimate', p.start.slice(0, 7), r.base, r.flex_pct,
                w.flex, r.tactical, w.totalSlots, r.me_cost, r.finance_cost, r.connect_cost,
                w.baseFlexSlotDay, r.tactical_cost, w.tacticalSlotDay, p.perSlotDay, p.days, p.products.me_cost, p.products.finance_cost, p.products.connect_cost,
                p.products.tactical_cost, p.cost, p.running];
        });
        const blob = new Blob([[head].concat(lines).map(r => r.map(cell).join(',')).join('\r\n')], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'autotrader_cost_breakdown.csv';
        a.click();
        URL.revokeObjectURL(a.href);
    }

    const atCpspd = v =>(v === null || v === undefined ? '-' : atFmt(v, 2));

    // Invoices and PPC Cost lists: shown or hidden by clicking their heading.
    // Both start collapsed each time the popup opens.
    function setAtBox(heading, show) {
        document.getElementById(heading.dataset.atBox).style.display = show ? '' : 'none';
        heading.querySelector('i').className = 'fas fa-chevron-' + (show ? 'down' : 'right');
    }
    function toggleAtBox(heading) {
        setAtBox(heading, document.getElementById(heading.dataset.atBox).style.display === 'none');
    }

    // Years and months folded to their total line. Kept while the popup is open.
    const atCollapsed = new Set();
    const atCollapsedYears = new Set();
    function toggleAtMonth(m) {
        if (atCollapsed.has(m)) atCollapsed.delete(m); else atCollapsed.add(m);
        renderAtDraft();
    }
    function toggleAtYear(y) {
        if (atCollapsedYears.has(y)) atCollapsedYears.delete(y); else atCollapsedYears.add(y);
        renderAtDraft();
    }
    // Folds (or opens) every year and every month.
    function atCollapseAll(collapse) {
        atCollapsed.clear();
        atCollapsedYears.clear();
        if (collapse) {
            const { months, years } = atFigures();
            months.forEach((t, m) => atCollapsed.add(m));
            years.forEach((t, y) => atCollapsedYears.add(y));
        }
    }
    // View Source Data: estimate rows show their stored monthly costs (editable)
    // instead of the cost for the row's dates.
    let atShowSource = false;
    function toggleAtSource() {
        atShowSource = !atShowSource;
        document.getElementById('atSourceBtn').innerHTML = atShowSource
            ? '<i class="fas fa-calendar-alt"></i> View Costs For Dates'
            : '<i class="fas fa-database"></i> View Source Data';
        document.querySelectorAll('#atRowsModal th[data-at-money]').forEach(th => {
            th.textContent = th.dataset.atMoney + (atShowSource ? ' (£ A Month, Estimates)' : ' (£)');
        });
        renderAtDraft();
    }

    function setAtMonthsCollapsed(collapse) {
        atCollapseAll(collapse);
        renderAtDraft();
    }

    function renderAtDraft() {
        // Money boxes show 2 decimals; the full figure stays in the draft and
        // is saved unchanged unless the box is typed in.
        const money = new Set(['me_cost', 'finance_cost', 'tactical_cost', 'connect_cost']);
        const box = (i, field, step) => {
            const v = atDraft[i][field];
            const shown = v === null || v === undefined || v === '' ? '' : money.has(field) ? Number(v).toFixed(2) : v;
            return `<input type="number" min="0" step="${step}" value="${shown}" data-field="${field}"
                        oninput="setAtField(${i}, '${field}', this.value)">`;
        };
        // Dates re-draw the table when the box is left (blur), not on every
        // key: a re-draw while typing would replace the box being typed in.
        const dateBox = (i, field, title = '') => `<input type="date" value="${escHtml(atDraft[i][field] || '')}" title="${title}"
                        oninput="setAtField(${i}, '${field}', this.value)" onblur="renderAtDraft()">`;
        // Estimate rows store a month's cost. Shown as the cost for the row's own
        // dates, like invoice rows, unless View Source Data is on, which shows
        // the stored monthly figure in an edit box.
        const moneyCell = (i, field, p) => (atDraft[i].source === 'invoice' || atShowSource || !p)
            ? box(i, field, 'any')
            : `<div class="at-period-sum" data-key="${p.key}" data-cell="p_${field}" title="Cost for this row's dates. View Source Data to edit the monthly figure">${atFmt(p.products[field], 2)}</div>`;
        const editRow = (i, p) => {
            const w = atWorked(atDraft[i]);
            return `<tr data-at="${i}">
                <td>${dateBox(i, 'from')}</td>
                <td>${dateBox(i, 'to', 'Blank: until the next row')}</td>
                <td title="${atDraft[i].source === 'invoice' ? 'Costs are for this row\'s From to To days' : 'Costs are per month'}">${atDraft[i].source === 'invoice' ? 'Invoice' : 'Estimate'}</td>
                <td>${box(i, 'base', 1)}</td>
                <td>${box(i, 'flex_pct', 'any')}</td>
                <td>${box(i, 'tactical', 1)}</td>
                <td class="num" data-cell="slots">${atFmt(w.totalSlots)}</td>
                <td>${moneyCell(i, 'me_cost', p)}</td>
                <td>${moneyCell(i, 'finance_cost', p)}</td>
                <td>${moneyCell(i, 'connect_cost', p)}</td>
                <td class="num" data-cell="bfslot">${atCpspd(w.baseFlexSlotDay)}</td>
                <td>${moneyCell(i, 'tactical_cost', p)}</td>
                <td class="num" data-cell="tacslot">${atCpspd(w.tacticalSlotDay)}</td>
                <td class="at-cpspd" data-key="${p ? p.key : ''}" data-cell="cpspd">${p ? atCpspd(p.perSlotDay) : '-'}</td>
                <td class="num" data-key="${p ? p.key : ''}" data-cell="days">${p ? p.days : '-'}</td>
                <td class="num" data-key="${p ? p.key : ''}" data-cell="cost">${p ? atFmt(p.cost, 2) : '-'}</td>
                <td class="num" data-key="${p ? p.key : ''}" data-cell="running">${p ? atFmt(p.running, 2) : '-'}</td>
                <td><button type="button" class="btn btn-remove-chart" title="Delete Row" onclick="removeAtPeriod(${i})">
                    <i class="fas fa-trash"></i></button></td>
            </tr>`;
        };
        const { pieces, months, years } = atFigures();
        const html = [];
        // Rows still being filled in (no From yet) at the top, so they are seen.
        atDraft.forEach((p, i) => { if (!p.from) html.push(editRow(i, null)); });
        // A month's or year's totals (level 'month' or 'year'): under each
        // product column, that product's cost for the days covered (what the
        // invoice charged, for invoice rows).
        const totalCells = (level, k, t) => {
            const at = `data-${level}="${k}"`;
            const prod = f => `<td class="num" ${at} data-cell="${f}">${atFmt(t.products[f], 2)}</td>`;
            return `${prod('me_cost')}${prod('finance_cost')}${prod('connect_cost')}<td></td>${prod('tactical_cost')}<td></td>
                <td class="at-cpspd" ${at} data-cell="cpspd">${atCpspd(t.slotDays ? t.slotCost / t.slotDays : null)}</td>
                <td class="num" ${at} data-cell="days" title="Days covered of the days in the ${level}">${t.days} of ${t.dim}</td>
                <td class="num" ${at} data-cell="cost"></td>
                <td class="num" ${at} data-cell="running">${atFmt(t.cost, 2)}</td><td></td>`;
        };
        // The total line under an open year's last month.
        const atYearTotal = (y, yt) => `<tr class="at-month-total at-year-bar">
                <td colspan="7">Total ${y}</td>${totalCells('year', y, yt)}
            </tr>`;
        let monthShown = null, yearShown = null;
        pieces.forEach(p => {
            const m = p.start.slice(0, 7);
            const y = m.slice(0, 4);
            const t = months.get(m);
            const yt = years.get(y);
            const yearFolded = atCollapsedYears.has(y);
            if (y !== yearShown) {
                // Year bar above the year's months, clicked to collapse or
                // expand. Collapsed, it carries the year's totals itself.
                yearShown = y;
                html.push(`<tr class="at-month-total at-month-bar at-year-bar">
                    <td colspan="7" class="at-month-toggle" onclick="toggleAtYear('${y}')" title="${yearFolded ? 'Expand' : 'Collapse'} this year">
                        <i class="fas fa-chevron-${yearFolded ? 'right' : 'down'}"></i> ${y}</td>
                    ${yearFolded ? totalCells('year', y, yt) : '<td colspan="11"></td>'}
                </tr>`);
            }
            if (yearFolded) return;
            const folded = atCollapsed.has(m);
            if (m !== monthShown) {
                // Month bar above the month's rows, clicked to collapse or
                // expand. Collapsed, it carries the month's totals itself.
                monthShown = m;
                html.push(`<tr class="at-month-total at-month-bar">
                    <td colspan="7" class="at-month-toggle at-month-indent" onclick="toggleAtMonth('${m}')" title="${folded ? 'Expand' : 'Collapse'} this month">
                        <i class="fas fa-chevron-${folded ? 'right' : 'down'}"></i> ${atMonthLabel(p.start)}</td>
                    ${folded ? totalCells('month', m, t) : '<td colspan="11"></td>'}
                </tr>`);
            }
            if (folded) {
                // The year's total line still goes under its last month.
                if (t.last === p.key && yt.last === m) html.push(atYearTotal(y, yt));
                return;
            } else if (p.first) {
                html.push(editRow(p.i, p));
            } else {
                // A row that runs on into this month: its days here, read only.
                html.push(`<tr class="at-continued">
                    <td>${atDateUk(p.start)}</td><td>${atDateUk(p.end)}</td>
                    <td colspan="5">Continued from the row starting ${atDateUk(atDraft[p.i].from)}</td>
                    ${['me_cost', 'finance_cost', 'connect_cost'].map(f => `<td class="num" data-key="${p.key}" data-cell="p_${f}">${atFmt(p.products[f], 2)}</td>`).join('')}
                    <td></td><td class="num" data-key="${p.key}" data-cell="p_tactical_cost">${atFmt(p.products.tactical_cost, 2)}</td><td></td>
                    <td class="at-cpspd" data-key="${p.key}" data-cell="cpspd">${atCpspd(p.perSlotDay)}</td>
                    <td class="num" data-key="${p.key}" data-cell="days">${p.days}</td>
                    <td class="num" data-key="${p.key}" data-cell="cost">${atFmt(p.cost, 2)}</td>
                    <td class="num" data-key="${p.key}" data-cell="running">${atFmt(p.running, 2)}</td><td></td>
                </tr>`);
            }
            if (t.last === p.key) {
                html.push(`<tr class="at-month-total">
                    <td colspan="7" class="at-month-indent">Total ${atMonthLabel(p.start)}</td>${totalCells('month', m, t)}
                </tr>`);
                if (yt.last === m) html.push(atYearTotal(y, yt));
            }
        });
        document.getElementById('atPeriodList').innerHTML = html.join('')
            || '<tr><td colspan="18" class="modal-hint">No rows yet. Apply the invoices, paste an adjustment email above, or add a row.</td></tr>';
        atDraft.forEach((p, i) => atMarkTactical(i));
    }

    // ── Invoices: imported straight away, not on Save ────────────────────────
    function atMessages(id, items) {
        document.getElementById(id).innerHTML = items
            .map(m => `<div class="at-${m.kind}">${escHtml(m.text)}</div>`).join('');
    }

    async function loadAtInvoices() {
        const list = document.getElementById('atInvoiceList');
        try {
            const resp = await fetch(BASE_URL + '/kpi/at-invoices').then(atReadJson);
            if (resp.status !== 'ok') throw new Error(resp.message || 'load failed');
            renderAtInvoices(resp);
        } catch (err) {
            list.innerHTML = `<tr><td colspan="9" class="modal-hint">Invoices could not be loaded: ${escHtml(err.message)}</td></tr>`;
        }
    }

    let atInvoicesShown = [];
    // Download CSV: the invoices list as shown, full figures (not rounded).
    function downloadAtInvoicesCsv() {
        const cell = v => { const s = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
        const lines = [['Invoice', 'Issued', 'Covers From', 'Covers To', 'Cost Excluding PPC', 'PPC', 'PPC Clicks', 'Cost Per PPC Click', 'Imported']]
            .concat(atInvoicesShown.map(inv => [inv.invoice_number, inv.issued, inv.date_from, inv.date_to, inv.lines_total, inv.ppc,
                inv.ppc_clicks, inv.ppc_clicks ? inv.ppc / inv.ppc_clicks : '', inv.imported_at.slice(0, 10)]));
        const blob = new Blob([lines.map(r => r.map(cell).join(',')).join('\r\n')], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'autotrader_invoices.csv';
        a.click();
        URL.revokeObjectURL(a.href);
    }

    function renderAtInvoices(resp) {
        atInvoicesShown = resp.invoices;
        document.getElementById('atInvoiceList').innerHTML = resp.invoices.map(inv => `<tr>
                <td>${escHtml(inv.invoice_number)}</td>
                <td>${atDateUk(inv.issued)}</td>
                <td>${atDateUk(inv.date_from)} - ${atDateUk(inv.date_to)}</td>
                <td class="num">${atFmt(inv.lines_total, 2)}</td>
                <td class="num">${atFmt(inv.ppc, 2)}</td>
                <td class="num">${inv.ppc_clicks === null ? (inv.ppc ? 'Import Again' : '-') : atFmt(inv.ppc_clicks)}</td>
                <td class="num">${inv.ppc_clicks ? atFmt(inv.ppc / inv.ppc_clicks, 4) : '-'}</td>
                <td>${atDateUk(inv.imported_at.slice(0, 10))}${inv.has_pdf ? '' : ' (PDF not saved)'}</td>
                <td><button type="button" class="btn btn-remove-chart" title="Delete Invoice"
                            onclick="deleteAtInvoice('${escHtml(inv.invoice_number)}')"><i class="fas fa-trash"></i></button></td>
            </tr>`).join('') || '<tr><td colspan="9" class="modal-hint">No invoices imported yet.</td></tr>';
        const c = resp.coverage;
        document.getElementById('atInvoiceCoverage').textContent = !c.from ? ''
            : `Invoiced ${atDateUk(c.from)} to ${atDateUk(c.to)}. `
              + (c.gaps.length ? 'Missing (left blank on the charts): '
                  + c.gaps.map(g => `${atDateUk(g[0])} - ${atDateUk(g[1])}`).join(', ') + '.'
                : 'No missing days.');
    }

    // The reply as JSON. When the server sends its HTML error page instead
    // (signed out, no access, or an error the endpoint did not catch), this
    // turns it into a readable message: the status, and the last line of the
    // page's Technical Details (the Python error) when there is one.
    async function atReadJson(r) {
        const text = await r.text();
        try { return JSON.parse(text); } catch (e) { /* not JSON: an HTML page */ }
        if (r.status === 401) return { status: 'error', message: 'Signed out. Log in again, then import.' };
        if (r.status === 403) return { status: 'error', message: 'No access to the KPI page.' };
        const page = new DOMParser().parseFromString(text, 'text/html');
        const detail = (page.querySelector('pre')?.textContent || '').trim().split('\n').pop()
            || page.querySelector('p')?.textContent || '';
        return { status: 'error', message: `Server error ${r.status}${detail ? ': ' + detail.trim() : ''}` };
    }

    function atFileBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
            reader.onerror = () => reject(new Error('could not read the file'));
            reader.readAsDataURL(file);
        });
    }

    // One PDF at a time, so each gets its own message and a refused one does
    // not stop the rest.
    async function importAtInvoices(input) {
        const files = [...input.files];
        input.value = '';
        if (!files.length) return;
        const messages = [];
        for (const [n, file] of files.entries()) {
            atMessages('atInvoiceMessages', [...messages, { kind: 'ok', text: `Importing ${n + 1} of ${files.length}: ${file.name}` }]);
            try {
                const resp = await fetch(BASE_URL + '/kpi/at-invoices/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, data: await atFileBase64(file) }),
                }).then(atReadJson);
                if (resp.status !== 'ok') {
                    messages.push({ kind: 'err', text: resp.message || `${file.name}: import failed` });
                    continue;
                }
                messages.push({ kind: 'ok', text: `${file.name}: invoice ${resp.invoice_number}, ${resp.lines} lines, `
                    + `£${atFmt(resp.lines_total, 2)} excluding PPC`
                    + (resp.ppc_clicks ? `; PPC £${atFmt(resp.ppc, 2)} for ${atFmt(resp.ppc_clicks)} clicks` : '')
                    + (resp.replaced ? ' (replaced the earlier import)' : '') });
                if (resp.s3_error) messages.push({ kind: 'warn', text: `${file.name}: imported, but the PDF was not saved to S3: ${resp.s3_error}` });
            } catch (err) {
                messages.push({ kind: 'err', text: `${file.name}: ${err.message}` });
            }
        }
        if (messages.some(m => m.kind === 'ok')) {
            messages.push({ kind: 'warn', text: 'The charts read the rows: press Apply Invoices To Rows, then Save.' });
        }
        atMessages('atInvoiceMessages', messages);
        loadAtInvoices();
    }

    async function deleteAtInvoice(number) {
        if (!confirm(`Delete invoice ${number}? Its rows stay in the list until you press Apply Invoices To Rows again.`)) return;
        try {
            const resp = await fetch(BASE_URL + '/kpi/at-invoices/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ invoice_number: number }),
            }).then(atReadJson);
            if (resp.status !== 'ok') throw new Error(resp.message || 'delete failed');
            atMessages('atInvoiceMessages', [{ kind: 'ok', text: `Invoice ${number} deleted.` }]);
            renderAtInvoices(resp);
        } catch (err) {
            atMessages('atInvoiceMessages', [{ kind: 'err', text: `Invoice ${number}: ${err.message}` }]);
        }
    }

    // ── Adjustment emails: read into a draft row, checked, then saved with Save ──
    // Products as Autotrader names them in the email. Anything else with a
    // quantity or price stops the read, so nothing is guessed.
    const AT_EMAIL_PRODUCTS = [
        { id: 'base', re: /^Contracted Adverts/i },
        { id: 'bonus', re: /^Bonus Advert Allowance/i },
        { id: 'me', re: /^Market Extension/i },
        { id: 'finance', re: /^Finance Calculator/i },
        { id: 'tactical', re: /^Tactical Adverts/i },
        { id: 'connect', re: /^(Retail Strategy|Connect)/i },
    ];
    const AT_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                       'september', 'october', 'november', 'december'];

    // {current: {product: {qty, money}}, changes: {...}, goLive: 'YYYY-MM-DD'|null, unknown: [names]}
    function parseAtEmail(text) {
        const out = { current: {}, changes: {}, goLive: null, unknown: [] };
        let section = null;
        for (const raw of text.split(/\r?\n/)) {
            const line = raw.trim();
            if (!line) continue;
            const live = line.match(/go live on\s+(?:[A-Za-z]+,?\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})/i);
            if (live) {
                const month = AT_MONTHS.indexOf(live[2].toLowerCase()) + 1;
                if (month) out.goLive = `${live[3]}-${String(month).padStart(2, '0')}-${live[1].padStart(2, '0')}`;
            }
            if (/current monthly spend is made up of/i.test(line)) { section = out.current; continue; }
            if (/changes we discussed/i.test(line)) { section = out.changes; continue; }
            if (!section || /^all other products/i.test(line)) continue;
            // Cells are tab-separated when pasted from Outlook; otherwise the
            // name runs up to the first number after a word.
            let cells = line.split('\t').map(c => c.trim()).filter(c => c !== '');
            if (cells.length === 1) {
                const m = line.match(/^(.*?[A-Za-z)])\s+(\d[\d,]*)(?:\s+(.*))?$/);
                if (!m) continue;
                cells = [m[1], m[2], ...(m[3] || '').split(/\s+/)];
            }
            const qtyCell = cells.slice(1).find(c => /^\d[\d,]*$/.test(c));
            const moneyCell = cells.slice(1).find(c => /^£[\d,]+(\.\d+)?$/.test(c));
            if (!qtyCell && !moneyCell) continue;     // a heading line
            const product = AT_EMAIL_PRODUCTS.find(p => p.re.test(cells[0]));
            if (!product) { out.unknown.push(cells[0]); continue; }
            section[product.id] = {
                qty: qtyCell ? Number(qtyCell.replace(/,/g, '')) : null,
                money: moneyCell ? Number(moneyCell.replace(/[£,]/g, '')) : null,
            };
        }
        return out;
    }

    // Row fields from one section of the email; fields the section does not
    // give are left out, so they can be filled from elsewhere.
    function atEmailFields(sec) {
        const row = {};
        if (sec.base && sec.base.qty !== null) row.base = sec.base.qty;
        if (sec.bonus && sec.bonus.qty !== null) {
            const base = row.base;
            row.flex_pct = base && Math.floor(base * 0.1 + 0.5) !== sec.bonus.qty
                ? Math.round(sec.bonus.qty / base * 10000) / 100 : 10;
        }
        if (sec.tactical) {
            if (sec.tactical.qty !== null) row.tactical = sec.tactical.qty;
            if (sec.tactical.money !== null) row.tactical_cost = sec.tactical.money;
        }
        if (sec.me && sec.me.money !== null) row.me_cost = sec.me.money;
        if (sec.finance && sec.finance.money !== null) row.finance_cost = sec.finance.money;
        if (sec.connect && sec.connect.money !== null) row.connect_cost = sec.connect.money;
        return row;
    }

    // A tactical change email ("We've now completed the below tactical advert
    // reduction"): a Tactical Adverts heading, then one line per dealer with
    // its Current and Future slots. Returns {current, future, sent} summed over
    // the dealer lines, or null when the text is not one of these.
    function parseAtTacticalEmail(text) {
        const lines = text.split(/\r?\n/).map(l => l.trim());
        const start = lines.findIndex(l => /^Tactical Adverts\b/i.test(l));
        if (start < 0 || !lines.some(l => /\bCurrent\b/i.test(l) && /\bFuture\b/i.test(l))) return null;
        let current = 0, future = 0, found = 0;
        for (const line of lines.slice(start + 1)) {
            if (!line || /\bCurrent\b.*\bFuture\b/i.test(line)) continue;
            const m = line.match(/^\d+\s*-\s*.+?\s+(\d[\d,]*)\s+(\d[\d,]*)$/);
            if (!m) { if (found) break; continue; }
            current += Number(m[1].replace(/,/g, ''));
            future += Number(m[2].replace(/,/g, ''));
            found++;
        }
        if (!found) return null;
        // "Sent: 19 October 2026 11:56" or "Sent: Monday, October 19, 2026 11:56 AM".
        let sent = null;
        const dm = text.match(/^\s*Sent:\s*(?:[A-Za-z]+,?\s+)?(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})/im);
        const md = text.match(/^\s*Sent:\s*(?:[A-Za-z]+,?\s+)?([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})/im);
        const [day, month, year] = dm ? [dm[1], dm[2], dm[3]] : md ? [md[2], md[1], md[3]] : [];
        const mi = month ? AT_MONTHS.indexOf(month.toLowerCase()) + 1 : 0;
        if (mi) sent = `${year}-${String(mi).padStart(2, '0')}-${day.padStart(2, '0')}`;
        return { current, future, sent };
    }

    // A copy of a row to start a new estimate row on `from`. An invoice row's
    // costs are for its own days, so they are turned into a month's cost.
    function atEstimateCopy(row, from) {
        const copy = { ...row };
        if (row.source === 'invoice' && row.to) {
            const scale = atDaysInMonth(from) / atDayCount(row.from, row.to);
            AT_PRODUCT_COSTS.forEach(f => { if (copy[f] !== null && copy[f] !== undefined) copy[f] = atPence(Number(copy[f]) * scale); });
        }
        return { ...copy, from, to: null, source: 'email' };
    }

    // Adds (or updates) the row for a tactical change email. A reduction
    // applies from the day it was sent; an increase from the day after, the
    // first full day billed at the new number.
    function readAtTacticalEmail(t) {
        if (!t.sent) {
            atMessages('atEmailMessages', [{ kind: 'err', text: 'Tactical change email: no Sent date found. Paste it with its From / Sent / To header lines.' }]);
            return false;
        }
        const from = t.future < t.current ? t.sent : atAddDays(t.sent, 1);
        const before = atDraft.filter(p => p.from && p.from <= from)
            .sort((a, b) => a.from.localeCompare(b.from)).pop();
        if (!before) {
            atMessages('atEmailMessages', [{ kind: 'err', text: `Tactical change email: no row in force on ${atDateUk(from)} to take the other figures from. Add that row first.` }]);
            return false;
        }
        const messages = [];
        if (Number(before.tactical) !== t.current) {
            messages.push({ kind: 'warn', text: `The email says ${t.current} tactical slots before the change; the row from ${atDateUk(before.from)} has ${before.tactical}.` });
        }
        let row;
        if (before.from === from && before.source !== 'invoice') {
            row = before;                                // a row already starts that day: change it
        } else {
            row = atEstimateCopy(before, from);
            // The row before ran on past this day with its own To: the new row
            // takes the rest of those days, and the row before ends the day before.
            if (before.to && before.to >= from) { row.to = before.to; before.to = atAddDays(from, -1); }
            atDraft.push(row);
        }
        row.tactical = t.future;
        if (Number(row.base) > 0 && row.me_cost !== null && row.me_cost !== undefined) {
            row.tactical_cost = atPence(t.future * Number(row.me_cost) / Number(row.base));
        }
        if (atTacticalOver(row)) {
            messages.push({ kind: 'err', text: `${t.future} tactical slots is more than ${AT_TACTICAL_MAX_PCT}% of ${row.base} base slots: Save will refuse it.` });
        }
        messages.unshift({ kind: 'ok', text: `Tactical ${t.current} to ${t.future} from ${atDateUk(from)} `
            + `(${t.future < t.current ? 'a reduction applies on the day it is made' : 'an increase is billed from the next full day'}): `
            + `Tactical £${atFmt(row.tactical_cost, 2)} a month. Check it, then Save.` });
        atMessages('atEmailMessages', messages);
        return true;
    }

    function readAtEmail() {
        const box = document.getElementById('atEmailPaste');
        const tactical = parseAtTacticalEmail(box.value);
        if (tactical) {
            if (readAtTacticalEmail(tactical)) { box.value = ''; renderAtDraft(); }
            return;
        }
        const email = parseAtEmail(box.value);
        const messages = [];
        if (email.unknown.length) {
            atMessages('atEmailMessages', [{ kind: 'err', text: 'Products not recognised, so nothing was added: '
                + email.unknown.join(', ') }]);
            return;
        }
        if (!email.goLive) {
            atMessages('atEmailMessages', [{ kind: 'err', text: 'No go-live date found (e.g. "go live on Thursday, 5th November 2026").' }]);
            return;
        }
        const changes = atEmailFields(email.changes);
        if (!Object.keys(changes).length) {
            atMessages('atEmailMessages', [{ kind: 'err', text: 'No changed products found under "The changes we discussed".' }]);
            return;
        }
        const current = atEmailFields(email.current);
        // The row in force the day before go-live: what the email's current
        // figures are checked against.
        const before = atDraft.filter(p => p.from && p.from < email.goLive)
            .sort((a, b) => a.from.localeCompare(b.from)).pop();
        const currentRow = from => ({ from, ...Object.fromEntries(AT_FIELDS.map(f => [f, current[f] ?? null])),
                                      to: null, source: 'email' });
        if (before) {
            const diffs = AT_FIELDS.filter(f => current[f] !== undefined && Number(before[f]) !== current[f])
                .map(f => `${AT_FIELD_LABELS[f]} ${atFmt(current[f], 2)} in the email, ${atFmt(before[f], 2)} in the row from ${atDateUk(before.from)}`);
            if (diffs.length) messages.push({ kind: 'warn', text: 'The email\'s current figures differ from the row before: ' + diffs.join('; ') });
            // The row before ends (e.g. the last invoice row) with days still to
            // go before go-live: the email's current figures cover those days.
            if (before.to && before.to < atAddDays(email.goLive, -1) && Object.keys(current).length) {
                atDraft.push(currentRow(atAddDays(before.to, 1)));
                messages.push({ kind: 'warn', text: `Added a row for the email's current figures from ${atDateUk(atAddDays(before.to, 1))}, `
                    + 'the day after the row before ends. Check its From date.' });
            }
        } else if (Object.keys(current).length) {
            // No row before: add the current figures, with the From date left
            // for you to type in. Save will not go through until it is.
            atDraft.push(currentRow(''));
            messages.push({ kind: 'warn', text: 'Added a row for the current figures. Type the date they started in its From box (left blank).' });
        }
        // Products the email does not list stay as they were: from the email's
        // current figures, then the row before.
        const row = { from: email.goLive, to: null, source: 'email' };
        AT_FIELDS.forEach(f => { row[f] = changes[f] ?? current[f] ?? (before ? before[f] : null); });
        const missing = AT_FIELDS.filter(f => row[f] === null || row[f] === undefined);
        const existing = atDraft.findIndex(p => p.from === email.goLive);
        if (existing >= 0) atDraft.splice(existing, 1, row); else atDraft.push(row);
        messages.unshift({ kind: 'ok', text: `${existing >= 0 ? 'Replaced' : 'Added'} the row from ${atDateUk(email.goLive)}: `
            + `£${atFmt(atWorked(row).monthTotal, 2)} a month. Check it, then Save.` });
        if (missing.length) messages.push({ kind: 'warn', text: 'Not in the email, so fill in: ' + missing.map(f => AT_FIELD_LABELS[f]).join(', ') });
        atMessages('atEmailMessages', messages);
        box.value = '';
        renderAtDraft();
    }

    // Reads two pasted spreadsheet columns, a date (dd/mm/yyyy) and a tactical
    // number, e.g. "04/10/2026<tab>95". Each number carries on until the next
    // date. Returns [{ date: 'YYYY-MM-DD', value }] in date order, plus the
    // lines that could not be read.
    function parseAtTacticalTable(text) {
        const entries = [], bad = [];
        text.split(/\r?\n/).map(l => l.trim()).filter(Boolean).forEach(line => {
            const m = line.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s*[\t ,;]\s*(\d[\d,]*(?:\.\d+)?)$/)
                || line.match(/^(\d{4})-(\d{2})-(\d{2})\s*[\t ,;]\s*(\d[\d,]*(?:\.\d+)?)$/);
            if (!m) { if (!/[a-z]/i.test(line)) bad.push(line); return; }
            const iso = m[1].length === 4 ? `${m[1]}-${m[2]}-${m[3]}`
                : `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
            entries.push({ date: iso, value: Math.round(Number(m[4].replace(/,/g, ''))) });
        });
        entries.sort((a, b) => a.date.localeCompare(b.date));
        return { entries, bad };
    }

    function readAtTacticalTable() {
        const box = document.getElementById('atTacticalPaste');
        const { entries, bad } = parseAtTacticalTable(box.value);
        if (!entries.length) {
            atMessages('atTacticalMessages', [{ kind: 'err', text: 'No dates and numbers found. Paste two columns: a date (dd/mm/yyyy) and the tactical number.' }]);
            return;
        }
        const first = entries[0].date, last = entries[entries.length - 1].date;
        const valueAt = d => entries.filter(e => e.date <= d).pop().value;
        // Dates to apply: each date the pasted number changes, plus the start
        // of every row already held inside the pasted range (so it follows the paste).
        const dates = new Set(entries.filter((e, i) => i === 0 || e.value !== entries[i - 1].value).map(e => e.date));
        atDraft.forEach(p => { if (p.from && p.from >= first && p.from <= last) dates.add(p.from); });
        const count = { added: 0, updated: 0, same: 0, invoice: 0, none: 0 };
        const touched = [];
        [...dates].sort().forEach(d => {
            const v = valueAt(d);
            const own = atDraft.find(p => p.from === d);
            const inForce = own || atDraft.filter(p => p.from && p.from < d)
                .sort((a, b) => a.from.localeCompare(b.from)).pop();
            if (!inForce) { count.none++; return; }
            if (inForce.source === 'invoice' && (own || !inForce.to || inForce.to >= d)) { count.invoice++; return; }
            if (Number(inForce.tactical) === v) { count.same++; return; }
            const row = own || atEstimateCopy(inForce, d);
            row.tactical = v;
            if (Number(row.base) > 0 && row.me_cost !== null && row.me_cost !== undefined) {
                row.tactical_cost = atPence(v * Number(row.me_cost) / Number(row.base));
            }
            if (own) count.updated++; else { atDraft.push(row); count.added++; }
            touched.push(row);
        });
        const messages = [{ kind: 'ok', text: `Tactical numbers from ${atDateUk(first)} to ${atDateUk(last)}: `
            + `${count.added} rows added, ${count.updated} updated, ${count.same} already correct. Check them in step 5, then Save.` }];
        if (count.invoice) messages.push({ kind: 'warn', text: `${count.invoice} dates fall on invoice rows, which were left as invoiced.` });
        if (count.none) messages.push({ kind: 'warn', text: `${count.none} dates have no earlier row to copy the other figures from, so they were skipped.` });
        if (bad.length) messages.push({ kind: 'warn', text: 'Lines not read: ' + bad.slice(0, 5).join('; ') });
        const over = touched.filter(atTacticalOver).length;
        if (over) messages.push({ kind: 'warn', text: `${over} rows have more than ${AT_TACTICAL_MAX_PCT}% of the base slots as tactical: Save will refuse them.` });
        atMessages('atTacticalMessages', messages);
        box.value = '';
        renderAtDraft();
    }

    function renderAtPpcDraft() {
        const order = atPpcDraft.map((p, i) => i).sort((a, b) => String(atPpcDraft[a].from).localeCompare(String(atPpcDraft[b].from)));
        document.getElementById('atPpcList').innerHTML = order.map(i => {
            const v = atPpcDraft[i].ppc_view_cost;
            return `<tr>
                <td><input type="date" value="${escHtml(atPpcDraft[i].from || '')}" oninput="setAtPpcField(${i}, 'from', this.value)"></td>
                <td><input type="number" min="0" step="any" value="${v === null || v === undefined ? '' : v}"
                           oninput="setAtPpcField(${i}, 'ppc_view_cost', this.value)"></td>
                <td><button type="button" class="btn btn-remove-chart" title="Delete Row" onclick="removeAtPpc(${i})">
                    <i class="fas fa-trash"></i></button></td>
            </tr>`;
        }).join('') || '<tr><td colspan="3" class="modal-hint">No rows yet.</td></tr>';
    }

    // Typing only updates the draft: re-drawing the table would replace the
    // box being typed in (a date box reports each valid date while it is
    // still being typed) and re-sort the rows under the cursor. Rows are put
    // in date order when the popup opens or a row is added or removed.
    function setAtPpcField(i, field, value) {
        atPpcDraft[i][field] = field === 'from' ? value : (value === '' ? null : Number(value));
    }

    function addAtPpc() {
        const last = atPpcDraft.length ? atPpcDraft[atPpcDraft.length - 1].ppc_view_cost : null;
        atPpcDraft.push({ from: new Date().toISOString().slice(0, 10), ppc_view_cost: last });
        renderAtPpcDraft();
    }

    function removeAtPpc(i) {
        atPpcDraft.splice(i, 1);
        renderAtPpcDraft();
    }

    // As setAtPpcField: the draft is updated and only the worked-out cells
    // (Flex Slots, Total Slots, Total Per Month) are refreshed in place.
    function setAtField(i, field, value) {
        const p = atDraft[i];
        p[field] = field === 'from' ? value : field === 'to' ? (value || null)
            : (value === '' ? null : AT_PRODUCT_COSTS.includes(field) ? atPence(value) : Number(value));
        // Estimate rows: a tactical slot costs the same a month as a contracted
        // slot (Market Extension / base slots; Autotrader's emails match this
        // to the penny), so Tactical (£) follows the slots and the package
        // price. Typing in Tactical (£) itself still overrides it.
        if (p.source !== 'invoice' && ['base', 'tactical', 'me_cost'].includes(field)
                && Number(p.base) > 0 && p.me_cost !== null && p.me_cost !== undefined) {
            p.tactical_cost = atPence((Number(p.tactical) || 0) * Number(p.me_cost) / Number(p.base));
            const box = document.querySelector(`#atPeriodList tr[data-at="${i}"] input[data-field="tactical_cost"]`);
            if (box) box.value = p.tactical_cost.toFixed(2);
        }
        if (field === 'base' || field === 'tactical') atMarkTactical(i);
        updateAtWorked();
    }

    // Autotrader allows tactical slots up to 10% of the base slots (kpi.py
    // _AT_TACTICAL_MAX_PCT; Save refuses an estimate row over it).
    const AT_TACTICAL_MAX_PCT = 10;
    const atTacticalOver = p => p.source !== 'invoice'
        && (Number(p.tactical) || 0) > (Number(p.base) || 0) * AT_TACTICAL_MAX_PCT / 100;
    function atMarkTactical(i) {
        const box = document.querySelector(`#atPeriodList tr[data-at="${i}"] input[data-field="tactical"]`);
        if (!box) return;
        const over = atTacticalOver(atDraft[i]);
        box.style.borderColor = over ? '#c0392b' : '';
        box.title = over ? `More than ${AT_TACTICAL_MAX_PCT}% of the base slots: Save will refuse this row` : '';
    }

    // Figures typed in only change the worked-out cells, so those are updated
    // in place (the boxes are not re-drawn). Dates change which rows and
    // month totals there are, so those re-draw the table instead (renderAtDraft).
    function updateAtWorked() {
        const list = document.getElementById('atPeriodList');
        list.querySelectorAll('tr[data-at]').forEach(tr => {
            const w = atWorked(atDraft[Number(tr.dataset.at)]);
            tr.querySelector('[data-cell="slots"]').textContent = atFmt(w.totalSlots);
            tr.querySelector('[data-cell="bfslot"]').textContent = atCpspd(w.baseFlexSlotDay);
            tr.querySelector('[data-cell="tacslot"]').textContent = atCpspd(w.tacticalSlotDay);
        });
        const { pieces, months, years } = atFigures();
        const set = (sel, cell, text) => list.querySelectorAll(`[${sel}][data-cell="${cell}"]`).forEach(el => { el.textContent = text; });
        pieces.forEach(p => {
            set(`data-key="${p.key}"`, 'cpspd', atCpspd(p.perSlotDay));
            set(`data-key="${p.key}"`, 'cost', atFmt(p.cost, 2));
            set(`data-key="${p.key}"`, 'running', atFmt(p.running, 2));
            AT_PRODUCT_COSTS.forEach(f => set(`data-key="${p.key}"`, 'p_' + f, atFmt(p.products[f], 2)));
        });
        months.forEach((t, m) => {
            AT_PRODUCT_COSTS.forEach(f => set(`data-month="${m}"`, f, atFmt(t.products[f], 2)));
            set(`data-month="${m}"`, 'cpspd', atCpspd(t.slotDays ? t.slotCost / t.slotDays : null));
            set(`data-month="${m}"`, 'running', atFmt(t.cost, 2));
        });
        years.forEach((t, y) => {
            AT_PRODUCT_COSTS.forEach(f => set(`data-year="${y}"`, f, atFmt(t.products[f], 2)));
            set(`data-year="${y}"`, 'cpspd', atCpspd(t.slotDays ? t.slotCost / t.slotDays : null));
            set(`data-year="${y}"`, 'running', atFmt(t.cost, 2));
        });
    }

    function addAtPeriod() {
        const latest = [...atDraft].sort((a, b) => String(a.from).localeCompare(String(b.from))).pop();
        const copy = latest ? { ...latest } : { base: null, flex_pct: 10, tactical: 0, me_cost: null,
            finance_cost: null, tactical_cost: 0, connect_cost: 0 };
        // Starts tomorrow: a slot increase takes effect straight away, and the
        // first full day is the first one billed at the new number.
        const from = atAddDays(new Date().toLocaleDateString('en-CA'), 1);
        atDraft.push(atEstimateCopy(copy, from));
        renderAtDraft();
    }

    function removeAtPeriod(i) {
        atDraft.splice(i, 1);
        renderAtDraft();
    }

    // Saves the rows if they changed. Returns true when saved, so the charts reload.
    // The rows as the server takes them, checked. Throws with a message for the page.
    function atRowsPayload() {
        const payload = atDraft.map(p => {
            // Same key order as the server (kpi.py _AT_PERIOD_FIELDS), so an
            // unchanged list compares equal and is not saved again.
            const row = { from: p.from };
            AT_FIELDS.forEach(f => { row[f] = p[f] === undefined || p[f] === '' ? null : p[f]; });
            row.to = p.to || null;
            row.source = p.source === 'invoice' ? 'invoice' : 'email';
            return row;
        });
        const bad = payload.find(p => !p.from || AT_FIELDS.some(f => p[f] === null));
        if (bad) throw new Error('Autotrader: every row needs a From date and every figure (0 if none).');
        const backwards = payload.find(p => p.to && p.to < p.from);
        if (backwards) throw new Error(`Autotrader: the row from ${atDateUk(backwards.from)} has a To date before its From date.`);
        const noTo = payload.find(p => p.source === 'invoice' && !p.to);
        if (noTo) throw new Error(`Autotrader: the invoice row from ${atDateUk(noTo.from)} needs a To date (its costs are for From to To).`);
        return payload;
    }

    // Saves the rows and PPC rows; on success they become the saved lists.
    async function atPostPeriods(payload, ppcPayload) {
        const resp = await fetch(BASE_URL + '/kpi/at-periods', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ periods: payload, ppc_periods: ppcPayload }),
        }).then(atReadJson);
        if (resp.status !== 'ok') throw new Error('Autotrader: ' + (resp.message || 'save failed'));
        atPeriods = resp.periods;
        atPpcPeriods = resp.ppc_periods;
        atDraft = atPeriods.map(p => ({ ...p }));
    }

    // Save button: saves the rows and PPC rows if they changed. Returns true
    // when saved (or when Apply Invoices already saved), so the charts reload.
    async function saveAtPeriods() {
        const payload = atRowsPayload();
        const ppcPayload = atPpcDraft.map(p => ({ from: p.from, ppc_view_cost: p.ppc_view_cost ?? null }));
        if (ppcPayload.some(p => !p.from || p.ppc_view_cost === null)) {
            throw new Error('Autotrader: every PPC cost row needs a From date and a cost.');
        }
        const savedByApply = atRowsSaved;
        atRowsSaved = false;
        const sortKey = list => JSON.stringify([...list].sort((a, b) => String(a.from).localeCompare(String(b.from))));
        if (sortKey(payload) === sortKey(atPeriods) && sortKey(ppcPayload) === sortKey(atPpcPeriods)) return savedByApply;
        await atPostPeriods(payload, ppcPayload);
        atPpcDraft = atPpcPeriods.map(p => ({ ...p }));
        return true;
    }

    function openSettingsModal() {
        Object.entries(SETTING_INPUTS).forEach(([name, id]) => {
            const input = document.getElementById(id);
            input.value = settings[name];
            const limits = settingLimits[name];
            if (limits) { input.min = limits[0]; input.max = limits[1]; }
        });
        const lim = name => settingLimits[name] ? `${settingLimits[name][0]}-${settingLimits[name][1]}` : '';
        document.getElementById('settingsHint').textContent =
            `Months ${lim('months')}, weeks ${lim('weeks')}, days available ${lim('days_total')}. `
            + 'Months and weeks shown include the current one. The "after" boxes add that many periods after the current one, '
            + `with no figures yet but their last year and target lines (months ${lim('months_after')}, weeks ${lim('weeks_after')}, `
            + `days ${lim('days_after')}). Days available includes the days after today.`;
        document.getElementById('appearanceHint').textContent =
            `Section heading ${lim('section_font_px')} px (e.g. "Upper Funnel"), chart title ${lim('title_font_px')} px, `
            + `chart subtitle ${lim('subtitle_font_px')} px (the optional line under a chart title), `
            + `chart period label ${lim('period_font_px')} px (monthly / weekly / daily at the right of the title), `
            + `top bar ${lim('toolbar_font_px')} px (every button and label in the bar at the top), `
            + `Autotrader figures ${lim('at_font_px')} px (Advert Allowance, Published, Capped, Unpublished), `
            + `Autotrader rows text ${lim('at_table_font_px')} px (Settings > Autotrader Costs), `
            + `chart legend ${lim('legend_font_px')} px (the series names above stacked charts), `
            + 'and the size the magnifier opens a chart popup at. '
            + 'Applies to the KPI page for everyone.';
        sectionDraft = SECTIONS.map(sectionDraftItem);
        loadChartDraft();
        renderSectionDraft();
        loadVehColumnDraft();
        loadThemeDraft();
        loadNamesDraft();
        loadDerivedDraft();
        loadAtDraft();
        restoreSettingsGroups();
        document.getElementById('settingsError').classList.remove('show');
        document.getElementById('settingsModal').classList.add('open');
    }

    // ── Colour themes (Settings > Appearance) ────────────────────────────────
    // Edited as a copy; Save sends the whole list. A chart whose theme is
    // deleted falls back to the first theme.
    let themeDraft = [];

    function loadThemeDraft() {
        themeDraft = JSON.parse(JSON.stringify(themes));
        renderThemeDraft();
    }

    function renderThemeDraft() {
        const uses = id => layout.rows.filter(r => isStacked(r) && (r.theme || (themes[0] && themes[0].id)) === id).length;
        document.getElementById('themeList').innerHTML = themeDraft.map((t, i) => {
            const swatches = t.colours.map((c, j) => `
                <span class="theme-swatch">
                    <input type="color" value="${escHtml(c)}" title="Colour ${j + 1}" onchange="themeDraft[${i}].colours[${j}] = this.value">
                    <button type="button" title="Remove this colour" onclick="removeThemeColour(${i}, ${j})">&times;</button>
                </span>`).join('');
            const n = uses(t.id);
            return `
                <div class="theme-row">
                    <input type="text" maxlength="40" value="${escHtml(t.name)}" oninput="themeDraft[${i}].name = this.value">
                    <div class="theme-swatches">
                        ${swatches}
                        <button type="button" class="btn btn-edit" title="Add a colour" onclick="addThemeColour(${i})"><i class="fas fa-plus"></i></button>
                    </div>
                    <label class="theme-transparency" title="How see-through the colours are: 0 is solid">
                        Transparency
                        <input type="number" min="0" max="90" step="5" value="${t.transparency || 0}"
                               oninput="themeDraft[${i}].transparency = this.value"> %
                    </label>
                    <span class="theme-uses">${n} chart${n === 1 ? '' : 's'}</span>
                    <button type="button" class="btn btn-remove-chart" title="Delete theme" onclick="removeThemeDraft(${i})"
                            ${themeDraft.length === 1 ? 'disabled' : ''}><i class="fas fa-trash"></i></button>
                </div>`;
        }).join('');
    }

    function addThemeDraft() {
        themeDraft.push({ id: 't' + Date.now().toString(36), name: 'New theme', colours: ['#2a78d6', '#eb6834', '#1baf7a'] });
        renderThemeDraft();
    }

    function removeThemeDraft(i) {
        const t = themeDraft[i];
        const n = layout.rows.filter(r => isStacked(r) && r.theme === t.id).length;
        if (n && !confirm(`${n} chart${n === 1 ? ' uses' : 's use'} "${t.name}". They will use the first theme instead. Delete it?`)) return;
        themeDraft.splice(i, 1);
        renderThemeDraft();
    }

    function addThemeColour(i) {
        if (themeDraft[i].colours.length >= 20) return;
        themeDraft[i].colours.push('#9aa0a6');
        renderThemeDraft();
    }

    function removeThemeColour(i, j) {
        if (themeDraft[i].colours.length <= 1) return;
        themeDraft[i].colours.splice(j, 1);
        renderThemeDraft();
    }

    async function saveThemeDraft() {
        const transparency = t => {
            const n = parseInt(t.transparency, 10);
            return isNaN(n) ? 0 : Math.min(90, Math.max(0, n));
        };
        const payload = themeDraft.map(t => ({ id: t.id, name: String(t.name || '').trim(), colours: t.colours, transparency: transparency(t) }));
        if (JSON.stringify(payload) === JSON.stringify(themes.map(t => ({ id: t.id, name: t.name, colours: t.colours, transparency: transparency(t) })))) return;
        const resp = await fetch(BASE_URL + '/kpi/themes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ themes: payload }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Colour themes save failed');
        themes = resp.themes;
        themeDraft = JSON.parse(JSON.stringify(themes));
        // Stacked charts pick up the new colours without new data.
        redrawStackedFromCache();
    }

    function redrawStackedFromCache(skipId = null) {
        shownRows().filter(r => isStacked(r) && r.id !== skipId)
            .forEach(r => { if (customRowData[r.id]) renderCustomRow(r, customRowData[r.id]); });
    }

    // ── Column names (Settings > Column Names) ──────────────────────────────
    // Saves the whole shared list if it differs from the one loaded; true
    // when it was saved.
    async function saveMetricNames(names) {
        if (JSON.stringify(names) === JSON.stringify(metricNames)) return false;
        const resp = await fetch(BASE_URL + '/kpi/metric-names', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ names }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Column names save failed');
        metricNames = resp.names;
        return true;
    }

    // Edited as a list of { key ('source|column'), name }; Save sends it all.
    let namesDraft = [];

    // Format of each column's figure in the point popup's breakdown, edited
    // on the same rows as the names. Saved separately (kpi.py metric-formats).
    const METRIC_FORMAT_OPTIONS = [['', 'Plain Number'], ['num', 'Number'], ['pct', '%'], ['pct100', '% (From Decimal)'],
                                   ['k', 'Thousands (k)'], ['km', 'k / M'], ['gbp', '£'], ['gbpkm', '£ k / M']];

    async function saveMetricFormats(formats) {
        if (JSON.stringify(formats) === JSON.stringify(metricFormats)) return false;
        const resp = await fetch(BASE_URL + '/kpi/metric-formats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ formats }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Column formats save failed');
        metricFormats = resp.formats;
        return true;
    }

    function loadNamesDraft() {
        const keys = [...new Set([...Object.keys(metricNames), ...Object.keys(metricFormats)])];
        namesDraft = keys
            .map(key => ({ key, name: metricNames[key] || '', fmt: (metricFormats[key] || {}).fmt || '',
                           decimals: (metricFormats[key] || {}).decimals || 0 }))
            .sort((a, b) => a.key.split('|')[1].localeCompare(b.key.split('|')[1]));
        renderNamesDraft();
    }

    function renderNamesDraft() {
        const options = key => {
            const missing = key && !metricsByKey[key]
                ? `<option value="${escHtml(key)}">${escHtml(key.slice(key.indexOf('|') + 1))}</option>` : '';
            const group = source => {
                const items = metricsCatalogue.filter(m => m.source === source);
                if (!items.length) return '';
                return `<optgroup label="${escHtml(sourceLabel(source))}">`
                    + items.map(m => `<option value="${escHtml(metricKey(m.source, m.column))}">${escHtml(m.column)}</option>`).join('')
                    + '</optgroup>';
            };
            return '<option value="">(choose a column)</option>' + missing + allMetricGroups(group);
        };
        const list = document.getElementById('namesList');
        list.innerHTML = namesDraft.map((n, i) => `
            <div class="series-row">
                <select onchange="namesDraft[${i}].key = this.value">${options(n.key)}</select>
                <input type="text" maxlength="60" value="${escHtml(n.name)}" placeholder="Name shown on charts"
                       oninput="namesDraft[${i}].name = this.value">
                <select title="How this column's figure shows in the point popup of a calculated chart"
                        onchange="namesDraft[${i}].fmt = this.value">
                    ${METRIC_FORMAT_OPTIONS.map(([v, l]) => `<option value="${v}"${v === (n.fmt || '') ? ' selected' : ''}>${escHtml(l)}</option>`).join('')}
                </select>
                <input type="number" min="0" max="3" step="1" style="width:60px;" value="${n.decimals || 0}"
                       title="Decimals in the point popup" oninput="namesDraft[${i}].decimals = parseInt(this.value, 10) || 0">
                <button type="button" class="btn btn-remove-chart" title="Remove name" onclick="namesDraft.splice(${i}, 1); renderNamesDraft()">
                    <i class="fas fa-trash"></i></button>
            </div>`).join('') || '<div class="point-status" style="padding:2px 0 6px;">No column names yet.</div>';
        // Each row has two selects: the column (first) and the format (second, already set above).
        list.querySelectorAll('.series-row').forEach((r, i) => { r.querySelector('select').value = namesDraft[i].key; });
    }

    // ── Derived column names (Settings > Column Names) ──
    async function saveDerivedNames(names) {
        if (JSON.stringify(names) === JSON.stringify(derivedNames)) return false;
        const resp = await fetch(BASE_URL + '/kpi/derived-names', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ names }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Derived column names save failed');
        derivedNames = resp.names;
        return true;
    }

    // Edited as [{ base, mult, name }] with base / mult as 'source|column'.
    let derivedDraft = [];

    function loadDerivedDraft() {
        derivedDraft = Object.entries(derivedNames).map(([key, name]) => {
            const [base, mult] = key.split('*');
            return { base, mult, name };
        }).sort((a, b) => a.name.localeCompare(b.name));
        renderDerivedDraft();
    }

    function renderDerivedDraft() {
        const options = (key, blank) => {
            const missing = key && !metricsByKey[key]
                ? `<option value="${escHtml(key)}">${escHtml(key.slice(key.indexOf('|') + 1))}</option>` : '';
            const group = source => {
                const items = metricsCatalogue.filter(m => m.source === source);
                if (!items.length) return '';
                return `<optgroup label="${escHtml(sourceLabel(source))}">`
                    + items.map(m => {
                        const k = metricKey(m.source, m.column);
                        return `<option value="${escHtml(k)}"${k === key ? ' selected' : ''}>${escHtml(metricOptionText(m.source, m.column))}</option>`;
                    }).join('') + '</optgroup>';
            };
            return `<option value="">${blank}</option>` + missing + allMetricGroups(group);
        };
        document.getElementById('derivedList').innerHTML = derivedDraft.map((n, i) => `
            <div class="series-row">
                <select title="The metric" onchange="derivedDraft[${i}].base = this.value">${options(n.base, '(choose a column)')}</select>
                <select title="Multiplied By" class="series-multiplied" onchange="derivedDraft[${i}].mult = this.value">${options(n.mult, '(multiplied by)')}</select>
                <input type="text" maxlength="60" value="${escHtml(n.name)}" placeholder="Name of the result, e.g. PPC Advert View Cost"
                       oninput="derivedDraft[${i}].name = this.value">
                <button type="button" class="btn btn-remove-chart" title="Remove name" onclick="derivedDraft.splice(${i}, 1); renderDerivedDraft()">
                    <i class="fas fa-trash"></i></button>
            </div>`).join('') || '<div class="point-status" style="padding:2px 0 6px;">No derived names yet.</div>';
    }

    function addDerivedDraft() {
        derivedDraft.push({ base: '', mult: '', name: '' });
        renderDerivedDraft();
    }

    // The draft as the object to save. Throws on a half-filled or repeated row.
    function derivedDraftPayload() {
        const out = {};
        derivedDraft.forEach(n => {
            const name = String(n.name || '').trim();
            if (!n.base && !n.mult && !name) return;
            if (!n.base || !n.mult || !name) throw new Error('Each derived name needs a column, a Multiplied By column and a name, or remove it.');
            const key = `${n.base}*${n.mult}`;
            if (key in out) throw new Error(`${name}: that pair of columns is listed twice.`);
            out[key] = name;
        });
        return out;
    }

    function addNameDraft() {
        namesDraft.push({ key: '', name: '', fmt: '', decimals: 0 });
        renderNamesDraft();
    }

    // The draft as the object to save. Throws on a half-filled or repeated row.
    function namesDraftPayload() {
        const names = {}, formats = {};
        namesDraft.forEach(n => {
            const name = String(n.name || '').trim();
            if (!n.key && !name && !n.fmt) return;   // an empty row added and left
            if (!n.key) throw new Error(`Choose a column for "${name || 'this format'}", or remove it.`);
            const column = n.key.slice(n.key.indexOf('|') + 1);
            if (!name && !n.fmt) throw new Error(`Enter a name or a format for ${column}, or remove it.`);
            if (n.key in names || n.key in formats) throw new Error(`${column} is listed twice.`);
            if (name) names[n.key] = name;
            if (n.fmt) formats[n.key] = { fmt: n.fmt, decimals: Math.min(3, Math.max(0, n.decimals || 0)) };
        });
        return { names, formats };
    }

    // ── Data sources (Settings > Data Sources) ───────────────────────────────
    // Snowflake tables whose metrics can be charted (kpi.py _data_sources).
    // Each add, change or removal saves straight away, not with Save, and the
    // metric lists are read again so the new metrics show in Edit Chart.
    const DS_DATE_TYPES = ['DATE', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ', 'TIMESTAMP'];
    const DS_NUMBER_TYPES = ['NUMBER', 'FLOAT', 'DECIMAL', 'NUMERIC', 'INT', 'INTEGER', 'BIGINT', 'DOUBLE', 'REAL'];
    // tables: null until first read (only when the form opens, as listing
    // every table takes a few seconds), then kept for the visit.
    const dsState = { builtIn: [], sources: [], uses: {}, tables: null, columns: [], editId: null, busy: 0 };
    const DS_SPINNER = '<i class="fas fa-spinner fa-spin"></i> ';

    async function loadDataSourcesTab() {
        const list = document.getElementById('dataSourceList');
        list.innerHTML = `<div class="point-status" style="padding:2px 0 6px;">${DS_SPINNER}Loading data sources...</div>`;
        document.getElementById('dataSourceBuiltIn').innerHTML = '';
        try {
            const resp = await fetch(BASE_URL + '/kpi/data-sources').then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'The data sources could not be read');
            dsState.builtIn = resp.built_in || [];
            dsState.sources = resp.sources || [];
            dsState.uses = resp.uses || {};
        } catch (err) {
            list.innerHTML = `<div class="point-status" style="padding:2px 0 6px;">${escHtml(err.message)}</div>`;
            return;
        }
        renderDataSourceList();
    }

    // What the form is waiting for, shown above it with a spinner. While
    // anything is loading the boxes and the Add button are disabled, so the
    // form never looks ready when it is not.
    function setDataSourceBusy(message) {
        dsState.busy += message ? 1 : -1;
        const status = document.getElementById('dataSourceStatus');
        if (message) status.innerHTML = DS_SPINNER + escHtml(message);
        else if (dsState.busy <= 0) { dsState.busy = 0; status.innerHTML = ''; }
        const busy = dsState.busy > 0;
        document.querySelectorAll('#dataSourceForm select, #dataSourceForm input').forEach(el => { el.disabled = busy; });
        document.getElementById('dataSourceSaveBtn').disabled = busy;
    }

    async function loadDataSourceTables() {
        if (dsState.tables) return;
        fillDsSelect('dsTable', [], '', 'Loading tables...');
        setDataSourceBusy('Loading the table list from Snowflake...');
        try {
            const resp = await fetch(BASE_URL + '/kpi/data-sources/tables').then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'The table list could not be read');
            dsState.tables = resp.tables || [];
        } catch (err) {
            showDataSourceError(err.message);
        } finally {
            setDataSourceBusy('');
        }
    }

    function openDataSourceForm() {
        document.getElementById('dataSourceForm').style.display = '';
        document.getElementById('dataSourceOpenBtn').style.display = 'none';
        resetDataSourceForm();
        document.getElementById('dsName').focus();
    }

    function closeDataSourceForm() {
        dsState.editId = null;
        document.getElementById('dataSourceForm').style.display = 'none';
        document.getElementById('dataSourceOpenBtn').style.display = '';
        showDataSourceError('');
    }

    // "12 metrics, used in 3 places"
    function dsCounts(metrics, uses) {
        const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
        return `${plural(metrics, 'metric')}, ${uses ? `used in ${plural(uses, 'place')}` : 'not used yet'}`;
    }

    // One built-in source (read only). SharePoint is one source made of every
    // KPI spreadsheet, so its files are listed under it with no counts of
    // their own.
    function builtInRowHtml(s) {
        const files = (s.files || []).map(f =>
            `<div class="ds-file">${escHtml(f.name)} <span class="ds-tag ${f.enabled ? 'on' : 'off'}">${f.enabled ? 'On' : 'Off'}</span></div>`).join('');
        const manage = s.id === 'sharepoint'
            ? `<a class="btn btn-sm" href="${BASE_URL}/settings/section/sharepoint" target="_blank" rel="noopener"
                  title="Add, change or switch spreadsheets on and off"><i class="fas fa-external-link-alt"></i> SharePoint Settings</a>`
            : '<span class="ds-tag off" title="Built into the page: cannot be changed or removed">Built In</span>';
        return `<div class="ds-row">
            <strong>${escHtml(s.name)}</strong>
            <span class="ds-detail">${escHtml(s.detail)} - ${escHtml(dsCounts(s.metrics, s.uses))}</span>
            ${manage}<span></span>
        </div>${files ? `<div class="ds-files">${files}</div>` : ''}`;
    }

    // Daily Summary heads the Snowflake box; SharePoint and Autotrader Costs
    // each get a box of their own.
    function renderDataSourceBuiltIn() {
        const find = id => dsState.builtIn.find(s => s.id === id);
        const sp = find('sharepoint'), at = find('autotrader');
        if (!sp && !at) {
            document.getElementById('dataSourceBuiltIn').innerHTML =
                '<div class="point-status" style="padding:2px 0 6px;">The built-in sources could not be read. The server may need restarting to pick up the latest code.</div>';
            return;
        }
        const spFiles = sp ? (sp.files || []).map(f => `<div class="ds-row">
                <strong>${escHtml(f.name)}</strong>
                <span class="ds-detail">KPI spreadsheet</span>
                <span class="ds-tag ${f.enabled ? 'on' : 'off'}">${f.enabled ? 'On' : 'Off'}</span><span></span>
            </div>`).join('') || '<div class="point-status" style="padding:2px 0 6px;">No KPI spreadsheets set up.</div>' : '';
        const spBox = sp ? `<div class="ds-section">
            <div class="ds-section-head">
                <span><i class="fas fa-file-excel"></i> SharePoint Spreadsheets</span>
                <a class="btn btn-edit btn-sm" href="${BASE_URL}/settings/section/sharepoint" target="_blank" rel="noopener"
                   title="Add, change or switch spreadsheets on and off"><i class="fas fa-external-link-alt"></i> SharePoint Settings</a>
            </div>
            <div class="ds-section-sub">Added and switched on or off in Settings > SharePoint. Charts read them as one source:
                ${escHtml(dsCounts(sp.metrics, sp.uses))} across all of them.</div>
            ${spFiles}
        </div>` : '';
        const atBox = at ? `<div class="ds-section">
            <div class="ds-section-head">
                <span><i class="fas fa-car"></i> Autotrader Costs</span>
                <span class="ds-tag off" title="Built into the page: cannot be changed or removed">Built In</span>
            </div>
            <div class="ds-section-sub">${escHtml(at.detail)}: ${escHtml(dsCounts(at.metrics, at.uses))}.</div>
        </div>` : '';
        document.getElementById('dataSourceBuiltIn').innerHTML = spBox + atBox;
    }

    function renderDataSourceList() {
        renderDataSourceBuiltIn();
        const dailySummary = dsState.builtIn.find(s => s.id === 'snowflake');
        document.getElementById('dataSourceList').innerHTML = (dailySummary ? builtInRowHtml(dailySummary) : '') + dsState.sources.map(s => {
            const uses = dsState.uses[s.id] || 0;
            const detail = s.shape === 'long'
                ? `${s.table} - one row per date per ${s.split_column}, figures from ${s.value_column}`
                : `${s.table} - one row per date`;
            const used = uses ? ` - used in ${uses} place${uses === 1 ? '' : 's'}` : '';
            return `<div class="ds-row">
                <strong>${escHtml(s.name)}</strong>
                <span class="ds-detail">${escHtml(detail + used)}</span>
                <button type="button" class="btn btn-sm" title="Edit or rename" onclick="editDataSource('${escHtml(s.id)}')"><i class="fas fa-pen"></i></button>
                <button type="button" class="btn btn-remove-chart" ${uses ? 'disabled' : ''}
                        title="${uses ? 'Used by a chart: change or remove those charts first' : 'Remove'}"
                        onclick="deleteDataSource('${escHtml(s.id)}')"><i class="fas fa-trash"></i></button>
            </div>`;
        }).join('') + (dsState.sources.length ? ''
            : '<div class="point-status" style="padding:2px 0 6px;">No tables added yet.</div>');
    }

    function showDataSourceError(message) {
        const el = document.getElementById('dataSourceError');
        el.textContent = message || '';
        el.classList.toggle('show', !!message);
    }

    function fillDsSelect(id, values, value, blank) {
        const select = document.getElementById(id);
        select.innerHTML = `<option value="">${escHtml(blank)}</option>`
            + values.map(v => `<option value="${escHtml(v)}">${escHtml(v)}</option>`).join('');
        select.value = values.includes(value) ? value : '';
    }

    async function resetDataSourceForm() {
        dsState.editId = null;
        dsState.columns = [];
        showDataSourceError('');
        document.getElementById('dataSourceFormTitle').textContent = 'Add Data Source';
        document.getElementById('dataSourceSaveBtn').innerHTML = '<i class="fas fa-plus"></i> Add';
        document.getElementById('dsName').value = '';
        document.getElementById('dsShape').value = 'wide';
        renderDataSourceColumns();
        await loadDataSourceTables();
        if (dsState.editId) return;   // an edit was opened while the list loaded
        fillDsSelect('dsTable', dsState.tables || [], '', '(choose a table)');
    }

    async function editDataSource(id) {
        const s = dsState.sources.find(x => x.id === id);
        if (!s) return;
        document.getElementById('dataSourceForm').style.display = '';
        document.getElementById('dataSourceOpenBtn').style.display = 'none';
        dsState.editId = id;
        showDataSourceError('');
        document.getElementById('dataSourceFormTitle').textContent = `Edit ${s.name}`;
        document.getElementById('dataSourceSaveBtn').innerHTML = '<i class="fas fa-save"></i> Save';
        document.getElementById('dsName').value = s.name;
        document.getElementById('dsShape').value = s.shape;
        await loadDataSourceTables();
        const all = dsState.tables || [];
        fillDsSelect('dsTable', all.includes(s.table) ? all : [s.table, ...all], s.table, '(choose a table)');
        loadDataSourceColumns(s.table, s);
    }

    // The chosen table's columns, read live so new columns show straight away.
    async function loadDataSourceColumns(table, keep = null) {
        dsState.columns = [];
        showDataSourceError('');
        if (table) {
            ['dsDate', 'dsSplit', 'dsValue'].forEach(id => fillDsSelect(id, [], '', 'Loading columns...'));
            setDataSourceBusy(`Loading the columns of ${table}...`);
            try {
                const resp = await fetch(BASE_URL + '/kpi/data-sources/columns?table=' + encodeURIComponent(table)).then(r => r.json());
                if (resp.status !== 'ok') throw new Error(resp.message || 'The columns could not be read');
                dsState.columns = resp.columns || [];
            } catch (err) {
                showDataSourceError(err.message);
            } finally {
                setDataSourceBusy('');
            }
        }
        renderDataSourceColumns(keep);
    }

    // Date, name and value column lists for the chosen table and layout.
    // keep: the entry being edited, whose columns are chosen; otherwise the
    // usual names (DT / DATE, EVENT_NAME) are picked where the table has them.
    function renderDataSourceColumns(keep = null) {
        const long = document.getElementById('dsShape').value === 'long';
        document.querySelectorAll('#setGroupDataSources .ds-long-only').forEach(el => { el.style.display = long ? '' : 'none'; });
        const cols = dsState.columns;
        const dates = cols.filter(c => DS_DATE_TYPES.includes(c.type)).map(c => c.column);
        const numbers = cols.filter(c => DS_NUMBER_TYPES.includes(c.type)).map(c => c.column);
        const all = cols.map(c => c.column);
        const current = id => document.getElementById(id).value;
        const pick = (id, list, field, guesses) => {
            const wanted = keep ? keep[field] : (list.includes(current(id)) ? current(id) : guesses.find(g => list.includes(g)) || list[0]);
            return wanted || '';
        };
        fillDsSelect('dsDate', dates, pick('dsDate', dates, 'date_column', ['DT', 'DATE']), '(choose the date column)');
        const date = current('dsDate');
        const names = all.filter(c => c !== date);
        fillDsSelect('dsSplit', names, pick('dsSplit', names, 'split_column', ['EVENT_NAME']), '(choose the name column)');
        const values = numbers.filter(c => c !== date && c !== current('dsSplit'));
        fillDsSelect('dsValue', values, pick('dsValue', values, 'value_column', ['EVENT_VOLUME']), '(choose the value column)');
    }

    // Reads the metric lists again, e.g. after a data source is added.
    async function reloadMetricsCatalogue() {
        const resp = await fetch(BASE_URL + '/kpi/metrics').then(r => r.json());
        if (resp.status !== 'ok') return;
        metricsCatalogue = resp.metrics || [];
        metricsByKey = Object.fromEntries(metricsCatalogue.map(m => [metricKey(m.source, m.column), m]));
        renderNamesDraft();
    }

    async function saveDataSource() {
        const long = document.getElementById('dsShape').value === 'long';
        const entry = {
            name: document.getElementById('dsName').value.trim(),
            table: document.getElementById('dsTable').value,
            shape: long ? 'long' : 'wide',
            date_column: document.getElementById('dsDate').value,
        };
        if (long) {
            entry.split_column = document.getElementById('dsSplit').value;
            entry.value_column = document.getElementById('dsValue').value;
        }
        if (dsState.editId) entry.id = dsState.editId;
        showDataSourceError('');
        setDataSourceBusy(`Checking ${entry.table || 'the table'} and saving...`);
        let saved = false;
        try {
            const resp = await fetch(BASE_URL + '/kpi/data-sources', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: entry }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'The data source could not be saved');
            saved = true;
        } catch (err) {
            showDataSourceError(err.message);
        } finally {
            setDataSourceBusy('');
        }
        if (!saved) return;
        closeDataSourceForm();
        await loadDataSourcesTab();
        await reloadMetricsCatalogue();
    }

    async function deleteDataSource(id) {
        const s = dsState.sources.find(x => x.id === id);
        if (!s || !confirm(`Remove the data source ${s.name}?`)) return;
        try {
            const resp = await fetch(BASE_URL + '/kpi/data-sources?id=' + encodeURIComponent(id), { method: 'DELETE' })
                .then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'The data source could not be removed');
            if (dsState.editId === id) closeDataSourceForm();
            await loadDataSourcesTab();
            await reloadMetricsCatalogue();
        } catch (err) {
            showDataSourceError(err.message);
        }
    }

    // Settings tabs. The popup reopens on the tab last used this visit.
    let settingsTab = 'setGroupCharts';

    function showSettingsTab(id) {
        settingsTab = id;
        // Picks up section names typed on the Sections tab.
        if (id === 'setGroupCharts') renderChartDraft();
        if (id === 'setGroupTargets') loadTargetsTab();
        if (id === 'setGroupDataSources') loadDataSourcesTab();
        document.querySelectorAll('#settingsModal .settings-pane').forEach(p => p.classList.toggle('active', p.id === id));
        document.querySelectorAll('#settingsModal .settings-tabs > button').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
        // A tab with sub-sections opens them out under it in the menu.
        document.querySelectorAll('#settingsModal .settings-subtabs').forEach(g => g.classList.toggle('open', g.dataset.parent === id));
    }

    function restoreSettingsGroups() {
        showSettingsTab(settingsTab);
        showChartSubtab(chartSubtab, false);
    }

    // Sub-sections of the Charts tab. Reopens on the one last used this visit.
    let chartSubtab = 'chartSubList';

    // Picking one from the menu also opens the Charts tab; reopening the
    // popup (switchTab false) leaves whichever tab was last used.
    function showChartSubtab(id, switchTab = true) {
        chartSubtab = id;
        if (switchTab && settingsTab !== 'setGroupCharts') showSettingsTab('setGroupCharts');
        document.querySelectorAll('#setGroupCharts .chart-subpane').forEach(p => p.classList.toggle('active', p.id === id));
        document.querySelectorAll('#settingsModal .settings-subtabs[data-parent="setGroupCharts"] button')
            .forEach(b => b.classList.toggle('active', b.dataset.sub === id));
    }

    // ── View Vehicles columns (edited in the Settings popup) ─────────────────
    // vehViewDraft = { table, shown: [{ column, label, format, decimals }],
    // live: [{ column, type }], saved: the setting as loaded, to tell whether
    // anything changed }. null while loading or when the columns could not be
    // read; Save then leaves the stored setting alone.
    let vehViewDraft = null;

    function vehViewPayload(draft) {
        return { table: draft.table, columns: draft.shown.map(c => ({
            column: c.column, label: c.label.trim(), format: c.format, decimals: c.decimals,
            // A column switched away from number or % drops its total.
            summary: VEH_NUMBER_FORMATS.includes(c.format) ? (c.summary || 'none') : 'none',
            blank_grey: !!c.blank_grey })),
            grey_rules: (draft.grey_rules || []).map(r => ({
                column: r.column, op: r.op,
                value: VEH_NO_VALUE_OPS.includes(r.op) ? '' : String(r.value || '').trim() })),
            filters: (draft.filters || []).filter(f => f.column).map(f => ({ column: f.column, label: (f.label || '').trim() })) };
    }

    // A saved column, with a format and decimals filled in where it has none
    // (saved before formats existed, or just added).
    function vehColumnSetting(c, type) {
        const format = c.format || defaultVehFormat(c.column, type);
        const decimals = Number.isInteger(c.decimals) ? c.decimals : (type === 'FLOAT' || format === 'pct' ? 2 : 0);
        return { column: c.column, label: c.label || '', format, decimals, summary: c.summary || 'none',
                 blank_grey: !!c.blank_grey };
    }

    async function loadVehColumnDraft(table) {
        vehViewDraft = null;
        document.getElementById('vehColSearch').value = '';
        document.getElementById('vehShownList').innerHTML = '<div class="point-status" style="padding:6px;">Loading columns...</div>';
        document.getElementById('vehHiddenList').innerHTML = '';
        try {
            const resp = await fetchVehicleColumns(table);
            const types = Object.fromEntries(resp.columns.map(c => [c.column, c.type]));
            // Each table keeps its own saved columns; switching table loads them.
            const shownBefore = resp.view.columns;
            vehViewDraft = {
                table: resp.table,
                shown: shownBefore.map(c => vehColumnSetting(c, types[c.column])),
                live: resp.columns,
                grey_rules: JSON.parse(JSON.stringify(resp.view.grey_rules || [])),
                filters: JSON.parse(JSON.stringify(resp.view.filters || [])),
                saved: JSON.stringify(vehViewPayload({ table: resp.view.table,
                    filters: resp.view.filters || [],
                    shown: resp.view.columns.map(c => ({ column: c.column, label: c.label || '',
                        format: c.format, decimals: c.decimals, summary: c.summary, blank_grey: c.blank_grey })),
                    grey_rules: resp.view.grey_rules || [] })),
            };
            renderGreyRules();
            renderVehQuickFilters();
            const select = document.getElementById('vehTableSelect');
            const tables = resp.tables.includes(resp.table) ? resp.tables : [resp.table, ...resp.tables];
            select.innerHTML = tables.map(t => `<option value="${escHtml(t)}">${escHtml(t)}</option>`).join('');
            select.value = resp.table;
            renderVehColumnDraft();
        } catch (err) {
            document.getElementById('vehShownList').innerHTML =
                `<div class="point-status" style="padding:6px;">Could not read the columns: ${escHtml(err.message)}</div>`;
        }
    }

    // ── Grey-out rules (Settings) ──
    // A row is greyed out when ANY rule matches. Each rule is a column, an
    // operator suited to the column's type, and a value, as for chart filters.
    function greyRuleKind(column) {
        const col = vehViewDraft ? vehViewDraft.live.find(c => c.column === column) : null;
        if (!col) return null;
        if (col.type === 'BOOLEAN') return 'bool';
        if (VEHICLE_NUMBER_TYPES.includes(col.type)) return 'num';
        if (VEH_DATE_TYPES.includes(col.type)) return 'date';
        return 'text';
    }

    function renderGreyRules() {
        if (!vehViewDraft) return;
        const columns = vehViewDraft.live.map(c => c.column);
        document.getElementById('vehGreyList').innerHTML = vehViewDraft.grey_rules.map((r, i) => {
            const kind = greyRuleKind(r.column);
            const ops = vehOpsFor(kind);
            // A saved operator the column's type does not offer moves to the first one.
            if (kind && !ops.some(([op]) => op === r.op)) { r.op = ops[0][0]; r.value = ''; }
            const noValue = VEH_NO_VALUE_OPS.includes(r.op);
            const list = r.op === 'in' || r.op === 'not in';
            const inputType = !list && kind === 'num' ? 'number' : !list && kind === 'date' ? 'date' : 'text';
            const cols = (r.column && !columns.includes(r.column) ? [r.column] : []).concat(columns);
            return `
                <div class="veh-filter">
                    <select onchange="onGreyRuleColumn(${i}, this.value)">
                        <option value="">(choose a column)</option>
                        ${cols.map(c => `<option value="${escHtml(c)}" ${c === r.column ? 'selected' : ''}>${escHtml(c)}${columns.includes(c) ? '' : ' (not in the table)'}</option>`).join('')}
                    </select>
                    <select onchange="vehViewDraft.grey_rules[${i}].op = this.value; renderGreyRules()">
                        ${ops.map(([op, label]) => `<option value="${escHtml(op)}" ${op === r.op ? 'selected' : ''}>${escHtml(label)}</option>`).join('')}
                    </select>
                    <input type="${inputType}" ${inputType === 'number' ? 'step="any"' : ''} maxlength="500"
                           value="${escHtml(noValue ? '' : r.value || '')}" placeholder="${noValue ? '(no value)' : list ? 'a, b, c' : ''}"
                           ${noValue ? 'disabled' : ''} oninput="vehViewDraft.grey_rules[${i}].value = this.value">
                    <button type="button" class="btn btn-remove-chart" title="Remove rule"
                            onclick="vehViewDraft.grey_rules.splice(${i}, 1); renderGreyRules()"><i class="fas fa-trash"></i></button>
                </div>`;
        }).join('') || '<div class="point-status" style="padding:2px 0 6px;">No rules - no rows are greyed out.</div>';
    }

    function addGreyRule() {
        if (!vehViewDraft) return;
        vehViewDraft.grey_rules.push({ column: '', op: '=', value: '' });
        renderGreyRules();
    }

    function onGreyRuleColumn(i, column) {
        const r = vehViewDraft.grey_rules[i];
        r.column = column;
        const ops = vehOpsFor(greyRuleKind(column)).map(([op]) => op);
        if (!ops.includes(r.op)) { r.op = ops[0]; r.value = ''; }
        renderGreyRules();
    }

    // ── Quick filters (Settings) ──
    // The filters shown above the vehicle list for this table: a column and the
    // label shown above it. Any column of the table can be used.
    function renderVehQuickFilters() {
        if (!vehViewDraft) return;
        const columns = vehViewDraft.live.map(c => c.column);
        const list = vehViewDraft.filters;
        document.getElementById('vehQuickList').innerHTML = list.map((f, i) => {
            const cols = (f.column && !columns.includes(f.column) ? [f.column] : []).concat(columns);
            return `
                <div class="veh-quick-row">
                    <select onchange="vehViewDraft.filters[${i}].column = this.value; renderVehQuickFilters()">
                        <option value="">(choose a column)</option>
                        ${cols.map(c => `<option value="${escHtml(c)}" ${c === f.column ? 'selected' : ''}>${escHtml(c)}${columns.includes(c) ? '' : ' (not in the table)'}</option>`).join('')}
                    </select>
                    <input type="text" maxlength="60" value="${escHtml(f.label || '')}" placeholder="${escHtml(f.column || 'Name')}"
                           title="Name shown above the filter" oninput="vehViewDraft.filters[${i}].label = this.value">
                    <button type="button" class="btn btn-edit" title="Move up" onclick="moveVehQuickFilter(${i}, -1)"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down" onclick="moveVehQuickFilter(${i}, 1)"
                            ${i === list.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="Remove filter"
                            onclick="vehViewDraft.filters.splice(${i}, 1); renderVehQuickFilters()"><i class="fas fa-trash"></i></button>
                </div>`;
        }).join('') || '<div class="point-status" style="padding:2px 0 6px;">No quick filters.</div>';
    }

    function addVehQuickFilter() {
        if (!vehViewDraft) return;
        vehViewDraft.filters.push({ column: '', label: '' });
        renderVehQuickFilters();
    }

    function moveVehQuickFilter(i, step) {
        const list = vehViewDraft.filters, j = i + step;
        if (j < 0 || j >= list.length) return;
        [list[i], list[j]] = [list[j], list[i]];
        renderVehQuickFilters();
    }

    // Each table's columns are saved separately. Switching loads the other
    // table's saved setup; any unsaved edits to the current one are lost, so
    // warn first (Save, with the Settings Save button, keeps them).
    function onVehTableChange() {
        const table = document.getElementById('vehTableSelect').value;
        if (vehViewDraft && table === vehViewDraft.table) return;
        if (vehViewDraft && JSON.stringify(vehViewPayload(vehViewDraft)) !== vehViewDraft.saved
            && !confirm(`Unsaved changes to ${vehViewDraft.table} will be lost - Save first to keep them. Switch table anyway?`)) {
            document.getElementById('vehTableSelect').value = vehViewDraft.table;
            return;
        }
        loadVehColumnDraft(table);
    }

    function renderVehColumnDraft() {
        if (!vehViewDraft) return;
        const types = Object.fromEntries(vehViewDraft.live.map(c => [c.column, c.type]));
        const shown = vehViewDraft.shown;
        document.getElementById('vehShownHead').textContent = `Shown, in order (${shown.length})`;
        document.getElementById('vehShownList').innerHTML = shown.length ? shown.map((c, i) => {
            const missing = !types[c.column];
            const formats = VEH_FORMATS.map(([value, label]) =>
                `<option value="${value}" ${value === c.format ? 'selected' : ''}>${label}</option>`).join('');
            const decimals = [0, 1, 2, 3, 4].map(n =>
                `<option value="${n}" ${n === c.decimals ? 'selected' : ''}>${n} dp</option>`).join('');
            const hasDecimals = VEH_NUMBER_FORMATS.includes(c.format);
            // Totals and averages only make sense on number and % columns.
            const summaries = [['none', 'No total'], ['sum', 'Total'], ['avg', 'Average']].map(([value, label]) =>
                `<option value="${value}" ${value === (hasDecimals ? c.summary : 'none') ? 'selected' : ''}>${label}</option>`).join('');
            return `
                <div class="veh-col-item${missing ? ' missing' : ''}">
                    <span class="veh-col-name" title="${escHtml(c.column)}${missing ? ' - not in the table' : ''}">${escHtml(c.column)}</span>
                    <input type="text" maxlength="60" value="${escHtml(c.label)}" placeholder="${escHtml(c.column)}"
                           title="Heading in the list" oninput="vehViewDraft.shown[${i}].label = this.value">
                    <select class="veh-col-format" title="How the values show"
                            onchange="vehViewDraft.shown[${i}].format = this.value; renderVehColumnDraft()">${formats}</select>
                    <select class="veh-col-decimals" title="Decimal places (number and %)" ${hasDecimals ? '' : 'disabled'}
                            onchange="vehViewDraft.shown[${i}].decimals = parseInt(this.value, 10)">${decimals}</select>
                    <select class="veh-col-summary" title="Figure in the row at the bottom of the list" ${hasDecimals ? '' : 'disabled'}
                            onchange="vehViewDraft.shown[${i}].summary = this.value">${summaries}</select>
                    <label class="veh-col-blank" title="Leave this column blank on grey-out rows (e.g. cancelled deals), and out of its total or average">
                        <input type="checkbox" ${c.blank_grey ? 'checked' : ''}
                               onchange="vehViewDraft.shown[${i}].blank_grey = this.checked"> Blank on grey</label>
                    <button type="button" class="btn btn-edit" title="Move up" onclick="moveVehColumn(${i}, -1)"
                            ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down" onclick="moveVehColumn(${i}, 1)"
                            ${i === shown.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="Stop showing" onclick="hideVehColumn(${i})">
                        <i class="fas fa-times"></i></button>
                </div>`;
        }).join('') : '<div class="point-status" style="padding:6px;">No columns chosen yet. Add them from the list on the right.</div>';

        const shownNames = new Set(shown.map(c => c.column));
        const search = document.getElementById('vehColSearch').value.trim().toUpperCase();
        document.getElementById('vehHiddenList').innerHTML = vehViewDraft.live
            .filter(c => !shownNames.has(c.column) && (!search || c.column.includes(search)))
            .map(c => `
                <div class="veh-col-item">
                    <span class="veh-col-name" title="${escHtml(c.column)}">${escHtml(c.column)}</span>
                    <span class="veh-col-type">${escHtml(c.type.toLowerCase())}</span>
                    <button type="button" class="btn btn-edit" onclick="showVehColumn('${escHtml(c.column)}')">
                        <i class="fas fa-plus"></i> Add</button>
                </div>`).join('');
    }

    function moveVehColumn(i, step) {
        const shown = vehViewDraft.shown, j = i + step;
        if (j < 0 || j >= shown.length) return;
        [shown[i], shown[j]] = [shown[j], shown[i]];
        renderVehColumnDraft();
    }

    function hideVehColumn(i) {
        vehViewDraft.shown.splice(i, 1);
        renderVehColumnDraft();
    }

    function showVehColumn(column) {
        const live = vehViewDraft.live.find(c => c.column === column);
        vehViewDraft.shown.push(vehColumnSetting({ column }, live ? live.type : null));
        renderVehColumnDraft();
    }

    async function saveVehColumnDraft() {
        if (!vehViewDraft) return;
        const payload = vehViewPayload(vehViewDraft);
        if (JSON.stringify(payload) === vehViewDraft.saved) return;
        const resp = await fetch(BASE_URL + '/kpi/vehicle-view', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ view: payload }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'View Vehicles columns save failed');
        vehViewDraft.saved = JSON.stringify(payload);
    }

    // ── Sections (edited in the Settings popup) ──────────────────────────────
    // Each item is { name, from, hidden, source }: `from` is the name it had
    // when the popup opened (null for a new one), so the server can carry
    // charts across a rename; hidden = no tab and its charts not loaded, but
    // kept; source = the one data source its charts may use (null = any).
    let sectionDraft = [];

    function sectionDraftItem(name) {
        return { name, from: name, hidden: hiddenSections.has(name), source: sectionSources[name] || null,
                 company: sectionCompany(name) };
    }

    function renderSectionDraft() {
        const last = sectionDraft.length - 1;
        const sourceOptions = s => '<option value="">Any Data Source</option>' + metricSources(true).map(src =>
            `<option value="${escHtml(src)}" ${src === s.source ? 'selected' : ''}>${escHtml(sourceLabel(src))}</option>`).join('');
        document.getElementById('sectionList').innerHTML = sectionDraft.map((s, i) => {
            const charts = chartDraft.filter(c => c.sec === s).length;
            const removeTitle = charts ? 'Move or remove its charts first' : 'Remove section';
            return `
                <div class="section-item ${s.hidden ? 'is-hidden' : ''}">
                    <input type="text" maxlength="60" value="${escHtml(s.name)}"
                           oninput="sectionDraft[${i}].name = this.value">
                    <span class="section-count">${charts} chart${charts === 1 ? '' : 's'}</span>
                    <select title="Charts in this section can only use this data source"
                            onchange="sectionDraft[${i}].source = this.value || null">${sourceOptions(s)}</select>
                    <select class="section-company" title="Which company this section is for (chart notes follow it)"
                            onchange="sectionDraft[${i}].company = this.value">${Object.entries(COMPANIES).map(([k, label]) =>
                        `<option value="${k}" ${k === (s.company || 'carsupermarket') ? 'selected' : ''}>${label}</option>`).join('')}</select>
                    <button type="button" class="btn btn-edit" title="${s.hidden ? 'Show Section' : 'Hide Section'}"
                            onclick="toggleSectionHidden(${i})">
                        <i class="fas ${s.hidden ? 'fa-eye-slash' : 'fa-eye'}"></i></button>
                    <button type="button" class="btn btn-edit" title="Move up"
                            onclick="moveSectionDraft(${i}, -1)" ${i === 0 ? 'disabled' : ''}>
                        <i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down"
                            onclick="moveSectionDraft(${i}, 1)" ${i === last ? 'disabled' : ''}>
                        <i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-remove-chart" title="${removeTitle}"
                            onclick="removeSectionDraft(${i})" ${charts ? 'disabled' : ''}>
                        <i class="fas fa-trash"></i></button>
                </div>`;
        }).join('');
    }

    function toggleSectionHidden(i) {
        sectionDraft[i].hidden = !sectionDraft[i].hidden;
        renderSectionDraft();
    }

    function addSectionDraft() {
        sectionDraft.push({ name: '', from: null, hidden: false, source: null, company: 'carsupermarket' });
        renderSectionDraft();
        renderChartDraft();
        const inputs = document.querySelectorAll('#sectionList input');
        inputs[inputs.length - 1].focus();
    }

    function moveSectionDraft(i, step) {
        const j = i + step;
        if (j < 0 || j >= sectionDraft.length) return;
        [sectionDraft[i], sectionDraft[j]] = [sectionDraft[j], sectionDraft[i]];
        renderSectionDraft();
        renderChartDraft();
    }

    function removeSectionDraft(i) {
        sectionDraft.splice(i, 1);
        renderSectionDraft();
        renderChartDraft();
    }

    // ── Charts (Settings > Charts) ───────────────────────────────────────────
    // One item per saved chart: { id, title, hidden, sec }. `sec` points at the
    // chart's item in sectionDraft, so a section renamed or added in the same
    // popup is followed. Shown grouped by section, in sectionDraft order.
    let chartDraft = [];

    function loadChartDraft() {
        chartDraft = layout.rows.map(r => ({
            id: r.id,
            title: rowTitle(r),
            hidden: !!r.hidden,
            sec: sectionDraft.find(s => s.from === r.section),
        }));
        renderChartDraft();
    }

    function sectionLabel(s) {
        return s.name.trim() || '(new section)';
    }

    function renderChartDraft() {
        const options = sel => sectionDraft.map((s, k) =>
            `<option value="${k}" ${s === sel ? 'selected' : ''}>${escHtml(sectionLabel(s))}</option>`).join('');
        const html = sectionDraft.map(s => {
            const items = chartDraft.filter(c => c.sec === s);
            if (!items.length) return '';
            return `<div class="chart-list-section">${escHtml(sectionLabel(s))}</div>` + items.map((c, n) => {
                const i = chartDraft.indexOf(c);
                return `
                <div class="chart-item ${c.hidden ? 'is-hidden' : ''}">
                    <label><input type="checkbox" ${c.hidden ? '' : 'checked'} onchange="setChartHidden(${i}, !this.checked)"> Show</label>
                    <span class="chart-item-title" title="${escHtml(c.title)}">${escHtml(c.title)}</span>
                    <select onchange="setChartSection(${i}, this.value)">${options(s)}</select>
                    <button type="button" class="btn btn-edit" title="Move up"
                            onclick="moveChartDraft(${i}, -1)" ${n === 0 ? 'disabled' : ''}>
                        <i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-edit" title="Move down"
                            onclick="moveChartDraft(${i}, 1)" ${n === items.length - 1 ? 'disabled' : ''}>
                        <i class="fas fa-arrow-down"></i></button>
                </div>`;
            }).join('');
        }).join('');
        document.getElementById('chartList').innerHTML = html || '<div class="modal-hint">No charts yet.</div>';
    }

    function setChartHidden(i, hidden) {
        chartDraft[i].hidden = hidden;
        renderChartDraft();
    }

    // Moved to another section: goes to the bottom of it.
    function setChartSection(i, k) {
        const [c] = chartDraft.splice(i, 1);
        c.sec = sectionDraft[parseInt(k, 10)];
        chartDraft.push(c);
        renderChartDraft();
        renderSectionDraft();
    }

    // Swap with the next chart up or down in the same section.
    function moveChartDraft(i, step) {
        const c = chartDraft[i];
        let j = i + step;
        while (j >= 0 && j < chartDraft.length && chartDraft[j].sec !== c.sec) j += step;
        if (j < 0 || j >= chartDraft.length) return;
        [chartDraft[i], chartDraft[j]] = [chartDraft[j], chartDraft[i]];
        renderChartDraft();
    }

    // The charts as they will be saved: { id, section, hidden }, grouped by
    // section in the new section order. Worked out before the sections are
    // saved, since that resets sectionDraft.
    function chartDraftPlan() {
        const plan = sectionDraft.flatMap(s => chartDraft
            .filter(c => c.sec === s)
            .map(c => ({ id: c.id, section: s.name.trim(), hidden: c.hidden })));
        // A chart left out here would be deleted by the save.
        if (plan.length !== layout.rows.length) throw new Error('Some charts have no section - reload the page');
        return plan;
    }

    function chartsChanged(plan) {
        const now = layout.rows.map(r => ({ id: r.id, section: r.section, hidden: !!r.hidden }));
        return JSON.stringify(plan) !== JSON.stringify(now);
    }

    async function saveChartPlan(plan) {
        const byId = Object.fromEntries(layout.rows.map(r => [r.id, r]));
        const rows = plan.map(p => {
            const row = { ...byId[p.id], section: p.section };
            if (p.hidden) row.hidden = true; else delete row.hidden;
            return row;
        });
        await saveLayout(rows);
        layout.rows = rows;
        renderLayout();
        // Charts shown again have no chart drawn yet.
        shownRows().forEach(row => {
            if (rowCharts[row.id]) return;
            if (customRowData[row.id]) renderCustomRow(row, customRowData[row.id]);
            else loadCustomRow(row);
        });
    }

    function sectionsChanged() {
        return sectionDraft.length !== SECTIONS.length
            || sectionDraft.some((s, i) => s.from !== SECTIONS[i] || s.name.trim() !== SECTIONS[i]
                || !!s.hidden !== hiddenSections.has(SECTIONS[i])
                || (s.source || null) !== (sectionSources[SECTIONS[i]] || null)
                || (s.company || 'carsupermarket') !== sectionCompany(SECTIONS[i]));
    }

    // `holdOn`: saved section names to keep for now, because charts are still
    // saved in them until the chart moves are saved (see saveSectionsAndCharts).
    async function saveSections(holdOn = []) {
        const items = sectionDraft.map(s => ({ name: s.name.trim(), from: s.from, hidden: !!s.hidden, source: s.source || null, company: s.company || 'carsupermarket' }))
            .concat(holdOn.map(name => ({ name, from: name })));
        const resp = await fetch(BASE_URL + '/kpi/sections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sections: items }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'section save failed');
        SECTIONS = resp.sections;
        layout = resp.layout;
        hiddenSections = new Set(resp.hidden_sections || []);
        sectionSources = resp.section_sources || {};
        sectionCompanies = resp.section_companies || {};
        renderLayout();
        redrawAllCharts();   // all-charts notes follow the section's company
        // A section shown again has charts not drawn yet.
        shownRows().forEach(row => {
            if (!rowCharts[row.id]) {
                if (customRowData[row.id]) renderCustomRow(row, customRowData[row.id]);
                else loadCustomRow(row);
            }
        });
        // Drop the blocks of sections that were renamed or removed. renderLayout
        // has already moved their charts into the new blocks.
        const keep = new Set([...SECTIONS.map(sectionDomId), MY_CHARTS_DOM_ID]);
        document.querySelectorAll('#sectionsContainer > .category-section').forEach(el => {
            if (!keep.has(el.id)) el.remove();
        });
        // Saved: the popup's list now starts from the saved names.
        sectionDraft = SECTIONS.map(sectionDraftItem);
        loadChartDraft();
        renderSectionDraft();
    }

    // Sections first, so new and renamed ones exist before charts move into
    // them. A removed section whose charts are being moved out is kept until
    // the charts are saved, then removed with a second section save.
    async function saveSectionsAndCharts() {
        const plan = chartDraftPlan();
        let keep = [];
        if (sectionsChanged()) {
            const kept = new Set(sectionDraft.map(s => s.from));
            keep = SECTIONS.filter(name => !kept.has(name) && layout.rows.some(r => r.section === name));
            const finalNames = sectionDraft.map(s => ({ name: s.name.trim(), hidden: !!s.hidden, source: s.source || null,
                                                        company: s.company || 'carsupermarket' }));
            await saveSections(keep);
            if (keep.length) sectionDraft = finalNames.map(f => ({ ...f, from: f.name }));
        }
        if (chartsChanged(plan)) await saveChartPlan(plan);
        if (keep.length) await saveSections();
    }

    // Autotrader rows popup, opened from Settings > Autotrader Costs. The rows
    // are the same draft the Settings Save button saves.
    function openAtRowsModal() {
        // Opens with this month and next month expanded (where they have rows)
        // and everything else folded.
        atCollapseAll(true);
        const thisMonth = new Date().toLocaleDateString('en-CA').slice(0, 7);
        const nextMonth = atAddDays(thisMonth + '-01', 32).slice(0, 7);
        [thisMonth, nextMonth].forEach(m => {
            atCollapsed.delete(m);
            atCollapsedYears.delete(m.slice(0, 4));
        });
        renderAtDraft();
        const modal = document.getElementById('atRowsModal');
        modal.classList.add('open');
        // The popup's heading sticks at -22px; the column headings pin just below it.
        const head = modal.querySelector('.chart-modal-head');
        modal.querySelector('.at-rows-table').style.setProperty('--at-head-h', (head.offsetHeight - 22) + 'px');
    }

    function closeAtRowsModal() {
        document.getElementById('atRowsModal').classList.remove('open');
    }

    function closeSettingsModal() {
        document.getElementById('atRowsModal').classList.remove('open');
        document.getElementById('settingsModal').classList.remove('open');
        // Rows saved by Apply Invoices change the charts, Save or not.
        if (atRowsSaved) {
            atRowsSaved = false;
            loadAll();
        }
    }

    async function saveSettings() {
        // Settings not in this popup (the Weekly Business Review's) are kept as they are.
        const next = { ...settings };
        for (const [name, id] of Object.entries(SETTING_INPUTS)) {
            next[name] = readSettingInput(document.getElementById(id));
        }
        const errorBox = document.getElementById('settingsError');
        if (next.days_window > next.days_total) {
            errorBox.textContent = 'Days shown at once cannot be more than the days available.';
            errorBox.classList.add('show');
            return;
        }
        if (next.days_after >= next.days_total) {
            errorBox.textContent = 'Days available must be more than the days after today.';
            errorBox.classList.add('show');
            return;
        }
        let names, formats, derived;
        try { ({ names, formats } = namesDraftPayload()); derived = derivedDraftPayload(); } catch (err) {
            errorBox.textContent = err.message;
            errorBox.classList.add('show');
            return;
        }
        const button = document.getElementById('settingsSaveBtn');
        button.disabled = true;
        try {
            blockSaveIfLoadFailed();
            await saveSectionsAndCharts();
            await saveVehColumnDraft();
            await saveThemeDraft();
            await saveMetricNames(names);
            await saveDerivedNames(derived);
            await saveMetricFormats(formats);
            const atChanged = await saveAtPeriods();
            const resp = await fetch(BASE_URL + '/kpi/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ settings: next }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'save failed');
            // New Autotrader figures change the charts drawn from them.
            const needsData = atChanged || ['months', 'weeks', 'days_total', 'months_after', 'weeks_after', 'days_after']
                .some(k => resp.settings[k] !== settings[k]);
            settings = resp.settings;
            applyAppearance();
            DayWindow.reset();
            closeSettingsModal();
            // Periods change what the server sends; the window and angle are
            // drawing only, so the loaded data is reused.
            if (needsData) loadAll();
            else renderAllFromCache();
            document.getElementById('statusText').textContent = 'Settings saved';
        } catch (err) {
            errorBox.textContent = 'Save failed: ' + err.message;
            errorBox.classList.add('show');
        } finally {
            button.disabled = false;
        }
    }
