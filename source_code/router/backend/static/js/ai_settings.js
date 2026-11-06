// Settings > AI: per-page Ask AI settings, and how much of each page's
// knowledge size limit is used. The knowledge entries themselves are edited on
// the Semantic & Context Layer page (semantic_layer.js). Talks to
// routers/ai_knowledge.py.
(function () {
    const root = document.getElementById('ai-settings');
    if (!root) return;
    const BASE = root.dataset.base;
    const $ = id => document.getElementById(id);

    let pages = [], usage = {};

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
            loadUsage();
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
            loadUsage();
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

    // ── Knowledge size used per page ────────────────────────────────────────
    async function loadUsage() {
        try {
            const data = await api('/settings/ai/usage');
            pages = data.pages; usage = data.usage;
            renderUsage();
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

    // ── Start ───────────────────────────────────────────────────────────────
    (async function start() {
        await loadUsage();
        fillSelect($('ai-page'), pages, 'kpi');
        $('ai-page').addEventListener('change', loadPage);
        loadPage();
    })();
})();
