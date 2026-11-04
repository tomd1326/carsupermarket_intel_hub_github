/* Settings > Developments: the log of developments, the update email built from
   the ticked ones, the email's template and the list of past emails. Everything
   typed is put on the page as text, never as HTML. The email itself comes from
   the server and is shown in a sandboxed frame with scripts off. */
(function () {
    const root = document.getElementById('developments-page');
    const API = root.dataset.base + '/settings/developments';
    const $ = id => document.getElementById(id);

    let developments = [];
    const ticked = new Set();
    let editingId = null;
    let previewTimer = null;
    let previewRun = 0;
    let sender = '';

    async function call(path, body) {
        const options = body === undefined ? {} : {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        };
        const resp = await fetch(API + path, options);
        let data = {};
        try { data = await resp.json(); } catch (e) { /* the message below covers it */ }
        if (!resp.ok) throw new Error(data.detail || `The server answered ${resp.status}`);
        if (data.status === 'error') throw new Error(data.message);
        return data;
    }

    function make(tag, text, className) {
        const el = document.createElement(tag);
        if (text !== undefined && text !== null) el.textContent = text;
        if (className) el.className = className;
        return el;
    }

    function setNote(id, text, isError) {
        const note = $(id);
        note.textContent = text;
        note.className = 'small' + (isError ? ' text-danger' : '');
    }

    // ── Developments list ────────────────────────────────────────────────

    function renderDevelopments(pageNames) {
        const body = document.querySelector('#dev-table tbody');
        body.textContent = '';
        developments.forEach(dev => {
            const row = document.createElement('tr');
            const tickCell = document.createElement('td');
            const tick = document.createElement('input');
            tick.type = 'checkbox';
            tick.className = 'form-check-input';
            tick.checked = ticked.has(dev.id);
            tick.setAttribute('aria-label', 'Include ' + dev.title);
            tick.addEventListener('change', () => {
                if (tick.checked) ticked.add(dev.id); else ticked.delete(dev.id);
                refreshPreview();
            });
            tickCell.appendChild(tick);
            row.appendChild(tickCell);
            row.appendChild(make('td', dev.title));
            row.appendChild(make('td', dev.dev_type));
            row.appendChild(make('td', pageNames[dev.page_key] || ''));
            row.appendChild(make('td', dev.released_label));
            const statusCell = document.createElement('td');
            statusCell.appendChild(make('span', dev.status === 'live' ? 'Live' : 'Draft',
                'badge ' + (dev.status === 'live' ? 'bg-success' : 'bg-secondary')));
            row.appendChild(statusCell);
            row.appendChild(make('td', dev.last_emailed));
            const editCell = make('td', null, 'text-end');
            const edit = make('button', 'Edit', 'btn btn-sm btn-outline-secondary');
            edit.type = 'button';
            edit.addEventListener('click', () => openEditor(dev));
            editCell.appendChild(edit);
            row.appendChild(editCell);
            body.appendChild(row);
        });
        $('dev-empty').style.display = developments.length ? 'none' : '';
    }

    let pageNames = {};

    function fillChoices(data) {
        pageNames = {};
        const pageSelect = $('dev-page');
        pageSelect.textContent = '';
        pageSelect.appendChild(make('option', 'No Page'));
        pageSelect.options[0].value = '';
        data.pages.forEach(page => {
            pageNames[page.key] = page.name;
            const option = make('option', page.name);
            option.value = page.key;
            pageSelect.appendChild(option);
        });
        fillTypes(data.types);
    }

    function fillTypes(types) {
        const list = $('dev-type-list');
        list.textContent = '';
        types.forEach(name => {
            const option = document.createElement('option');
            option.value = name;
            list.appendChild(option);
        });
    }

    // ── Editor ───────────────────────────────────────────────────────────

    function addFaqRow(question, answer) {
        const row = make('div', null, 'faq-row');
        const q = make('input', null, 'form-control faq-q');
        q.placeholder = 'Question';
        q.value = question || '';
        const a = make('textarea', null, 'form-control faq-a');
        a.placeholder = 'Answer';
        a.rows = 2;
        a.value = answer || '';
        const remove = make('button', 'Remove', 'btn btn-sm btn-outline-secondary');
        remove.type = 'button';
        remove.addEventListener('click', () => row.remove());
        row.append(q, a, remove);
        $('faq-rows').appendChild(row);
    }

    function openEditor(dev) {
        editingId = dev ? dev.id : null;
        $('dev-editor-title').textContent = dev ? 'Edit Development' : 'Add Development';
        $('dev-title').value = dev ? dev.title : '';
        $('dev-type').value = dev ? dev.dev_type : '';
        $('dev-status').value = dev ? dev.status : 'draft';
        $('dev-page').value = dev ? (dev.page_key || '') : '';
        $('dev-released').value = dev ? dev.released_on : '';
        $('dev-replaces').value = dev ? dev.replaces : '';
        $('dev-summary').value = dev ? dev.summary : '';
        $('dev-details').value = dev ? dev.details : '';
        $('dev-guide').value = dev ? dev.guide : '';
        $('faq-rows').textContent = '';
        (dev ? dev.faqs : []).forEach(faq => addFaqRow(faq.question, faq.answer));
        $('dev-editor-error').textContent = '';
        $('dev-delete-btn').style.display = dev ? '' : 'none';
        $('dev-editor').style.display = '';
        $('dev-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
        $('dev-title').focus({ preventScroll: true });
    }

    function closeEditor() {
        editingId = null;
        $('dev-editor').style.display = 'none';
    }

    async function saveDevelopment() {
        const payload = {
            id: editingId,
            title: $('dev-title').value,
            dev_type: $('dev-type').value,
            status: $('dev-status').value,
            page_key: $('dev-page').value,
            released_on: $('dev-released').value,
            replaces: $('dev-replaces').value,
            summary: $('dev-summary').value,
            details: $('dev-details').value,
            guide: $('dev-guide').value,
            faqs: [...document.querySelectorAll('#faq-rows .faq-row')].map(row => ({
                question: row.querySelector('.faq-q').value,
                answer: row.querySelector('.faq-a').value,
            })),
        };
        const btn = $('dev-save-btn');
        btn.disabled = true;
        $('dev-editor-error').textContent = '';
        try {
            await call('/save', payload);
            closeEditor();
            await load();
        } catch (e) {
            $('dev-editor-error').textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    }

    async function deleteDevelopment() {
        const title = $('dev-title').value;
        if (!confirm(`Delete "${title}"? Past emails keep their own copy of the text.`)) return;
        try {
            await call('/delete', { id: editingId });
            ticked.delete(editingId);
            closeEditor();
            await load();
        } catch (e) {
            $('dev-editor-error').textContent = e.message;
        }
    }

    // ── Email ────────────────────────────────────────────────────────────

    function emailBody() {
        return { subject: $('email-subject').value, intro: $('email-intro').value, ids: [...ticked] };
    }

    function setEmailButtons(enabled) {
        $('email-send-btn').disabled = !enabled;
        $('email-copy-btn').disabled = !enabled;
    }

    function refreshPreview() {
        clearTimeout(previewTimer);
        setEmailButtons(false);
        previewTimer = setTimeout(runPreview, 300);
    }

    async function runPreview() {
        const run = ++previewRun;
        if (!ticked.size) {
            $('email-preview').srcdoc = '';
            setNote('email-note', 'Tick at least one development.');
            return;
        }
        try {
            const data = await call('/email/preview', emailBody());
            if (run !== previewRun) return;
            $('email-preview').srcdoc = data.html;
            setNote('email-note', data.problem || '', !!data.problem);
            setEmailButtons(!data.problem);
        } catch (e) {
            if (run !== previewRun) return;
            setNote('email-note', e.message, true);
        }
    }

    async function copyEmail(html, text) {
        if (!navigator.clipboard || !window.ClipboardItem) {
            throw new Error('this browser cannot copy formatted text');
        }
        await navigator.clipboard.write([new ClipboardItem({
            'text/html': new Blob([html], { type: 'text/html' }),
            'text/plain': new Blob([text], { type: 'text/plain' }),
        })]);
    }

    // The confirmation box every send goes through. Nothing is sent without it.
    function confirmSend(subject, to) {
        const recipients = [...new Set(to.split(/[;,\s]+/).filter(Boolean))];
        if (!recipients.length) {
            setNote('email-note', 'Enter at least one address to send to.', true);
            return false;
        }
        return confirm(`Send "${subject}" from ${sender} to ${recipients.length} people?\n\n${recipients.join('\n')}`);
    }

    async function sendTicked() {
        const to = $('email-to').value;
        if (!confirmSend($('email-subject').value, to)) return;
        setEmailButtons(false);
        try {
            const data = await call('/email/send', { ...emailBody(), to });
            setNote('email-note', data.message);
            await loadEmails();
        } catch (e) {
            setNote('email-note', e.message, true);
        } finally {
            setEmailButtons(true);
        }
    }

    async function generate(useEmail) {
        setEmailButtons(false);
        try {
            const data = await call('/email/generate', emailBody());
            await useEmail(data);
            await loadEmails();
        } catch (e) {
            setNote('email-note', e.message, true);
        } finally {
            setEmailButtons(true);
        }
    }

    function copyTicked() {
        return generate(async data => {
            try {
                await copyEmail(data.html, data.text);
            } catch (e) {
                throw new Error(`Could not copy: ${e.message}. Use Send Email instead.`);
            }
            setNote('email-note', 'Copied. Paste it into a new email.');
        });
    }

    // ── Past emails ──────────────────────────────────────────────────────

    function renderEmails(emails) {
        const body = document.querySelector('#email-table tbody');
        body.textContent = '';
        emails.forEach(item => {
            const row = document.createElement('tr');
            row.appendChild(make('td', item.created_label));
            row.appendChild(make('td', item.subject));
            row.appendChild(make('td', item.titles.join(', ')));
            const actions = make('td', null, 'text-end text-nowrap');
            const view = make('button', 'View', 'btn btn-sm btn-outline-secondary me-1');
            view.type = 'button';
            view.addEventListener('click', () => viewEmail(item));
            const send = make('button', 'Send', 'btn btn-sm btn-outline-secondary me-1');
            send.type = 'button';
            send.addEventListener('click', () => sendPast(item));
            const copy = make('button', 'Copy', 'btn btn-sm btn-outline-secondary');
            copy.type = 'button';
            copy.addEventListener('click', () => copyPast(item));
            actions.append(view, send, copy);
            row.appendChild(actions);
            body.appendChild(row);
        });
        $('email-empty').style.display = emails.length ? 'none' : '';
    }

    async function viewEmail(item) {
        try {
            const stored = await call(`/email/${item.id}`);
            previewRun++;
            $('email-preview').srcdoc = stored.html;
            setEmailButtons(false);
            setNote('email-note', `Showing the email from ${item.created_label}. Change a tick to go back to your new email.`);
            $('email-preview').scrollIntoView({ behavior: 'smooth', block: 'center' });
        } catch (e) {
            setNote('email-note', e.message, true);
        }
    }

    async function sendPast(item) {
        const to = $('email-to').value;
        if (!confirmSend(item.subject, to)) return;
        try {
            const data = await call(`/email/${item.id}/send`, { to });
            setNote('email-note', data.message);
        } catch (e) {
            setNote('email-note', e.message, true);
        }
    }

    async function copyPast(item) {
        try {
            const stored = await call(`/email/${item.id}`);
            await copyEmail(stored.html, stored.text);
            setNote('email-note', 'Copied. Paste it into a new email.');
        } catch (e) {
            setNote('email-note', `Could not copy: ${e.message}. Use Send instead.`, true);
        }
    }

    async function loadEmails() {
        const data = await call('/data');
        developments = data.developments;
        renderDevelopments(pageNames);
        renderEmails(data.emails);
    }

    // ── Template ─────────────────────────────────────────────────────────

    function fillTemplate(template) {
        document.querySelectorAll('[data-template]').forEach(input => {
            input.value = template[input.dataset.template] ?? '';
        });
    }

    async function saveTemplate() {
        const payload = {};
        document.querySelectorAll('[data-template]').forEach(input => {
            payload[input.dataset.template] = input.value;
        });
        const btn = $('template-save-btn');
        btn.disabled = true;
        try {
            const data = await call('/template', payload);
            fillTemplate(data.template);
            setNote('template-note', 'Saved.');
            await load();
        } catch (e) {
            setNote('template-note', e.message, true);
        } finally {
            btn.disabled = false;
        }
    }

    // ── Start ────────────────────────────────────────────────────────────

    async function load(isFirst) {
        const data = await call('/data');
        developments = data.developments;
        sender = data.sender;
        // A development deleted elsewhere must not stay ticked out of sight.
        const known = new Set(developments.map(d => d.id));
        [...ticked].forEach(id => { if (!known.has(id)) ticked.delete(id); });
        fillChoices(data);
        renderDevelopments(pageNames);
        renderEmails(data.emails);
        if (isFirst) {
            fillTemplate(data.template);
            $('email-to').value = data.template.default_send_to;
            $('email-subject').value = data.template.subject;
            $('email-intro').value = data.template.intro;
        }
        refreshPreview();
    }

    $('dev-add-btn').addEventListener('click', () => openEditor(null));
    $('dev-save-btn').addEventListener('click', saveDevelopment);
    $('dev-cancel-btn').addEventListener('click', closeEditor);
    $('dev-delete-btn').addEventListener('click', deleteDevelopment);
    $('faq-add-btn').addEventListener('click', () => addFaqRow());
    $('email-subject').addEventListener('input', refreshPreview);
    $('email-intro').addEventListener('input', refreshPreview);
    $('email-send-btn').addEventListener('click', sendTicked);
    $('email-copy-btn').addEventListener('click', copyTicked);
    $('template-save-btn').addEventListener('click', saveTemplate);

    load(true).catch(e => {
        const box = $('dev-load-error');
        box.textContent = 'Could not load developments: ' + e.message;
        box.style.display = '';
    });
})();
