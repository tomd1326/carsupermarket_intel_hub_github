// Semantic & Context Layer page: the entries every Ask AI reads, for people
// to read too. Everyone with the page reads and downloads; the admin adds,
// edits, approves and deletes entries and sets up the page (Page Settings).
// Talks to routers/semantic_layer.py. All entry text is written with
// textContent, never innerHTML, so nothing typed into an entry can run.
(function () {
    const root = document.getElementById('semantic-layer');
    if (!root) return;
    const BASE = root.dataset.base;
    const $ = id => document.getElementById(id);

    let entries = [], settings = null, kinds = [], layers = [], isAdmin = false;
    let pages = [], fieldTypes = [], usage = {};
    let layerTab = '';
    let editing = null;
    let editorValues = {};
    let tablesCache = null;

    async function api(path, body) {
        const opts = body === undefined ? {} : {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
        const resp = await fetch(BASE + path, opts);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || data.message || 'Request failed');
        if (data.status === 'error') throw new Error(data.message);
        return data;
    }

    // While a save runs the popup is greyed out and locked and the button says
    // what is happening (same as whileSaving in trade_report.js).
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

    function fillSelect(sel, options, value) {
        sel.innerHTML = '';
        options.forEach(([v, label]) => {
            const o = document.createElement('option');
            o.value = v; o.textContent = label; o.selected = v === value;
            sel.appendChild(o);
        });
    }

    const kindLabel = k => (settings.kind_labels[k]) || (kinds.find(x => x[0] === k) || [k, k])[1];
    const layerOf = k => settings.kind_layers[k] || 'context';
    const layerLabel = l => (settings.layer_labels[l]) || (layers.find(x => x[0] === l) || [l, l])[1];
    const fieldApplies = (f, kind) => !f.kinds.length || f.kinds.includes(kind);
    const NO_CATEGORY = '__none__';

    // ── Load ────────────────────────────────────────────────────────────────
    async function load() {
        try {
            const data = await api('/semantic-layer/data');
            entries = data.entries; settings = data.settings; kinds = data.kinds; layers = data.layers;
            isAdmin = data.is_admin;
            if (isAdmin) { pages = data.pages; fieldTypes = data.field_types; usage = data.usage; }
            applyAppearance();
            render();
        } catch (e) {
            $('sl-list').innerHTML = '';
            $('sl-list').appendChild(el('div', 'sl-empty', 'Could not load: ' + e.message));
        }
    }

    function applyAppearance() {
        root.style.setProperty('--sl-title-size', settings.title_font_px + 'px');
        root.style.setProperty('--sl-text-size', settings.text_font_px + 'px');
        root.style.setProperty('--sl-badge-size', settings.badge_font_px + 'px');
        $('sl-intro').textContent = settings.intro;
    }

    // ── Filters ─────────────────────────────────────────────────────────────
    function kindsInUse() {
        const shown = isAdmin ? kinds.map(k => k[0]) : settings.kinds_shown;
        return shown.filter(k => !layerTab || layerOf(k) === layerTab);
    }

    function categoryList() {
        const list = settings.categories.slice();
        entries.forEach(e => {
            if (e.category && !list.some(c => c.toLowerCase() === e.category.toLowerCase())) list.push(e.category);
        });
        return list;
    }

    function renderTabs() {
        const box = $('sl-tabs');
        box.innerHTML = '';
        const tabs = [['', 'All']].concat(layers.map(([l]) => [l, layerLabel(l)]));
        tabs.forEach(([key, label]) => {
            const n = entries.filter(e => !key || layerOf(e.kind) === key).length;
            const b = el('button', key === layerTab ? 'active' : '', `${label} (${n})`);
            b.type = 'button';
            b.addEventListener('click', () => { layerTab = key; render(); });
            box.appendChild(b);
        });
    }

    function renderFilters() {
        const kindSel = $('sl-kind'), catSel = $('sl-category');
        const kindNow = kindSel.value, catNow = catSel.value;
        const usable = kindsInUse();
        fillSelect(kindSel, [['', 'All Kinds']].concat(usable.map(k => [k, kindLabel(k)])),
            usable.includes(kindNow) ? kindNow : '');
        fillSelect(catSel, [['', 'All Categories']].concat(categoryList().map(c => [c, c]), [[NO_CATEGORY, 'No Category']]), catNow);
    }

    function matches(e) {
        if (layerTab && layerOf(e.kind) !== layerTab) return false;
        const kind = $('sl-kind').value, cat = $('sl-category').value;
        if (kind && e.kind !== kind) return false;
        if (cat === NO_CATEGORY ? !!e.category : (cat && (e.category || '').toLowerCase() !== cat.toLowerCase())) return false;
        const status = isAdmin ? $('sl-status').value : '';
        if (status === 'hidden' ? e.show_on_page !== false : (status && e.status !== status)) return false;
        const q = $('sl-search').value.trim().toLowerCase();
        if (!q) return true;
        const hay = [e.title, e.content, e.tags, e.category, kindLabel(e.kind)]
            .concat(Object.values(e.fields || {})).join('\n').toLowerCase();
        return q.split(/\s+/).every(word => hay.includes(word));
    }

    // ── List ────────────────────────────────────────────────────────────────
    function groups(rows) {
        const by = settings.group_by;
        const sorted = rows.slice().sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
        if (by === 'none') return [['', sorted]];
        if (by === 'kind') {
            return kinds.map(([k]) => [kindLabel(k), sorted.filter(e => e.kind === k)]).filter(g => g[1].length);
        }
        const out = categoryList().map(c => [c, sorted.filter(e => (e.category || '').toLowerCase() === c.toLowerCase())]);
        out.push(['No Category', sorted.filter(e => !e.category)]);
        return out.filter(g => g[1].length);
    }

    function render() {
        renderTabs();
        renderFilters();
        renderAdminBar();
        const rows = entries.filter(matches);
        $('sl-count').textContent = `${rows.length} of ${entries.length} entries`;
        const list = $('sl-list'), nav = $('sl-nav');
        list.innerHTML = ''; nav.innerHTML = '';
        if (!rows.length) {
            list.appendChild(el('div', 'sl-empty', entries.length ? 'No entries match.' : 'No entries yet.'));
            return;
        }
        groups(rows).forEach(([title, items]) => {
            if (title) {
                list.appendChild(el('h2', 'sl-group-title', title));
                nav.appendChild(el('div', 'sl-nav-group', title));
            }
            items.forEach(e => {
                list.appendChild(card(e));
                const a = el('a', '', e.title);
                a.href = '#e-' + e.id;
                nav.appendChild(a);
            });
        });
        flashHash();
    }

    function card(e) {
        const c = el('article', 'sl-card');
        c.id = 'e-' + e.id;
        if (e.status === 'proposed') c.classList.add('proposed');
        if (e.show_on_page === false || !settings.kinds_shown.includes(e.kind)) c.classList.add('hidden-entry');

        const head = el('div', 'sl-card-head');
        head.appendChild(el('h3', 'sl-card-title', e.title));
        const layer = layerOf(e.kind);
        head.appendChild(el('span', 'sl-badge layer-' + layer, layerLabel(layer)));
        head.appendChild(el('span', 'sl-badge', kindLabel(e.kind)));
        if (e.category && settings.group_by !== 'category') head.appendChild(el('span', 'sl-badge', e.category));
        if (isAdmin) {
            if (e.status === 'proposed') head.appendChild(el('span', 'sl-badge warn', 'Proposed'));
            if (!e.enabled) head.appendChild(el('span', 'sl-badge muted', 'Not Read By Ask AI'));
            if (e.show_on_page === false) head.appendChild(el('span', 'sl-badge muted', 'Hidden From Page'));
            else if (!settings.kinds_shown.includes(e.kind)) head.appendChild(el('span', 'sl-badge muted', 'Kind Hidden From Others'));
        }
        (e.tags || '').split(',').map(t => t.trim()).filter(Boolean)
            .forEach(t => head.appendChild(el('span', 'sl-tag', t)));
        c.appendChild(head);

        const values = e.fields || {};
        const shown = settings.fields.filter(f => fieldApplies(f, e.kind) && (values[f.key] || '').trim());
        if (shown.length) {
            const grid = el('div', 'sl-fields');
            shown.forEach(f => {
                grid.appendChild(el('div', 'sl-field-label', f.label));
                grid.appendChild(el('div', 'sl-field-value' + (f.type === 'code' ? ' code' : ''), values[f.key].trim()));
            });
            c.appendChild(grid);
        }
        if ((e.content || '').trim()) {
            if (shown.length) c.appendChild(el('div', 'sl-content-label', settings.content_label));
            c.appendChild(el('div', 'sl-content', e.content.trim()));
        }

        const foot = el('div', 'sl-card-foot');
        const when = (e.updated_at || '').slice(0, 10);
        foot.appendChild(el('span', '', `Updated ${when}${e.updated_by ? ' by ' + e.updated_by : ''}`));
        foot.appendChild(el('span', 'spacer'));
        const btn = (text, cls, fn) => {
            const b = el('button', 'btn btn-sm ' + cls, text);
            b.type = 'button';
            b.addEventListener('click', fn);
            foot.appendChild(b);
            return b;
        };
        btn('Copy Link', 'btn-link p-0 me-2', ev => copyLink(e, ev.target));
        if (isAdmin) {
            if (e.status === 'proposed') {
                const b = btn('Approve', 'btn-success', () => runOnButton(b, 'Approving...', async () => {
                    await api('/semantic-layer/status', {id: e.id, status: 'active'});
                    await load();
                }));
            }
            btn('Edit', 'btn-outline-primary', () => openEditor(e));
        }
        c.appendChild(foot);
        return c;
    }

    async function runOnButton(button, busyText, work) {
        try { await whileSaving(button, busyText, work); }
        catch (err) { alert(err.message); }
    }

    function copyLink(e, button) {
        const url = location.origin + location.pathname + '#e-' + e.id;
        navigator.clipboard.writeText(url).then(() => {
            button.textContent = 'Copied';
            setTimeout(() => { button.textContent = 'Copy Link'; }, 1500);
        }).catch(() => prompt('Copy this link:', url));
    }

    function flashHash() {
        const target = location.hash && document.getElementById(location.hash.slice(1));
        if (!target) return;
        target.scrollIntoView({block: 'start'});
        target.classList.add('flash');
        setTimeout(() => target.classList.remove('flash'), 2000);
    }
    window.addEventListener('hashchange', flashHash);

    ['sl-search', 'sl-kind', 'sl-category', 'sl-status'].forEach(id => {
        const box = $(id);
        if (box) box.addEventListener('input', render);
    });

    // ── Admin bar ───────────────────────────────────────────────────────────
    function renderAdminBar() {
        if (!isAdmin) return;
        const proposed = entries.filter(e => e.status === 'proposed').length;
        const approveAll = $('sl-approve-all');
        approveAll.style.display = proposed ? '' : 'none';
        approveAll.textContent = `Approve All Proposed (${proposed})`;
        const box = $('sl-usage');
        box.innerHTML = '';
        Object.keys(usage).forEach(page => {
            const u = usage[page];
            const name = (pages.find(p => p[0] === page) || [page, page])[1];
            let text = `Ask AI on ${name}: ${u.chars.toLocaleString()} of ${u.max_chars.toLocaleString()} characters used.`;
            if (u.skipped.length) text += ' Over the limit, left out: ' + u.skipped.map(s => s.title).join(', ') +
                '. Raise the limit in Settings > AI or switch entries off for Ask AI.';
            box.appendChild(el('div', u.skipped.length ? 'over' : '', text));
        });
    }

    if ($('sl-add')) {
        $('sl-add').addEventListener('click', () => openEditor(null));
        $('sl-approve-all').addEventListener('click', ev => {
            const n = entries.filter(e => e.status === 'proposed').length;
            if (!confirm(`Approve all ${n} proposed entries? Everyone with this page will see them and Ask AI will read them.`)) return;
            runOnButton(ev.target, 'Approving...', async () => { await api('/semantic-layer/approve-all', {}); await load(); });
        });
        $('sl-seed').addEventListener('click', ev => runOnButton(ev.target, 'Adding...', async () => {
            const data = await api('/semantic-layer/starting-entries', {});
            await load();
            alert(data.added ? `${data.added} starting entries added as Proposed.` : 'All starting entries are already there.');
        }));
        $('sl-settings-btn').addEventListener('click', openSettings);
    }

    // ── Editor ──────────────────────────────────────────────────────────────
    function openModal(id) { $(id).classList.add('open'); }
    function closeModal(id) { $(id).classList.remove('open'); }

    function fillKindSelect(sel, value) {
        sel.innerHTML = '';
        layers.forEach(([l]) => {
            const group = document.createElement('optgroup');
            group.label = layerLabel(l);
            kinds.filter(([k]) => layerOf(k) === l).forEach(([k]) => {
                const o = document.createElement('option');
                o.value = k; o.textContent = kindLabel(k); o.selected = k === value;
                group.appendChild(o);
            });
            if (group.children.length) sel.appendChild(group);
        });
    }

    function openEditor(e) {
        editing = e;
        editorValues = Object.assign({}, e ? e.fields : {});
        $('sl-e-heading').textContent = e ? 'Edit Entry' : 'Add Entry';
        const firstKind = $('sl-kind').value || (layerTab ? kinds.map(k => k[0]).find(k => layerOf(k) === layerTab) : 'metric');
        fillKindSelect($('sl-e-kind'), e ? e.kind : firstKind);
        const dl = $('sl-e-category-list');
        dl.innerHTML = '';
        categoryList().forEach(c => { const o = document.createElement('option'); o.value = c; dl.appendChild(o); });
        $('sl-e-category').value = e ? (e.category || '') : ($('sl-category').value && $('sl-category').value !== NO_CATEGORY ? $('sl-category').value : '');
        $('sl-e-title').value = e ? e.title : '';
        $('sl-e-tags').value = e ? (e.tags || '') : '';
        $('sl-e-content').value = e ? (e.content || '') : '';
        $('sl-e-content-label').textContent = settings.content_label;
        $('sl-e-status').value = e ? e.status : 'active';
        fillSelect($('sl-e-scope'), pages, e ? e.scope : 'all');
        $('sl-e-enabled').checked = e ? e.enabled : true;
        $('sl-e-show').checked = e ? e.show_on_page !== false : true;
        $('sl-e-delete').style.display = e ? '' : 'none';
        $('sl-e-note').textContent = '';
        kindChanged();
        openModal('sl-editor-modal');
        $('sl-e-title').focus();
    }

    function keepFieldValues() {
        $('sl-e-fields').querySelectorAll('[data-key]').forEach(box => { editorValues[box.dataset.key] = box.value; });
    }

    function renderEditorFields() {
        const kind = $('sl-e-kind').value;
        const box = $('sl-e-fields');
        box.innerHTML = '';
        settings.fields.filter(f => fieldApplies(f, kind)).forEach(f => {
            const wrap = el('div', 'modal-field');
            const id = 'sl-e-f-' + f.key;
            const label = el('label', '', f.label);
            label.htmlFor = id;
            wrap.appendChild(label);
            let input;
            if (f.type === 'text') {
                input = document.createElement('input');
                input.type = 'text';
            } else {
                input = document.createElement('textarea');
                input.rows = f.type === 'code' ? 4 : 3;
                if (f.type === 'code') input.className = 'code';
            }
            input.id = id;
            input.dataset.key = f.key;
            input.value = editorValues[f.key] || '';
            wrap.appendChild(input);
            box.appendChild(wrap);
        });
    }

    function kindChanged() {
        keepFieldValues();
        renderEditorFields();
        const kind = $('sl-e-kind').value;
        const pick = kind === 'table' || kind === 'column';
        $('sl-e-pick').style.display = pick ? '' : 'none';
        $('sl-e-column-wrap').style.display = kind === 'column' ? '' : 'none';
        $('sl-e-title-help').textContent = kind === 'column' ? 'SCHEMA.TABLE.COLUMN - pick it above or type it.'
            : kind === 'table' ? 'SCHEMA.TABLE - pick it above or type it.' : '';
        if (pick) loadTables();
    }

    async function loadTables() {
        const sel = $('sl-e-table');
        const parts = $('sl-e-title').value.split('.');
        const current = parts.length >= 2 ? parts[0] + '.' + parts[1] : '';
        if (!tablesCache) {
            fillSelect(sel, [['', 'Loading tables...']], '');
            try { tablesCache = (await api('/semantic-layer/tables')).tables; }
            catch (e) { fillSelect(sel, [['', e.message]], ''); return; }
        }
        fillSelect(sel, [['', 'Choose a table']].concat(tablesCache.map(t => [t, t])), current);
        if ($('sl-e-kind').value === 'column') loadColumns(parts.length === 3 ? parts[2] : '');
    }

    async function loadColumns(value) {
        const table = $('sl-e-table').value, sel = $('sl-e-column');
        if (!table) { fillSelect(sel, [['', 'Choose a table first']], ''); return; }
        fillSelect(sel, [['', 'Loading columns...']], '');
        try {
            const cols = (await api('/semantic-layer/columns?table=' + encodeURIComponent(table))).columns;
            fillSelect(sel, [['', 'Choose a column']].concat(Object.keys(cols).map(c => [c, `${c} (${cols[c]})`])), value || '');
        } catch (e) {
            fillSelect(sel, [['', e.message]], '');
        }
    }

    function pickedTitle() {
        const kind = $('sl-e-kind').value, table = $('sl-e-table').value, column = $('sl-e-column').value;
        if (kind === 'table' && table) $('sl-e-title').value = table;
        if (kind === 'column' && table && column) $('sl-e-title').value = table + '.' + column;
    }

    if ($('sl-editor-modal')) {
        $('sl-e-kind').addEventListener('change', kindChanged);
        $('sl-e-table').addEventListener('change', () => {
            pickedTitle();
            if ($('sl-e-kind').value === 'column') loadColumns('');
        });
        $('sl-e-column').addEventListener('change', pickedTitle);
        $('sl-e-cancel').addEventListener('click', () => closeModal('sl-editor-modal'));
        $('sl-e-save').addEventListener('click', async ev => {
            keepFieldValues();
            const kind = $('sl-e-kind').value;
            const fields = {};
            settings.fields.forEach(f => {
                const v = (editorValues[f.key] || '').trim();
                if (v && fieldApplies(f, kind)) fields[f.key] = v;
            });
            // Values typed under a field that has since been removed are kept.
            Object.keys(editing ? editing.fields : {}).forEach(k => {
                if (!settings.fields.some(f => f.key === k)) fields[k] = editing.fields[k];
            });
            const entry = {
                title: $('sl-e-title').value, kind, category: $('sl-e-category').value.trim(),
                tags: $('sl-e-tags').value, fields, content: $('sl-e-content').value,
                status: $('sl-e-status').value, scope: $('sl-e-scope').value,
                enabled: $('sl-e-enabled').checked, show_on_page: $('sl-e-show').checked,
            };
            if (editing) entry.id = editing.id;
            $('sl-e-note').textContent = '';
            try {
                await whileSaving(ev.target, 'Saving...', async () => {
                    const data = await api('/semantic-layer/save', entry);
                    closeModal('sl-editor-modal');
                    await load();
                    location.hash = 'e-' + data.entry.id;
                });
            } catch (e) {
                $('sl-e-note').textContent = e.message;
            }
        });
        $('sl-e-delete').addEventListener('click', async ev => {
            if (!editing || !confirm(`Delete "${editing.title}"? This cannot be undone.`)) return;
            try {
                await whileSaving(ev.target, 'Removing...', async () => {
                    await api('/semantic-layer/delete', {id: editing.id});
                    closeModal('sl-editor-modal');
                    await load();
                });
            } catch (e) {
                $('sl-e-note').textContent = e.message;
            }
        });
    }

    // ── Page settings ───────────────────────────────────────────────────────
    let draft = null;

    function openSettings() {
        draft = JSON.parse(JSON.stringify(settings));
        draft.categories = draft.categories.map(c => ({name: c, original: c}));
        showSettings();
        $('sl-s-note').textContent = '';
        openModal('sl-settings-modal');
    }

    function showSettings() {
        $('sl-s-intro').value = draft.intro;
        $('sl-s-content-label').value = draft.content_label;
        $('sl-s-group-by').value = draft.group_by;
        $('sl-s-export-name').value = draft.export_name;
        $('sl-s-title-size').value = draft.title_font_px;
        $('sl-s-text-size').value = draft.text_font_px;
        $('sl-s-badge-size').value = draft.badge_font_px;
        renderLayerSettings();
        renderKindSettings();
        renderCategorySettings();
        renderFieldSettings();
    }

    function textInput(value, onInput, maxLength) {
        const i = document.createElement('input');
        i.type = 'text'; i.value = value || ''; if (maxLength) i.maxLength = maxLength;
        i.addEventListener('input', () => onInput(i.value));
        return i;
    }

    function renderLayerSettings() {
        const box = $('sl-s-layers');
        box.innerHTML = '';
        layers.forEach(([l, def]) => {
            const wrap = el('div', 'modal-field');
            wrap.appendChild(el('label', '', def + ' Label'));
            wrap.appendChild(textInput(draft.layer_labels[l] || def, v => { draft.layer_labels[l] = v; }, 60));
            box.appendChild(wrap);
        });
    }

    function renderKindSettings() {
        const body = $('sl-s-kinds').querySelector('tbody');
        body.innerHTML = '';
        kinds.forEach(([k, def]) => {
            const tr = document.createElement('tr');
            tr.appendChild(el('td', 'text-muted', def));
            const labelTd = document.createElement('td');
            labelTd.appendChild(textInput(draft.kind_labels[k] || def, v => { draft.kind_labels[k] = v; }, 60));
            tr.appendChild(labelTd);
            const layerTd = document.createElement('td');
            const sel = document.createElement('select');
            fillSelect(sel, layers.map(([l, d]) => [l, draft.layer_labels[l] || d]), draft.kind_layers[k]);
            sel.addEventListener('change', () => { draft.kind_layers[k] = sel.value; });
            layerTd.appendChild(sel);
            tr.appendChild(layerTd);
            const shownTd = document.createElement('td');
            const cb = document.createElement('input');
            cb.type = 'checkbox'; cb.checked = draft.kinds_shown.includes(k);
            cb.addEventListener('change', () => {
                draft.kinds_shown = draft.kinds_shown.filter(x => x !== k);
                if (cb.checked) draft.kinds_shown.push(k);
            });
            shownTd.appendChild(cb);
            tr.appendChild(shownTd);
            body.appendChild(tr);
        });
    }

    function moveButtons(list, i, rerender) {
        const td = document.createElement('td');
        td.className = 'text-nowrap text-end';
        const b = (text, title, fn, disabled) => {
            const x = el('button', 'btn btn-sm btn-outline-secondary ms-1', text);
            x.type = 'button'; x.title = title; x.disabled = !!disabled;
            x.addEventListener('click', () => { fn(); rerender(); });
            td.appendChild(x);
        };
        b('Up', 'Move up', () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; }, i === 0);
        b('Down', 'Move down', () => { [list[i + 1], list[i]] = [list[i], list[i + 1]]; }, i === list.length - 1);
        b('Remove', 'Remove', () => { list.splice(i, 1); });
        return td;
    }

    function renderCategorySettings() {
        const body = $('sl-s-categories').querySelector('tbody');
        body.innerHTML = '';
        draft.categories.forEach((c, i) => {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.appendChild(textInput(c.name, v => { c.name = v; }, 100));
            tr.appendChild(td);
            tr.appendChild(moveButtons(draft.categories, i, renderCategorySettings));
            body.appendChild(tr);
        });
    }

    function renderFieldSettings() {
        const body = $('sl-s-fields').querySelector('tbody');
        body.innerHTML = '';
        const typeLabels = {text: 'One Line', textarea: 'Several Lines', code: 'Code / SQL'};
        draft.fields.forEach((f, i) => {
            const tr = document.createElement('tr');
            const labelTd = document.createElement('td');
            labelTd.appendChild(textInput(f.label, v => { f.label = v; }, 80));
            tr.appendChild(labelTd);
            const typeTd = document.createElement('td');
            const sel = document.createElement('select');
            fillSelect(sel, fieldTypes.map(t => [t, typeLabels[t] || t]), f.type);
            sel.addEventListener('change', () => { f.type = sel.value; });
            typeTd.appendChild(sel);
            tr.appendChild(typeTd);
            const kindsTd = document.createElement('td');
            const checks = el('div', 'sl-checks');
            kinds.forEach(([k, def]) => {
                const lab = document.createElement('label');
                const cb = document.createElement('input');
                cb.type = 'checkbox'; cb.checked = f.kinds.includes(k);
                cb.addEventListener('change', () => {
                    f.kinds = f.kinds.filter(x => x !== k);
                    if (cb.checked) f.kinds.push(k);
                });
                lab.appendChild(cb);
                lab.appendChild(document.createTextNode(draft.kind_labels[k] || def));
                checks.appendChild(lab);
            });
            kindsTd.appendChild(checks);
            tr.appendChild(kindsTd);
            const aiTd = document.createElement('td');
            const ai = document.createElement('input');
            ai.type = 'checkbox'; ai.checked = f.send_to_ai;
            ai.addEventListener('change', () => { f.send_to_ai = ai.checked; });
            aiTd.appendChild(ai);
            tr.appendChild(aiTd);
            tr.appendChild(moveButtons(draft.fields, i, renderFieldSettings));
            body.appendChild(tr);
        });
    }

    if ($('sl-settings-modal')) {
        $('sl-s-add-category').addEventListener('click', () => {
            draft.categories.push({name: '', original: ''});
            renderCategorySettings();
            const inputs = $('sl-s-categories').querySelectorAll('input');
            if (inputs.length) inputs[inputs.length - 1].focus();
        });
        $('sl-s-add-field').addEventListener('click', () => {
            draft.fields.push({key: '', label: '', type: 'text', kinds: [], send_to_ai: true});
            renderFieldSettings();
            const inputs = $('sl-s-fields').querySelectorAll('input[type="text"]');
            if (inputs.length) inputs[inputs.length - 1].focus();
        });
        $('sl-s-cancel').addEventListener('click', () => closeModal('sl-settings-modal'));
        $('sl-s-save').addEventListener('click', async ev => {
            const renames = {};
            draft.categories.forEach(c => {
                if (c.original && c.name.trim() && c.name.trim() !== c.original) renames[c.original] = c.name.trim();
            });
            const payload = Object.assign({}, draft, {
                intro: $('sl-s-intro').value,
                content_label: $('sl-s-content-label').value,
                group_by: $('sl-s-group-by').value,
                export_name: $('sl-s-export-name').value,
                title_font_px: $('sl-s-title-size').value,
                text_font_px: $('sl-s-text-size').value,
                badge_font_px: $('sl-s-badge-size').value,
                categories: draft.categories.map(c => c.name),
                renames,
            });
            $('sl-s-note').textContent = '';
            try {
                await whileSaving(ev.target, 'Saving...', async () => {
                    await api('/semantic-layer/settings', payload);
                    closeModal('sl-settings-modal');
                    await load();
                });
            } catch (e) {
                $('sl-s-note').textContent = e.message;
            }
        });
        $('sl-s-reset').addEventListener('click', async ev => {
            if (!confirm('Put every page setting back to the default? Categories, fields and labels you have set are lost. Entries are not changed.')) return;
            try {
                await whileSaving(ev.target, 'Saving...', async () => {
                    await api('/semantic-layer/settings/reset', {});
                    closeModal('sl-settings-modal');
                    await load();
                });
            } catch (e) {
                $('sl-s-note').textContent = e.message;
            }
        });
    }

    load();
})();
