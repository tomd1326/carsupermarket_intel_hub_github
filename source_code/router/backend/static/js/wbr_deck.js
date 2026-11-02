// Weekly Business Review slides. Loaded before kpi.js on the review page only;
// everything it calls from kpi.js (settings, findRow, customRowData,
// buildRowCharts, wbrSliceData, wbrStart, wbrScrollerHtml, rowTitle, escHtml ...) is
// only used once the page has loaded.
//
// The deck is saved in kpi_layout under 'wbr_deck' (see kpi.py). Until the
// admin first changes a slide nothing is saved, and the grid's charts are
// shown laid out on slides. The grid (wbr_items) is never changed from here.
//
// Each slide item draws its OWN chart from the loaded data, so the same chart
// can sit on any number of slides and the grid's cards are never moved.
// Item x / y / w / h are in grid squares (Settings: Slide Grid Columns / Rows).
const WbrDeck = (() => {
    const VIEW_KEY = 'wbrDeckView';   // admin only, this browser: 'slides' | 'grid' | 'report'
    let saved = null;                 // the deck as stored, or null if never saved
    let deck = null;                  // the deck shown (saved, or laid out from the grid)
    let current = 0;                  // index of the slide showing
    let selected = null;              // admin: id of the item picked on the slide
    const charts = {};                // item id -> Chart
    let saving = Promise.resolve();

    const cols = () => Number(settings.wbr_grid_cols) || 24;
    const rows = () => Number(settings.wbr_grid_rows) || 12;
    const snapOn = () => Number(settings.wbr_snap) !== 0;
    const newId = p => p + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    const round2 = v => Math.round(v * 100) / 100;
    // Editing controls: the admin in Admin View only (User View sees what everyone else does).
    const editing = () => !isUserView();

    function viewPref() {
        if (!IS_ADMIN) return null;
        try { return localStorage.getItem(VIEW_KEY); } catch (e) { return null; }
    }

    // Report, then Slides, show when their setting is on; the admin can also
    // switch view in this browser, to build them before turning them on for everyone.
    function view() {
        const pref = viewPref();
        if (pref) return pref;
        if (Number(settings.wbr_report_on) === 1) return 'report';
        return Number(settings.wbr_slides_on) === 1 ? 'slides' : 'grid';
    }

    function active() {
        return view() === 'slides';
    }

    // The grid's charts laid out on slides: charts per row as on the grid,
    // two rows a slide.
    function fromGrid() {
        const per = Math.max(1, Number(settings.wbr_columns) || 3);
        const w = round2(cols() / per), h = round2(rows() / 2);
        const slides = [];
        wbrItems.forEach((it, i) => {
            const n = i % (per * 2);
            if (n === 0) slides.push({ id: newId('s'), title: 'Weekly Business Review', subtitle: '', items: [] });
            const item = { id: 'd' + it.id, kind: 'chart', ref: it.ref, period: it.period,
                           x: round2((n % per) * w), y: round2(Math.floor(n / per) * h), w, h };
            ['title', 'subtitle'].forEach(k => { if (it[k]) item[k] = it[k]; });
            slides[slides.length - 1].items.push(item);
        });
        if (!slides.length) slides.push({ id: newId('s'), title: 'Weekly Business Review', subtitle: '', items: [] });
        return { slides };
    }

    function getDeck() {
        if (!deck) deck = saved ? JSON.parse(JSON.stringify(saved)) : fromGrid();
        return deck;
    }

    function setSaved(value) {
        saved = value && Array.isArray(value.slides) && value.slides.length ? value : null;
        deck = null;
    }

    function refs() {
        return getDeck().slides.flatMap(s => s.items.map(i => i.ref));
    }

    function slide() {
        const slides = getDeck().slides;
        current = Math.min(Math.max(current, 0), slides.length - 1);
        return slides[current];
    }

    function findItem(id) {
        return slide().items.find(i => i.id === id);
    }

    // The page changes straight away and is put back if the save fails.
    function save() {
        const before = saved;
        const body = JSON.parse(JSON.stringify(getDeck()));
        saving = saving.then(async () => {
            blockSaveIfLoadFailed();
            const resp = await fetch(BASE_URL + '/weekly-business-review/deck', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ deck: body }),
            }).then(r => r.json());
            if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
            saved = body;
        }).catch(err => {
            saved = before;
            deck = null;
            render();
            document.getElementById('statusText').textContent = 'Slides save failed: ' + err.message;
        });
    }

    // ── Drawing ──
    function destroyCharts() {
        Object.keys(charts).forEach(id => { charts[id].destroy(); delete charts[id]; });
    }

    function drawItem(item) {
        const el = document.querySelector(`[data-slide-item="${item.id}"]`);
        const row = findRow(item.ref);
        if (!el) return;
        if (!row) {
            el.querySelector('.wbr-slide-plot').innerHTML = '<div class="wbr-item-missing">This chart has been removed from the KPI page.</div>';
            return;
        }
        const full = customRowData[row.id];
        if (!full) return;   // drawn when its data arrives (rowLoaded)
        // The same window as every other review chart (Settings, wbrWindow).
        const d = wbrSliceData(row, full);
        if (charts[item.id]) { charts[item.id].destroy(); delete charts[item.id]; }
        charts[item.id] = buildRowCharts(row, d, [{ k: item.period, canvasId: 'wbrc-' + item.id, existing: null,
                                                    first: item.period === 'd' ? 0 : wbrStart(row.id, item.period) }])[0];
        updateWbrSliders();
    }

    function stripHtml() {
        const slides = getDeck().slides;
        const items = slides.map((s, i) => `
            <div class="wbr-strip-slide ${i === current ? 'active' : ''}" onclick="WbrDeck.show(${i})">
                <span class="wbr-strip-num">${i + 1}</span>
                <span class="wbr-strip-title">${escHtml(s.title || 'Untitled Slide')}</span>
                ${editing() ? `<span class="wbr-strip-btns">
                    <button type="button" class="chart-title-btn" title="Move Up" onclick="event.stopPropagation(); WbrDeck.moveSlide(${i}, -1)" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="chart-title-btn" title="Move Down" onclick="event.stopPropagation(); WbrDeck.moveSlide(${i}, 1)" ${i === slides.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="chart-title-btn" title="Delete Slide" onclick="event.stopPropagation(); WbrDeck.deleteSlide(${i})" ${slides.length === 1 ? 'disabled' : ''}><i class="fas fa-trash"></i></button>
                </span>` : ''}
            </div>`).join('');
        const add = editing() ? `<button type="button" class="btn btn-edit wbr-strip-add" onclick="WbrDeck.addSlide()"><i class="fas fa-plus"></i> Add Slide</button>` : '';
        return items + add;
    }

    function toolsHtml() {
        const s = slide();
        const n = getDeck().slides.length;
        const nav = `
            <button type="button" class="chart-title-btn" onclick="WbrDeck.show(${current - 1})" ${current === 0 ? 'disabled' : ''}><i class="fas fa-chevron-left"></i> Previous</button>
            <span class="wbr-deck-count">Slide ${current + 1} Of ${n}</span>
            <button type="button" class="chart-title-btn" onclick="WbrDeck.show(${current + 1})" ${current === n - 1 ? 'disabled' : ''}>Next <i class="fas fa-chevron-right"></i></button>`;
        if (!editing()) return nav;
        const item = selected && findItem(selected);
        let itemTools = '<span class="text-muted">Click a chart\'s title bar to pick it, drag it to move, drag its corner to resize.</span>';
        if (item) {
            itemTools = wbrTitleBoxes(item, `WbrDeck.setItemText('${item.id}', KEY, this.value)`)
                + `<button type="button" class="chart-title-btn" onclick="WbrDeck.removeItem('${item.id}')"><i class="fas fa-times"></i> Remove From Slide</button>`;
        }
        return `<div class="wbr-deck-tools-row">${nav}
                    <label class="wbr-count">Title <input type="text" class="wbr-title-input" value="${escHtml(s.title || '')}" onchange="WbrDeck.setText('title', this.value)"></label>
                    <label class="wbr-count">Subtitle <input type="text" class="wbr-title-input" value="${escHtml(s.subtitle || '')}" onchange="WbrDeck.setText('subtitle', this.value)"></label>
                    <button type="button" class="chart-title-btn" onclick="openWbrPicker()"><i class="fas fa-plus"></i> Add Chart To Slide</button>
                </div>
                <div class="wbr-deck-tools-row">${itemTools}</div>`;
    }

    function slideHtml() {
        const s = slide();
        const c = cols(), r = rows();
        const pct = (v, of) => (Math.min(v, of) / of * 100) + '%';
        const items = s.items.map(item => {
            const row = findRow(item.ref);
            const title = row ? wbrItemTitle(item, row) : 'Removed Chart';
            const subtitle = row ? wbrItemSubtitle(item, row) : '';
            return `
                <div class="wbr-slide-item ${item.id === selected ? 'selected' : ''}" data-slide-item="${item.id}"
                     style="left:${pct(item.x, c)};top:${pct(item.y, r)};width:${pct(item.w, c)};height:${pct(item.h, r)};">
                    <div class="wbr-slide-item-head" data-slide-drag="${item.id}">
                        <span class="wbr-slide-item-title">${escHtml(title)}</span>
                        <span class="wbr-slide-item-period">${escHtml(PERIOD_NAMES[item.period] || '')}</span>
                        ${item.period === 'd' ? '' : wbrScrollerHtml(item.period)}
                        ${typeof WbrComments !== 'undefined' ? WbrComments.slotHtml(`chart:${item.ref}|${item.period}`) : ''}
                    </div>
                    ${subtitle ? `<div class="wbr-slide-item-subtitle">${escHtml(subtitle)}</div>` : ''}
                    <div class="wbr-slide-plot"><canvas id="wbrc-${item.id}"></canvas></div>
                    ${editing() ? `<div class="wbr-slide-resize" data-slide-resize="${item.id}"></div>` : ''}
                </div>`;
        }).join('');
        return `
            <div class="wbr-slide-head">
                <div class="wbr-slide-title">${escHtml(s.title || '')}</div>
                ${s.subtitle ? `<div class="wbr-slide-subtitle">${escHtml(s.subtitle)}</div>` : ''}
            </div>
            <div class="wbr-slide-body ${editing() ? 'editing' : ''} ${editing() && Number(settings.wbr_slide_gridlines) === 1 ? 'gridlines' : ''}" id="wbrSlideBody" style="--wbr-cols:${c};--wbr-rows:${r};">${items}</div>`;
    }

    function render() {
        const box = document.getElementById('wbrDeck');
        if (!box) return;
        const shown = view();
        const on = shown === 'slides';
        const viewSwitch = document.getElementById('wbrViewSwitch');
        if (viewSwitch) {
            viewSwitch.style.display = '';
            viewSwitch.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === shown));
        }
        document.getElementById('wbrGrid').style.display = shown === 'grid' ? '' : 'none';
        if (shown !== 'grid') document.getElementById('wbrEmpty').style.display = 'none';
        box.style.display = on ? '' : 'none';
        if (typeof WbrReport !== 'undefined') WbrReport.setVisible(shown === 'report');
        destroyCharts();
        if (!on) return;
        const root = document.documentElement.style;
        root.setProperty('--wbr-slide-border', `${Number(settings.wbr_slide_border_px) || 0}px solid ${settings.wbr_slide_border_colour || '#cbd5e1'}`);
        root.setProperty('--wbr-slide-title', (Number(settings.wbr_slide_title_px) || 28) + 'px');
        root.setProperty('--wbr-slide-subtitle', (Number(settings.wbr_slide_subtitle_px) || 16) + 'px');
        document.getElementById('wbrDeckStrip').innerHTML = stripHtml();
        document.getElementById('wbrDeckTools').innerHTML = toolsHtml();
        document.getElementById('wbrSlide').innerHTML = slideHtml();
        slide().items.forEach(drawItem);
    }

    // A KPI row's data has arrived or changed: redraw its items on this slide.
    function rowLoaded(row) {
        if (!active()) return;
        slide().items.filter(i => i.ref === row.id).forEach(drawItem);
    }

    // ── Changes (admin) ──
    function change(fn) {
        fn(getDeck());
        render();
        save();
    }

    function addChart(ref, period) {
        const row = findRow(ref);
        if (!row) return;
        const per = Math.max(1, Number(settings.wbr_columns) || 3);
        const w = round2(cols() / per), h = round2(rows() / 2);
        const s = slide();
        // Below the lowest item, or at the top if there is no room left.
        const bottom = s.items.reduce((m, i) => Math.max(m, i.y + i.h), 0);
        const y = bottom + h <= rows() ? bottom : 0;
        const item = { id: newId('i'), kind: 'chart', ref, period, x: 0, y: round2(y), w, h };
        selected = item.id;
        change(() => { s.items.push(item); });
        if (!customRowData[ref]) { renderLayout(); loadCustomRow(row); }
        closeWbrPicker();
    }

    function removeItem(id) {
        selected = null;
        change(() => { const s = slide(); s.items = s.items.filter(i => i.id !== id); });
    }

    // A chart's own title / subtitle on this slide; blank = the KPI chart's.
    function setItemText(id, key, value) {
        const text = String(value || '').trim().slice(0, key === 'title' ? 200 : 300);
        change(() => {
            const item = findItem(id);
            if (!item) return;
            if (text) item[key] = text;
            else delete item[key];
        });
    }

    function setText(key, value) {
        change(() => { slide()[key] = String(value).slice(0, key === 'title' ? 200 : 300); });
    }

    function addSlide() {
        change(d => {
            d.slides.splice(current + 1, 0, { id: newId('s'), title: 'New Slide', subtitle: '', items: [] });
            current += 1;
        });
    }

    function moveSlide(i, step) {
        change(d => {
            const j = i + step;
            if (j < 0 || j >= d.slides.length) return;
            [d.slides[i], d.slides[j]] = [d.slides[j], d.slides[i]];
            if (current === i) current = j;
            else if (current === j) current = i;
        });
    }

    function deleteSlide(i) {
        const d = getDeck();
        if (d.slides.length < 2) return;
        if (!confirm(`Delete slide ${i + 1} (${d.slides[i].title || 'Untitled Slide'})?`)) return;
        change(dk => { dk.slides.splice(i, 1); if (current >= i && current > 0) current -= 1; });
    }

    function show(i) {
        selected = null;
        current = i;
        render();
    }

    function setView(name) {
        try { localStorage.setItem(VIEW_KEY, name); } catch (e) {}
        render();
        // Charts drawn while the grid was hidden need their size again.
        setTimeout(resizeAllCharts, 10);
    }

    // ── Moving and resizing (admin) ──
    let drag = null;

    document.addEventListener('pointerdown', event => {
        if (!editing() || !active()) return;
        // The month / week scroller sits in the title bar; using it never moves the chart.
        if (event.target.closest('[data-wbr-scroller], .wbr-comment-slot')) return;
        const handle = event.target.closest('[data-slide-drag], [data-slide-resize]');
        if (!handle) return;
        const id = handle.dataset.slideDrag || handle.dataset.slideResize;
        const item = findItem(id);
        const body = document.getElementById('wbrSlideBody');
        if (!item || !body) return;
        if (selected !== id) {
            selected = id;
            document.getElementById('wbrDeckTools').innerHTML = toolsHtml();
            document.querySelectorAll('[data-slide-item]').forEach(el => el.classList.toggle('selected', el.dataset.slideItem === id));
        }
        const rect = body.getBoundingClientRect();
        drag = { id, resize: !!handle.dataset.slideResize, x0: event.clientX, y0: event.clientY,
                 start: { x: item.x, y: item.y, w: item.w, h: item.h },
                 unitX: rect.width / cols(), unitY: rect.height / rows(), moved: false };
        handle.setPointerCapture(event.pointerId);
        event.preventDefault();
    });

    document.addEventListener('pointermove', event => {
        if (!drag) return;
        const item = findItem(drag.id);
        if (!item) return;
        const fit = v => (snapOn() ? Math.round(v) : Math.round(v * 10) / 10);
        const dx = (event.clientX - drag.x0) / drag.unitX, dy = (event.clientY - drag.y0) / drag.unitY;
        const min = snapOn() ? 1 : 0.5;
        const s = drag.start;
        if (drag.resize) {
            item.w = Math.min(Math.max(fit(s.w + dx), min), cols() - item.x);
            item.h = Math.min(Math.max(fit(s.h + dy), min), rows() - item.y);
        } else {
            item.x = Math.min(Math.max(fit(s.x + dx), 0), Math.max(0, cols() - item.w));
            item.y = Math.min(Math.max(fit(s.y + dy), 0), Math.max(0, rows() - item.h));
        }
        drag.moved = drag.moved || item.x !== s.x || item.y !== s.y || item.w !== s.w || item.h !== s.h;
        const el = document.querySelector(`[data-slide-item="${item.id}"]`);
        if (el) {
            el.style.left = (item.x / cols() * 100) + '%';
            el.style.top = (item.y / rows() * 100) + '%';
            el.style.width = (item.w / cols() * 100) + '%';
            el.style.height = (item.h / rows() * 100) + '%';
        }
    });

    function endDrag() {
        if (!drag) return;
        const moved = drag.moved;
        drag = null;
        if (moved) save();
    }
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);

    // Left / right arrow keys move between slides (not while typing).
    document.addEventListener('keydown', event => {
        if (!document.getElementById('wbrDeck') || !active()) return;
        if (event.target.closest('input, textarea, select, [contenteditable]')) return;
        if (document.querySelector('.modal-overlay.open')) return;
        if (event.key === 'ArrowLeft' && current > 0) show(current - 1);
        else if (event.key === 'ArrowRight' && current < getDeck().slides.length - 1) show(current + 1);
    });

    return {
        active, setSaved, refs, render, rowLoaded, addChart, removeItem, setText, setItemText,
        addSlide, moveSlide, deleteSlide, show, setView,
        charts: () => Object.values(charts),
    };
})();
