// Weekly Business Review > Report view. Loaded before kpi.js on the review page
// only; everything it calls from kpi.js (escHtml, BASE_URL ...) is only used once
// the page has loaded. wbr_deck.js decides which view shows and calls setVisible.
//
// The server builds every section (routers/wbr_report.py); this draws them. The
// sections have no content until their data is built.
const WbrReport = (() => {
    let visible = false;
    let report = null;        // the last response from the server
    let error = '';
    let loading = false;
    let current = 0;          // index of the section showing

    // Settings > Report (admin). Nothing is saved until the settings have loaded,
    // so a failed load cannot overwrite them with blanks.
    let sections = null;
    let titles = {};

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
            const url = BASE_URL + '/weekly-business-review/report' + (refresh ? '?refresh=1' : '');
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
                    ${!isUserView() ? `<button type="button" class="btn btn-edit" onclick="WbrReport.reload()"><i class="fas fa-sync"></i> Reload Data</button>` : ''}
                </div>${warnings}`;
    }

    function render() {
        if (!visible) return;
        const strip = document.getElementById('wbrReportStrip');
        const meta = document.getElementById('wbrReportMeta');
        const slide = document.getElementById('wbrReportSlide');
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
        slide.innerHTML = section
            ? `<div class="wbr-slide-head"><div class="wbr-slide-title">${escHtml(section.title)}</div></div>
               <div class="wbr-report-empty">This section has no data yet.</div>`
            : '<div class="wbr-report-empty">Every section is switched off in Settings > Report.</div>';
    }

    function show(index) {
        current = index;
        render();
    }

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
        return sections.map((s, i) => `
            <div class="wbr-report-section">
                <label><input type="checkbox" ${s.on ? 'checked' : ''} onchange="WbrReport.setSectionOn(${i}, this.checked)"> ${escHtml(titles[s.id] || s.id)}</label>
                <span class="wbr-strip-btns">
                    <button type="button" class="chart-title-btn" title="Move Up" onclick="WbrReport.moveSection(${i}, -1)" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="chart-title-btn" title="Move Down" onclick="WbrReport.moveSection(${i}, 1)" ${i === sections.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                </span>
            </div>`).join('');
    }

    function drawSections() {
        document.getElementById('wbrReportSections').innerHTML = sections ? sectionsHtml() : '';
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
        drawSections();
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
            status.textContent = '';
            drawSections();
            document.getElementById('wbrReportSources').innerHTML = sourcesHtml(resp.sources || []);
        } catch (err) {
            status.textContent = 'Report settings could not be loaded: ' + err.message;
        }
    }

    function setSectionOn(index, on) {
        sections[index].on = on;
    }

    function moveSection(index, by) {
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
            } }),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Report settings save failed');
        report = null;
    }

    return { setVisible, show, reload: () => load(true), openSettings, saveSettings, setSectionOn, moveSection };
})();
