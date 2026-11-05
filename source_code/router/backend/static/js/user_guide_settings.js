/* Settings > User Guide: the list of sections, the editor with its preview, and the
   guide's settings. Everything typed is put on the page as text, never as HTML. The
   preview is the only HTML, and the server escapes the typed text when building it. */
(function () {
    const root = document.getElementById('guide-settings-page');
    const BASE = root.dataset.base;
    const API = BASE + '/settings/user-guide';
    const $ = id => document.getElementById(id);

    let sections = [];
    let pages = [];
    let people = [];
    let visibilities = {};
    let settings = {};
    let editingId = null;
    let previewTimer = null;
    let previewRun = 0;

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

    function button(label, title, onClick) {
        const btn = make('button', label, 'btn btn-sm btn-outline-secondary');
        btn.type = 'button';
        if (title) btn.title = title;
        btn.addEventListener('click', onClick);
        return btn;
    }

    // ── List ─────────────────────────────────────────────────────────────

    function renderList() {
        const body = document.querySelector('#guide-table tbody');
        const pageNames = Object.fromEntries(pages.map(p => [p.key, p.name]));
        body.textContent = '';
        sections.forEach((section, index) => {
            const row = document.createElement('tr');
            const moveCell = document.createElement('td');
            moveCell.className = 'text-nowrap';
            const up = button('', 'Move Up', () => move(index, -1));
            const down = button('', 'Move Down', () => move(index, 1));
            up.appendChild(make('i', null, 'fas fa-arrow-up'));
            down.appendChild(make('i', null, 'fas fa-arrow-down'));
            up.disabled = index === 0;
            down.disabled = index === sections.length - 1;
            down.classList.add('ms-1');
            moveCell.append(up, down);
            row.appendChild(moveCell);
            row.appendChild(make('td', section.title));
            row.appendChild(make('td', pageNames[section.page_key] || ''));
            row.appendChild(make('td', visibilities[section.visibility] || section.visibility));
            const count = section.audience.length;
            const peopleCell = make('td', count === 0 ? 'Only you' : `You + ${count}`);
            peopleCell.title = section.audience.join(', ');
            row.appendChild(peopleCell);
            const actions = document.createElement('td');
            actions.className = 'text-end text-nowrap';
            const link = button('Copy Link', 'Copy a link to this section, e.g. for an email', () => copyLink(section));
            const edit = button('Edit', '', () => openEditor(section));
            const del = button('Delete', '', () => removeSection(section));
            edit.classList.add('ms-1');
            del.classList.add('ms-1');
            actions.append(link, edit, del);
            row.appendChild(actions);
            body.appendChild(row);
        });
        $('guide-empty').style.display = sections.length ? 'none' : '';
    }

    async function move(index, step) {
        const moved = sections.splice(index, 1)[0];
        sections.splice(index + step, 0, moved);
        renderList();
        try {
            await call('/reorder', { ids: sections.map(s => s.id) });
            setNote('guide-list-note', '');
        } catch (e) {
            setNote('guide-list-note', e.message, true);
            load();
        }
    }

    async function copyLink(section) {
        const url = `${location.origin}${BASE}/user-guide?section=${section.id}`;
        try {
            await navigator.clipboard.writeText(url);
            setNote('guide-list-note', 'Link copied: ' + url);
        } catch (e) {
            setNote('guide-list-note', url);
        }
    }

    async function removeSection(section) {
        if (!confirm(`Delete "${section.title}"? This cannot be undone.`)) return;
        try {
            await call('/delete', { id: section.id });
            if (editingId === section.id) closeEditor();
            setNote('guide-list-note', 'Deleted.');
            load();
        } catch (e) {
            setNote('guide-list-note', e.message, true);
        }
    }

    async function generate() {
        if (!confirm('Add a starter section for every page that has none? They will be seen only by you.')) return;
        try {
            const data = await call('/generate', {});
            setNote('guide-list-note', data.added.length ? `Added ${data.added.length} sections.` : 'Every page already has a section.');
            load();
        } catch (e) {
            setNote('guide-list-note', e.message, true);
        }
    }

    // One page's starter section, opened ready to write. A page that gained a
    // section since this list loaded keeps it, and that one is opened instead.
    async function generatePage() {
        const pageKey = $('guide-generate-page').value;
        if (!pageKey) return;
        try {
            const data = await call('/generate', { page_key: pageKey });
            await load();
            const section = sections.find(s => s.id === data.added[0]) || sections.find(s => s.page_key === pageKey);
            setNote('guide-list-note', data.added.length ? 'Section added. It is seen only by you until you change Who Can See.' : 'That page already has a section.');
            if (section) openEditor(section);
        } catch (e) {
            setNote('guide-list-note', e.message, true);
        }
    }

    function fillGeneratePicker() {
        const picker = $('guide-generate-page');
        const keep = picker.value;
        const covered = new Set(sections.map(s => s.page_key));
        const open = pages.filter(p => !covered.has(p.key));
        picker.textContent = '';
        picker.appendChild(new Option(open.length ? 'Choose A Page' : 'Every Page Has A Section', ''));
        open.forEach(p => picker.appendChild(new Option(p.name, p.key)));
        picker.value = open.some(p => p.key === keep) ? keep : '';
        pickerChanged();
    }

    function pickerChanged() {
        $('guide-generate-page-btn').disabled = !$('guide-generate-page').value;
    }

    // ── Editor ───────────────────────────────────────────────────────────

    function starterFor(pageName) {
        return (settings.starter || '').split('{page}').join(pageName);
    }

    function openEditor(section) {
        editingId = section ? section.id : null;
        $('guide-editor-title').textContent = section ? 'Edit Section' : 'Add Section';
        $('guide-title').value = section ? section.title : '';
        $('guide-page').value = section ? (section.page_key || '') : '';
        $('guide-visibility').value = section ? section.visibility : 'only_me';
        $('guide-body').value = section ? section.body : '';
        const chosen = new Set(section ? section.chosen_users : []);
        document.querySelectorAll('#guide-people input').forEach(tick => { tick.checked = chosen.has(tick.value); });
        showPeople();
        setNote('guide-save-note', '');
        $('guide-editor').style.display = '';
        $('guide-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
        refreshPreview();
    }

    function closeEditor() {
        editingId = null;
        $('guide-editor').style.display = 'none';
    }

    function showPeople() {
        $('guide-people-wrap').style.display = $('guide-visibility').value === 'users' ? '' : 'none';
    }

    // Choosing a page for a new, empty section starts it off.
    function pageChosen() {
        const page = pages.find(p => p.key === $('guide-page').value);
        if (!page) return;
        if (!$('guide-title').value.trim()) $('guide-title').value = page.name;
        if (!$('guide-body').value.trim()) {
            $('guide-body').value = starterFor(page.name);
            refreshPreview();
        }
    }

    function refreshPreview() {
        clearTimeout(previewTimer);
        previewTimer = setTimeout(async () => {
            const run = ++previewRun;
            try {
                const data = await call('/preview', { body: $('guide-body').value });
                if (run === previewRun) $('guide-preview').innerHTML = data.html;
            } catch (e) {
                if (run === previewRun) $('guide-preview').textContent = e.message;
            }
        }, 250);
    }

    async function saveSection() {
        const payload = {
            id: editingId,
            title: $('guide-title').value,
            page_key: $('guide-page').value,
            visibility: $('guide-visibility').value,
            chosen_users: [...document.querySelectorAll('#guide-people input:checked')].map(t => t.value),
            body: $('guide-body').value,
        };
        try {
            const data = await call('/save', payload);
            editingId = data.section.id;
            $('guide-editor-title').textContent = 'Edit Section';
            setNote('guide-save-note', 'Saved.');
            load();
        } catch (e) {
            setNote('guide-save-note', e.message, true);
        }
    }

    // ── Guide settings ───────────────────────────────────────────────────

    function fillSettings() {
        $('set-title').value = settings.title;
        $('set-font-size').value = settings.font_size;
        $('set-nav-width').value = settings.nav_width;
        $('set-starter').value = settings.starter;
    }

    async function saveSettings() {
        try {
            const data = await call('/settings', {
                title: $('set-title').value,
                font_size: $('set-font-size').value,
                nav_width: $('set-nav-width').value,
                starter: $('set-starter').value,
            });
            settings = data.settings;
            fillSettings();
            setNote('set-save-note', 'Saved.');
        } catch (e) {
            setNote('set-save-note', e.message, true);
        }
    }

    // ── Loading ──────────────────────────────────────────────────────────

    function fillChoices() {
        const page = $('guide-page');
        const keepPage = page.value;
        page.textContent = '';
        page.appendChild(new Option('None', ''));
        pages.forEach(p => page.appendChild(new Option(p.name, p.key)));
        page.value = keepPage;

        const visibility = $('guide-visibility');
        const keepVisibility = visibility.value || 'only_me';
        visibility.textContent = '';
        Object.entries(visibilities).forEach(([key, label]) => visibility.appendChild(new Option(label, key)));
        visibility.value = keepVisibility;

        const box = $('guide-people');
        const ticked = new Set([...box.querySelectorAll('input:checked')].map(t => t.value));
        box.textContent = '';
        people.forEach(person => {
            const label = make('label', null, 'd-flex align-items-center gap-2');
            const tick = document.createElement('input');
            tick.type = 'checkbox';
            tick.className = 'form-check-input m-0';
            tick.value = person.username;
            tick.checked = ticked.has(person.username);
            label.append(tick, make('span', person.name));
            box.appendChild(label);
        });
    }

    async function load() {
        try {
            const data = await call('/data');
            sections = data.sections;
            pages = data.pages;
            people = data.people;
            visibilities = data.visibilities;
            const firstLoad = !settings.title;
            settings = data.settings;
            fillChoices();
            fillGeneratePicker();
            renderList();
            if (firstLoad) {
                fillSettings();
                // The Edit button on the guide lands here with its section open.
                const wanted = new URLSearchParams(location.search).get('section');
                const section = sections.find(s => String(s.id) === wanted);
                if (section) openEditor(section);
            }
            $('guide-load-error').style.display = 'none';
        } catch (e) {
            $('guide-load-error').textContent = 'Could not load the guide: ' + e.message;
            $('guide-load-error').style.display = '';
        }
    }

    $('guide-add-btn').addEventListener('click', () => openEditor(null));
    $('guide-generate-btn').addEventListener('click', generate);
    $('guide-generate-page').addEventListener('change', pickerChanged);
    $('guide-generate-page-btn').addEventListener('click', generatePage);
    $('guide-cancel-btn').addEventListener('click', closeEditor);
    $('guide-save-btn').addEventListener('click', saveSection);
    $('guide-page').addEventListener('change', pageChosen);
    $('guide-visibility').addEventListener('change', showPeople);
    $('guide-body').addEventListener('input', refreshPreview);
    $('set-save-btn').addEventListener('click', saveSettings);
    load();
})();
