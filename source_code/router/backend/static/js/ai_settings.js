// Settings > AI: per-page Ask AI settings and the Knowledge entries.
// Talks to routers/ai_knowledge.py. All entry text is written with
// textContent, never innerHTML, so nothing typed into an entry can run.
(function () {
    const root = document.getElementById('ai-settings');
    if (!root) return;
    const BASE = root.dataset.base;
    const $ = id => document.getElementById(id);

    let entries = [], kinds = [], pages = [], usage = {};
    let editingId = null;
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

    function fillSelect(sel, options, value) {
        sel.innerHTML = '';
        options.forEach(([v, label]) => {
            const o = document.createElement('option');
            o.value = v; o.textContent = label; o.selected = v === value;
            sel.appendChild(o);
        });
    }

    const label = (list, key) => (list.find(p => p[0] === key) || [key, key])[1];

    // ── Page settings ───────────────────────────────────────────────────────
    async function loadPage() {
        const page = $('ai-page').value;
        $('ai-preview').style.display = 'none';
        $('ai-page-note').textContent = '';
        try {
            const data = await api('/settings/ai/page/' + page);
            showPage(data.settings);
        } catch (e) {
            $('ai-page-note').textContent = e.message;
        }
    }

    function showPage(s) {
        $('ai-instructions').value = s.instructions;
        $('ai-max-tokens').value = s.max_tokens;
        $('ai-temperature').value = s.temperature;
        $('ai-history-turns').value = s.history_turns;
        $('ai-knowledge-max').value = s.knowledge_max_chars;
    }

    $('ai-page-save').addEventListener('click', async () => {
        try {
            const data = await api('/settings/ai/page/' + $('ai-page').value, {
                instructions: $('ai-instructions').value,
                max_tokens: $('ai-max-tokens').value,
                temperature: $('ai-temperature').value,
                history_turns: $('ai-history-turns').value,
                knowledge_max_chars: $('ai-knowledge-max').value,
            });
            showPage(data.settings);
            $('ai-page-note').textContent = 'Saved.';
            loadEntries();
        } catch (e) {
            $('ai-page-note').textContent = e.message;
        }
    });

    $('ai-page-reset').addEventListener('click', async () => {
        if (!confirm('Put this page\'s instructions and limits back to the defaults?')) return;
        try {
            const data = await api('/settings/ai/page/' + $('ai-page').value + '/reset', {});
            showPage(data.settings);
            $('ai-page-note').textContent = 'Reset to default.';
            loadEntries();
        } catch (e) {
            $('ai-page-note').textContent = e.message;
        }
    });

    $('ai-preview-btn').addEventListener('click', async () => {
        const box = $('ai-preview');
        if (box.style.display !== 'none') { box.style.display = 'none'; return; }
        box.textContent = 'Loading...';
        box.style.display = '';
        try {
            const d = await api('/settings/ai/preview/' + $('ai-page').value);
            let head = `${d.chars.toLocaleString()} characters sent before the question. ` +
                `Knowledge: ${d.knowledge_chars.toLocaleString()} of ${d.knowledge_max_chars.toLocaleString()} allowed.`;
            if (d.skipped.length) head += '\nLeft out, over the size limit: ' + d.skipped.map(s => s.title).join(', ');
            box.textContent = head + '\n\n' + d.prompt;
        } catch (e) {
            box.textContent = e.message;
        }
    });

    // ── Knowledge list ──────────────────────────────────────────────────────
    async function loadEntries() {
        try {
            const data = await api('/settings/ai/knowledge');
            entries = data.entries; kinds = data.kinds; pages = data.pages; usage = data.usage;
            renderUsage();
            renderTable();
        } catch (e) {
            $('ai-usage').textContent = e.message;
        }
    }

    function renderUsage() {
        const box = $('ai-usage');
        box.innerHTML = '';
        Object.keys(usage).forEach(page => {
            const u = usage[page];
            const div = document.createElement('div');
            div.className = 'small' + (u.skipped.length ? ' text-danger fw-semibold' : ' text-muted');
            let text = `${label(pages, page)}: ${u.chars.toLocaleString()} of ${u.max_chars.toLocaleString()} characters of knowledge used.`;
            if (u.skipped.length) text += ' Over the limit, left out: ' + u.skipped.map(s => s.title).join(', ') +
                '. Raise the limit in Page Settings or switch entries off.';
            div.textContent = text;
            box.appendChild(div);
        });
    }

    function renderTable() {
        const q = $('ai-search').value.trim().toLowerCase();
        const fPage = $('ai-filter-page').value;
        const fStatus = $('ai-filter-status').value;
        const tbody = $('ai-table').querySelector('tbody');
        tbody.innerHTML = '';
        const rows = entries.filter(e =>
            (!q || e.title.toLowerCase().includes(q) || e.content.toLowerCase().includes(q)) &&
            (!fPage || e.scope === fPage) && (!fStatus || e.status === fStatus));
        rows.forEach(e => {
            const tr = document.createElement('tr');
            if (!e.enabled) tr.className = 'text-muted';
            const cell = text => { const td = document.createElement('td'); td.textContent = text; tr.appendChild(td); return td; };
            cell(e.title);
            cell(label(kinds, e.kind));
            cell(label(pages, e.scope));
            const st = cell(e.status === 'proposed' ? 'Proposed' : 'Active');
            if (e.status === 'proposed') st.className = 'text-warning fw-semibold';
            cell(e.enabled ? 'On' : 'Off');
            cell(e.content.length.toLocaleString()).className = 'text-end';
            cell((e.updated_at || '').slice(0, 16).replace('T', ' ') + (e.updated_by ? ' ' + e.updated_by : ''));
            const actions = document.createElement('td');
            actions.className = 'text-nowrap text-end';
            const btn = (text, cls, fn) => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-sm ' + cls + ' ms-1'; b.textContent = text;
                b.addEventListener('click', fn);
                actions.appendChild(b);
            };
            if (e.status === 'proposed') btn('Approve', 'btn-success', () => setStatus(e.id, 'active'));
            btn('Edit', 'btn-outline-primary', () => openEditor(e));
            btn(e.enabled ? 'Switch Off' : 'Switch On', 'btn-outline-secondary',
                () => saveEntry({...e, enabled: !e.enabled}).catch(err => alert(err.message)));
            btn('Delete', 'btn-outline-danger', () => deleteEntry(e));
            tr.appendChild(actions);
            tbody.appendChild(tr);
        });
        if (!rows.length) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 8; td.className = 'text-muted'; td.textContent = 'No entries.';
            tr.appendChild(td); tbody.appendChild(tr);
        }
    }

    async function setStatus(id, status) {
        try { await api('/settings/ai/knowledge/status', {id, status}); loadEntries(); }
        catch (e) { alert(e.message); }
    }

    async function saveEntry(entry) {
        await api('/settings/ai/knowledge/save', entry);
        loadEntries();
    }

    async function deleteEntry(e) {
        if (!confirm(`Delete "${e.title}"? This cannot be undone.`)) return;
        try { await api('/settings/ai/knowledge/delete', {id: e.id}); loadEntries(); }
        catch (err) { alert(err.message); }
    }

    // ── Editor ──────────────────────────────────────────────────────────────
    function openEditor(e) {
        $('ai-draft-box').style.display = 'none';
        editingId = e ? e.id : null;
        fillSelect($('ai-e-kind'), kinds, e ? e.kind : 'document');
        fillSelect($('ai-e-scope'), pages, e ? e.scope : 'all');
        $('ai-e-title').value = e ? e.title : '';
        $('ai-e-status').value = e ? e.status : 'active';
        $('ai-e-enabled').checked = e ? e.enabled : true;
        $('ai-e-content').value = e ? e.content : '';
        $('ai-e-note').textContent = '';
        showColumnPick();
        $('ai-editor').style.display = '';
        $('ai-e-title').focus();
    }

    // A Column Note's title is SCHEMA.TABLE.COLUMN, picked from the live tables.
    async function showColumnPick() {
        const isColumn = $('ai-e-kind').value === 'column';
        $('ai-e-column-pick').style.display = isColumn ? '' : 'none';
        $('ai-e-title').readOnly = isColumn;
        if (!isColumn) return;
        const [table, column] = splitTitle($('ai-e-title').value);
        await loadTables($('ai-e-table'), table);
        await loadColumns(column);
    }

    function splitTitle(title) {
        const parts = (title || '').split('.');
        return parts.length === 3 ? [parts[0] + '.' + parts[1], parts[2]] : ['', ''];
    }

    async function loadTables(sel, value) {
        if (!tablesCache) {
            fillSelect(sel, [['', 'Loading tables...']], '');
            try { tablesCache = (await api('/settings/ai/tables')).tables; }
            catch (e) { fillSelect(sel, [['', e.message]], ''); return; }
        }
        fillSelect(sel, [['', 'Choose a table']].concat(tablesCache.map(t => [t, t])), value || '');
    }

    async function loadColumns(value) {
        const table = $('ai-e-table').value;
        const sel = $('ai-e-column');
        if (!table) { fillSelect(sel, [['', 'Choose a table first']], ''); return; }
        fillSelect(sel, [['', 'Loading columns...']], '');
        try {
            const cols = (await api('/settings/ai/columns?table=' + encodeURIComponent(table))).columns;
            fillSelect(sel, [['', 'Choose a column']].concat(Object.keys(cols).map(c => [c, `${c} (${cols[c]})`])), value || '');
        } catch (e) {
            fillSelect(sel, [['', e.message]], '');
        }
        setColumnTitle();
    }

    function setColumnTitle() {
        const t = $('ai-e-table').value, c = $('ai-e-column').value;
        if (t && c) $('ai-e-title').value = t + '.' + c;
    }

    $('ai-e-kind').addEventListener('change', showColumnPick);
    $('ai-e-table').addEventListener('change', () => loadColumns(''));
    $('ai-e-column').addEventListener('change', setColumnTitle);

    $('ai-e-save').addEventListener('click', async () => {
        const entry = {
            title: $('ai-e-title').value, kind: $('ai-e-kind').value, scope: $('ai-e-scope').value,
            status: $('ai-e-status').value, enabled: $('ai-e-enabled').checked, content: $('ai-e-content').value,
        };
        if (editingId) entry.id = editingId;
        try {
            await saveEntry(entry);
            $('ai-editor').style.display = 'none';
        } catch (e) {
            $('ai-e-note').textContent = e.message;
        }
    });

    $('ai-e-cancel').addEventListener('click', () => { $('ai-editor').style.display = 'none'; });
    $('ai-add').addEventListener('click', () => openEditor(null));
    $('ai-add-column').addEventListener('click', () => {
        openEditor(null);
        $('ai-e-kind').value = 'column';
        showColumnPick();
    });

    // ── Draft With AI and starting entries ──────────────────────────────────
    $('ai-draft').addEventListener('click', async () => {
        $('ai-editor').style.display = 'none';
        $('ai-draft-box').style.display = '';
        await loadTables($('ai-draft-table'), '');
    });
    $('ai-draft-cancel').addEventListener('click', () => { $('ai-draft-box').style.display = 'none'; });
    $('ai-draft-go').addEventListener('click', async () => {
        const table = $('ai-draft-table').value;
        if (!table) return;
        const btn = $('ai-draft-go'), note = $('ai-draft-note');
        btn.disabled = true;
        note.textContent = 'Cortex is drafting a description of ' + table + '...';
        try {
            const data = await api('/settings/ai/knowledge/draft', {table});
            $('ai-draft-box').style.display = 'none';
            await loadEntries();
            openEditor(data.entry);
        } catch (e) {
            note.textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    });

    $('ai-seed').addEventListener('click', async () => {
        try {
            const data = await api('/settings/ai/knowledge/starting-entries', {});
            alert(data.added ? `${data.added} starting entries added as Proposed.` : 'All starting entries are already there.');
            loadEntries();
        } catch (e) { alert(e.message); }
    });

    ['ai-search', 'ai-filter-page', 'ai-filter-status'].forEach(id =>
        $(id).addEventListener('input', renderTable));

    // ── Start ───────────────────────────────────────────────────────────────
    (async function start() {
        await loadEntries();
        fillSelect($('ai-page'), pages.filter(p => p[0] !== 'all'), 'kpi');
        fillSelect($('ai-filter-page'), [['', 'Any Page']].concat(pages), '');
        $('ai-page').addEventListener('change', loadPage);
        loadPage();
    })();
})();
