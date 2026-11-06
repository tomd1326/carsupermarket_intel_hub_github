/* A page's alert (utils/page_alerts.py): the Alerts tab in its Settings popup, and the
   Send Alert popup. The page gives the URL of its alert routes:
     GET  <api>        saved alert and every user
     POST <api>        save    {alert}
     POST <api>/test   send the alert as set up in the tab to the person testing it
     POST <api>/send   send the saved alert to the people ticked */
const PageAlerts = (() => {
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const BLOCK_NAMES = { text: 'Text', table: 'Preview Table', link: 'Link Button' };

    async function postJson(url, body) {
        const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Failed');
        return data;
    }

    async function getJson(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.message || 'Failed');
        return data;
    }

    // Grey out and lock the popup while the server works, with the button saying so.
    async function whileBusy(button, busyText, work) {
        const box = button.closest('.modal-content');
        const text = button.innerHTML;
        box && box.classList.add('busy');
        button.disabled = true;
        button.textContent = busyText;
        try { return await work(); } finally {
            box && box.classList.remove('busy');
            button.disabled = false;
            button.innerHTML = text;
        }
    }

    /* ── Style rows: one per field of STYLE_FIELDS (utils/page_alerts.py) ──
       globals null: the global style, every field set. globals given: a page's own
       style, where a field is only held in values when the page ticks Own for it. */
    class StyleEditor {
        constructor(el, meta, values, globals) {
            this.el = el; this.meta = meta; this.v = { ...values }; this.g = globals || null;
            this.render();
        }

        value(f) { return f.key in this.v ? this.v[f.key] : (this.g ? this.g[f.key] : f.default); }

        values() { return { ...this.v }; }

        control(f, own) {
            const val = this.value(f), off = own ? '' : ' disabled';
            const common = `data-key="${esc(f.key)}"${off}`;
            if (f.kind === 'colour') return `<input type="color" class="pa-colour" ${common} value="${esc(val)}">`;
            if (f.kind === 'size') return `<input type="number" class="form-control form-control-sm" style="width:80px" min="8" max="24" ${common} value="${esc(val)}">`;
            if (f.kind === 'bool') return `<input type="checkbox" class="form-check-input" ${common} ${val ? 'checked' : ''}>`;
            if (f.kind === 'font') return `<select class="form-select form-select-sm" style="width:160px" ${common}>${this.meta.fonts.map(n => `<option ${n === val ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`;
            return `<input type="text" class="form-control form-control-sm" style="max-width:320px" maxlength="200" ${common} value="${esc(val)}">`;
        }

        render() {
            this.el.innerHTML = this.meta.fields.map(f => {
                const own = !this.g || f.key in this.v;
                const tick = this.g ? `<label class="form-check mb-0 small pa-own"><input type="checkbox" class="form-check-input" data-own="${esc(f.key)}" ${own ? 'checked' : ''}> Own</label>` : '';
                return `<div class="pa-style-row"><span class="pa-style-name">${esc(f.label)}</span>${tick}${this.control(f, own)}</div>`;
            }).join('');
            this.el.querySelectorAll('[data-own]').forEach(cb => cb.addEventListener('change', () => {
                const f = this.meta.fields.find(x => x.key === cb.dataset.own);
                if (cb.checked) this.v[f.key] = this.value(f); else delete this.v[f.key];
                this.render();
            }));
            this.el.querySelectorAll('[data-key]').forEach(inp => {
                const f = this.meta.fields.find(x => x.key === inp.dataset.key);
                inp.addEventListener(inp.type === 'checkbox' || inp.tagName === 'SELECT' ? 'change' : 'input', () => {
                    this.v[f.key] = f.kind === 'size' ? (parseInt(inp.value, 10) || 0) : f.kind === 'bool' ? inp.checked : inp.value;
                });
            });
        }
    }

    /* ── Settings > Alerts tab ─────────────────────────────────────────── */
    class Editor {
        constructor(container, api) {
            this.el = container;
            this.api = api;
            this.alert = null;
            this.users = [];
        }

        async load() {
            this.alert = null;
            this.el.innerHTML = '<div class="text-muted small">Loading the alert...</div>';
            try {
                const data = await getJson(this.api);
                this.alert = { users: [...data.alert.users], subject: data.alert.subject, blocks: data.alert.blocks.map(b => ({ ...b })) };
                this.styleMeta = data.style_meta;
                this.globalStyle = data.global_style;
                this.ownStyle = { ...data.alert.style };
                this.users = data.users;
                this.placeholders = data.placeholders;
                this.render();
            } catch (e) {
                this.el.innerHTML = `<div class="text-danger small">Could not load the alert: ${esc(e.message)}</div>`;
            }
        }

        draft() {
            if (!this.alert) return null;
            return {
                users: [...this.alert.users],
                subject: this.alert.subject.trim(),
                style: this.styleEditor ? this.styleEditor.values() : this.ownStyle,
                blocks: this.alert.blocks.map(b => b.type === 'table'
                    ? { type: 'table', max_rows: parseInt(b.max_rows, 10) || 0 }
                    : { type: b.type, text: (b.text || '').trim() }),
            };
        }

        // Saves only once loaded, so a failed load never overwrites the saved alert.
        async save() {
            const draft = this.draft();
            if (draft) await postJson(this.api, { alert: draft });
        }

        render() {
            const a = this.alert;
            const tags = this.placeholders.map(p => `<code>{${esc(p)}}</code>`).join(' ');
            this.el.innerHTML = `
                <p class="text-muted small mb-2">Send Alert on the page emails this to the people ticked below,
                    using the data on the page at the time. Send Test To Me sends it as set up here, saved or not, to you only.</p>
                <div class="fw-semibold small mb-1">Send To</div>
                <div class="pa-users mb-3">${this.users.map(u => `
                    <label class="form-check mb-0" title="${esc(u.email || 'No email address on Settings > User Accounts')}">
                        <input type="checkbox" class="form-check-input" data-user="${esc(u.username)}"
                            ${a.users.includes(u.username) ? 'checked' : ''} ${u.email ? '' : 'disabled'}>
                        <span class="form-check-label">${esc(u.name)}${u.email ? '' : ' <span class="text-muted">(No Email)</span>'}</span>
                    </label>`).join('')}</div>
                <div class="fw-semibold small mb-1">Subject</div>
                <input type="text" class="form-control form-control-sm mb-1 pa-subject" maxlength="200" value="${esc(a.subject)}">
                <div class="form-text mb-3">These are filled in when sent, in the subject and in text parts: ${tags}</div>
                <div class="fw-semibold small mb-1">Style</div>
                <div class="form-text mb-1">Follows the style on Settings &gt; Email Alerts. Tick Own to set a field for this page only.
                    Email programs cannot load web fonts, so DM Sans only shows where it is installed; others see Arial.</div>
                <div class="pa-style mb-3"></div>
                <div class="fw-semibold small mb-1">Email Content</div>
                <div class="pa-blocks"></div>
                <div class="d-flex gap-2 mt-2">
                    <button type="button" class="btn btn-sm btn-outline-primary" data-add="text"><i class="fas fa-plus"></i> Add Text</button>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-add="table"><i class="fas fa-plus"></i> Add Preview Table</button>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-add="link"><i class="fas fa-plus"></i> Add Link Button</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary ms-auto pa-test"><i class="fas fa-paper-plane me-1"></i>Send Test To Me</button>
                </div>
                <div class="pa-result small mt-2"></div>`;
            this.el.querySelectorAll('[data-user]').forEach(cb => cb.addEventListener('change', () => {
                const u = cb.dataset.user;
                a.users = cb.checked ? [...new Set([...a.users, u])] : a.users.filter(x => x !== u);
            }));
            this.el.querySelector('.pa-subject').addEventListener('input', e => { a.subject = e.target.value; });
            this.styleEditor = new StyleEditor(this.el.querySelector('.pa-style'), this.styleMeta,
                this.styleEditor ? this.styleEditor.values() : this.ownStyle, this.globalStyle);
            this.el.querySelectorAll('[data-add]').forEach(b => b.addEventListener('click', () => {
                const type = b.dataset.add;
                a.blocks.push(type === 'table' ? { type, max_rows: 50 } : { type, text: type === 'link' ? 'Open {page}' : '' });
                this.renderBlocks();
            }));
            this.el.querySelector('.pa-test').addEventListener('click', e => this.test(e.currentTarget));
            this.renderBlocks();
        }

        renderBlocks() {
            const blocks = this.alert.blocks;
            const holder = this.el.querySelector('.pa-blocks');
            const last = blocks.length - 1;
            holder.innerHTML = blocks.map((b, i) => {
                const input = b.type === 'text'
                    ? `<textarea class="form-control form-control-sm" rows="3" maxlength="4000" data-i="${i}" data-field="text">${esc(b.text)}</textarea>`
                    : b.type === 'link'
                        ? `<input type="text" class="form-control form-control-sm" maxlength="100" data-i="${i}" data-field="text" value="${esc(b.text)}" placeholder="Button text">`
                        : `<label class="d-flex align-items-center gap-2 small mb-0">Show the first
                               <input type="number" class="form-control form-control-sm" style="width:80px" min="1" max="500" data-i="${i}" data-field="max_rows" value="${esc(b.max_rows)}">
                               rows, with the page's columns and cell colours</label>`;
                return `<div class="pa-block">
                    <span class="pa-block-name">${BLOCK_NAMES[b.type]}</span>
                    <div class="flex-grow-1">${input}</div>
                    <button type="button" class="btn btn-sm btn-outline-secondary" title="Move up" data-move="${i}" data-step="-1" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" title="Move down" data-move="${i}" data-step="1" ${i === last ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                    <button type="button" class="btn btn-sm btn-outline-danger" title="Remove" data-remove="${i}"><i class="fas fa-times"></i></button>
                </div>`;
            }).join('') || '<div class="text-muted small">The email is empty. Add at least one part.</div>';
            holder.querySelectorAll('[data-field]').forEach(inp => inp.addEventListener('input', () => {
                blocks[inp.dataset.i][inp.dataset.field] = inp.value;
            }));
            holder.querySelectorAll('[data-move]').forEach(btn => btn.addEventListener('click', () => {
                const i = +btn.dataset.move, j = i + +btn.dataset.step;
                [blocks[i], blocks[j]] = [blocks[j], blocks[i]];
                this.renderBlocks();
            }));
            holder.querySelectorAll('[data-remove]').forEach(btn => btn.addEventListener('click', () => {
                blocks.splice(+btn.dataset.remove, 1);
                this.renderBlocks();
            }));
        }

        async test(button) {
            const out = this.el.querySelector('.pa-result');
            out.className = 'pa-result small mt-2';
            out.textContent = '';
            try {
                const data = await whileBusy(button, 'Sending...', () => postJson(this.api + '/test', { alert: this.draft() }));
                out.classList.add('text-success');
                out.textContent = 'Test sent to ' + data.sent_to.join(', ');
            } catch (e) {
                out.classList.add('text-danger');
                out.textContent = 'Test not sent: ' + e.message;
            }
        }
    }

    /* ── Send Alert popup ──────────────────────────────────────────────── */
    async function openSend(api, modalId) {
        const modalEl = document.getElementById(modalId);
        const body = modalEl.querySelector('.pa-send-body');
        const sendBtn = modalEl.querySelector('.pa-send-btn');
        sendBtn.classList.remove('d-none');
        sendBtn.disabled = true;
        body.innerHTML = '<div class="text-muted small">Loading...</div>';
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
        try {
            const data = await getJson(api);
            const byName = Object.fromEntries(data.users.map(u => [u.username, u]));
            const people = data.alert.users.map(u => byName[u]).filter(u => u && u.email);
            if (!people.length) {
                body.innerHTML = '<div class="text-danger small">Nobody is ticked to receive this alert. Tick people in Settings &gt; Alerts.</div>';
                return;
            }
            body.innerHTML = `<p class="mb-2">Email <strong>${esc(data.alert.subject)}</strong> to:</p>
                <ul class="mb-2">${people.map(u => `<li>${esc(u.name)} <span class="text-muted">(${esc(u.email)})</span></li>`).join('')}</ul>
                <div class="form-text">It uses the data on the page now. Change who gets it and what it says in Settings &gt; Alerts.</div>
                <div class="pa-send-result small mt-2"></div>`;
            sendBtn.disabled = false;
            sendBtn.onclick = async () => {
                const out = body.querySelector('.pa-send-result');
                out.className = 'pa-send-result small mt-2';
                try {
                    const res = await whileBusy(sendBtn, 'Sending...', () => postJson(api + '/send'));
                    out.classList.add('text-success');
                    out.textContent = 'Sent to ' + res.sent_to.length + (res.sent_to.length === 1 ? ' person.' : ' people.');
                    sendBtn.classList.add('d-none');
                } catch (e) {
                    out.classList.add('text-danger');
                    out.textContent = 'Not sent: ' + e.message;
                }
            };
        } catch (e) {
            body.innerHTML = `<div class="text-danger small">Could not load the alert: ${esc(e.message)}</div>`;
        }
    }

    return { Editor, StyleEditor, openSend, whileBusy, getJson, postJson };
})();
