// Weekly Business Review Comments (server: routers/wbr_comments.py). Loaded
// before kpi.js on the review page only; what it uses from kpi.js (settings,
// findRow, rowTitle, escHtml, PERIOD_NAMES, BASE_URL) is only called once the
// page has loaded.
//
// Discussion threads on a chart, laid out like Google Slides comments: a
// coloured initial, name and time, a tick to resolve, a menu to edit or
// delete, replies inside the same card, @mentions in blue and "Assigned to".
// Not the same as Notes (business changes drawn on the charts).
//
// Every chart on the grid and on the slides has a comment button (a
// .wbr-comment-slot in its title line, filled here) with its open thread
// count. It opens the side panel for that chart; the toolbar's Comments
// button opens it for every chart. All shown only while Settings > Comments
// is On.
const WbrComments = (() => {
    let comments = [];          // every comment, threads and replies, oldest first
    let people = [];            // [{username, name, email}] who can be @mentioned
    let me = '';
    let isAdminUser = false;
    let loaded = false;
    let loading = null;
    let panelTarget = undefined;   // undefined = closed, null = all charts, else a target
    let showResolved = false;
    let editing = null;            // id of the comment being edited
    let pollTimer = null;
    const AVATAR_COLOURS = ['#7b1fa2', '#00695c', '#d81b60', '#1565c0', '#ef6c00', '#5e35b1', '#2e7d32', '#6d4c41', '#00838f', '#c62828'];

    const on = () => Number(settings.wbr_comments_on) === 1;

    // ── Data ──
    function load() {
        loading = fetch(BASE_URL + '/weekly-business-review/comments')
            .then(r => r.json())
            .then(d => {
                if (d.status !== 'ok') throw new Error(d.message || 'load failed');
                comments = d.comments || [];
                people = d.people || [];
                me = d.me;
                isAdminUser = !!d.is_admin;
                loaded = true;
                refreshButtons();
                renderPanel();
            })
            .catch(err => { document.getElementById('statusText').textContent = 'Comments: ' + err.message; })
            .finally(() => { loading = null; });
        return loading;
    }

    async function post(path, body) {
        const resp = await fetch(BASE_URL + '/weekly-business-review/comments' + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'save failed');
        await load();
        return resp;
    }

    const threads = () => comments.filter(c => c.parent_id === null);
    const repliesOf = id => comments.filter(c => c.parent_id === id);
    const openCount = target => threads().filter(c => c.target === target && !c.resolved_at).length;

    // ── Names, times, text ──
    function person(username) {
        return people.find(p => p.username === username) || { username, name: username, email: '' };
    }
    function avatarHtml(username, name) {
        let h = 0;
        for (const ch of username || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0;
        const colour = AVATAR_COLOURS[h % AVATAR_COLOURS.length];
        const letter = ((name || username || '?').trim()[0] || '?').toUpperCase();
        return `<span class="wbr-avatar" style="background:${colour}">${escHtml(letter)}</span>`;
    }
    // "12:55 PM Today", "11:33 AM Sep 7", with the year when not this year.
    function whenText(iso) {
        const d = new Date(iso);
        const now = new Date();
        const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time + ' Today';
        const opts = { month: 'short', day: 'numeric' };
        if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
        return time + ' ' + d.toLocaleDateString('en-US', opts);
    }
    // Plain text made safe, then each @mention of a real person shown in blue
    // as their email (as Google Slides does), line breaks kept.
    function bodyHtml(text) {
        return escHtml(text).replace(/@([A-Za-z0-9._\-]+(?:@[A-Za-z0-9.\-]+)?)/g, (all, name) => {
            const p = people.find(x => x.username.toLowerCase() === name.replace(/\.$/, '').toLowerCase());
            return p ? `<span class="wbr-mention">@${escHtml(p.email || p.username)}</span>${name.endsWith('.') ? '.' : ''}` : all;
        }).replace(/\n/g, '<br>');
    }
    function targetTitle(target) {
        const m = /^chart:(.+)\|([mwd])$/.exec(target || '');
        const row = m && findRow(m[1]);
        return row ? `${rowTitle(row)} - ${PERIOD_NAMES[m[2]]}` : 'Removed Chart';
    }

    // ── Chart buttons ──
    function refreshButtons() {
        const show = on();
        const btn = document.getElementById('wbrCommentsBtn');
        if (btn) btn.style.display = show ? '' : 'none';
        document.querySelectorAll('.wbr-comment-slot').forEach(slot => {
            if (!show) { slot.innerHTML = ''; return; }
            const n = loaded ? openCount(slot.dataset.commentTarget) : 0;
            slot.innerHTML = `<button type="button" class="chart-title-btn wbr-comment-btn ${n ? 'has-comments' : ''}" title="Comments"
                                      onclick="WbrComments.open('${escHtml(slot.dataset.commentTarget)}')">
                                  <i class="fas fa-comment"></i>${n ? ` ${n}` : ''}</button>`;
        });
        if (show && !loaded && !loading) load();
        if (!show) close();
    }

    // ── Panel ──
    function panel() {
        let el = document.getElementById('wbrCommentsPanel');
        if (!el) {
            el = document.createElement('aside');
            el.id = 'wbrCommentsPanel';
            el.className = 'wbr-cpanel';
            el.hidden = true;
            document.body.appendChild(el);
            el.addEventListener('input', onInput);
            el.addEventListener('keydown', onKeydown);
        }
        return el;
    }

    function open(target) {
        if (!on()) return;
        panelTarget = target || null;
        showResolved = Number(settings.wbr_comments_show_resolved) === 1;
        editing = null;
        panel().hidden = false;
        renderPanel();
        if (!loaded && !loading) load();
        if (!pollTimer) pollTimer = setInterval(() => { if (!document.hidden && panelTarget !== undefined && !busyTyping()) load(); }, 60000);
    }
    function openAll() { open(null); }

    function close() {
        panelTarget = undefined;
        const el = document.getElementById('wbrCommentsPanel');
        if (el) el.hidden = true;
        clearInterval(pollTimer);
        pollTimer = null;
    }

    // Do not redraw under someone typing.
    function busyTyping() {
        const el = document.getElementById('wbrCommentsPanel');
        return !!(el && [...el.querySelectorAll('textarea')].some(t => t.value.trim()));
    }

    function commentHtml(c, isThread) {
        const p = person(c.author);
        const canEdit = c.author === me;
        const canDelete = canEdit || isAdminUser;
        const canResolve = isThread && (c.author === me || c.assigned_to === me || isAdminUser);
        const resolveBtn = !canResolve ? '' : c.resolved_at
            ? `<button type="button" class="wbr-c-icon" title="Reopen" onclick="WbrComments.resolve(${c.id}, false)"><i class="fas fa-undo"></i></button>`
            : `<button type="button" class="wbr-c-icon wbr-c-tick" title="Mark As Resolved And Hide Discussion" onclick="WbrComments.resolve(${c.id}, true)"><i class="fas fa-check"></i></button>`;
        const menu = !(canEdit || canDelete) ? '' : `
            <span class="wbr-c-menu">
                <button type="button" class="wbr-c-icon" title="More Options" onclick="WbrComments.toggleMenu(this)"><i class="fas fa-ellipsis-v"></i></button>
                <span class="wbr-c-menu-list" hidden>
                    ${canEdit ? `<button type="button" onclick="WbrComments.startEdit(${c.id})">Edit</button>` : ''}
                    ${canDelete ? `<button type="button" onclick="WbrComments.remove(${c.id})">Delete</button>` : ''}
                </span>
            </span>`;
        const body = editing === c.id
            ? `<textarea class="wbr-c-input" data-edit="${c.id}" rows="3">${escHtml(c.body)}</textarea>
               <div class="wbr-c-buttons">
                   <button type="button" class="btn btn-apply btn-sm" onclick="WbrComments.saveEdit(${c.id})">Save</button>
                   <button type="button" class="btn btn-edit btn-sm" onclick="WbrComments.cancelEdit()">Cancel</button>
               </div>`
            : `<div class="wbr-c-body">${bodyHtml(c.body)}</div>`;
        const assigned = isThread && c.assigned_to
            ? `<div class="wbr-c-assigned">Assigned to<br>${escHtml(person(c.assigned_to).email || c.assigned_to)}</div>` : '';
        return `
            <div class="wbr-c" data-comment="${c.id}">
                <div class="wbr-c-head">
                    ${avatarHtml(c.author, p.name)}
                    <div class="wbr-c-who">
                        <div class="wbr-c-name">${escHtml(c.author_name || p.name)}</div>
                        <div class="wbr-c-time">${escHtml(whenText(c.created_at))}${c.edited_at ? ' (Edited)' : ''}</div>
                    </div>
                    ${resolveBtn}${menu}
                </div>
                ${body}
                ${assigned}
            </div>`;
    }

    function threadHtml(t) {
        const replies = repliesOf(t.id).map(r => commentHtml(r, false)).join('');
        const label = panelTarget === null
            ? `<button type="button" class="wbr-c-chart" onclick="WbrComments.open('${escHtml(t.target)}')">${escHtml(targetTitle(t.target))}</button>` : '';
        const resolvedLine = t.resolved_at ? `<div class="wbr-c-resolved"><i class="fas fa-check"></i> Resolved by ${escHtml(person(t.resolved_by).name)}</div>` : '';
        return `
            <div class="wbr-thread ${t.resolved_at ? 'resolved' : ''}" data-thread="${t.id}">
                ${label}${resolvedLine}
                ${commentHtml(t, true)}
                ${replies}
                ${t.resolved_at ? '' : `
                <div class="wbr-c-reply">
                    <textarea class="wbr-c-input" data-reply="${t.id}" rows="1" placeholder="Reply or add others with @"></textarea>
                    <div class="wbr-c-buttons" hidden>
                        <button type="button" class="btn btn-apply btn-sm" onclick="WbrComments.reply(${t.id})">Reply</button>
                        <button type="button" class="btn btn-edit btn-sm" onclick="WbrComments.clearReply(${t.id})">Cancel</button>
                    </div>
                </div>`}
            </div>`;
    }

    function renderPanel() {
        if (panelTarget === undefined) return;
        const el = panel();
        const list = threads()
            .filter(t => panelTarget === null || t.target === panelTarget)
            .filter(t => showResolved || !t.resolved_at)
            .sort((a, b) => b.created_at.localeCompare(a.created_at));
        const resolvedCount = threads().filter(t => (panelTarget === null || t.target === panelTarget) && t.resolved_at).length;
        const title = panelTarget === null ? 'All Comments' : targetTitle(panelTarget);
        const newBox = panelTarget === null ? '' : `
            <div class="wbr-cpanel-new">
                <textarea class="wbr-c-input" id="wbrNewComment" rows="3" placeholder="Comment or add others with @"></textarea>
                <label class="wbr-c-assign" id="wbrAssignRow" hidden><input type="checkbox" id="wbrAssign"> <span id="wbrAssignText"></span></label>
                <div class="wbr-c-buttons">
                    <button type="button" class="btn btn-apply btn-sm" onclick="WbrComments.add()">Comment</button>
                    <button type="button" class="btn btn-edit btn-sm" onclick="WbrComments.clearNew()">Cancel</button>
                </div>
            </div>`;
        const kept = keepTyping(el);
        el.innerHTML = `
            <div class="wbr-cpanel-head">
                <span class="wbr-cpanel-title"><i class="fas fa-comment"></i> ${escHtml(title)}</span>
                <button type="button" class="modal-close-x" onclick="WbrComments.close()" aria-label="Close">&times;</button>
            </div>
            <div class="wbr-cpanel-tools">
                ${panelTarget === null ? '' : '<button type="button" class="chart-title-btn" onclick="WbrComments.openAll()">All Comments</button>'}
                <label class="wbr-c-showres"><input type="checkbox" ${showResolved ? 'checked' : ''} onchange="WbrComments.setShowResolved(this.checked)">
                    Show Resolved${resolvedCount ? ` (${resolvedCount})` : ''}</label>
            </div>
            ${newBox}
            <div class="wbr-cpanel-list">
                ${!loaded ? '<div class="text-muted">Loading...</div>'
                  : list.length ? list.map(threadHtml).join('') : '<div class="text-muted wbr-c-none">No comments yet.</div>'}
            </div>
            <div class="wbr-mention-list" hidden></div>`;
        restoreTyping(el, kept);
        updateAssign();
    }

    // Text being typed survives a redraw (a reload or another action).
    function keepTyping(el) {
        const kept = {};
        el.querySelectorAll('textarea').forEach(t => {
            const key = t.id || (t.dataset.reply ? 'r' + t.dataset.reply : t.dataset.edit ? 'e' + t.dataset.edit : '');
            if (key && t.value) kept[key] = t.value;
        });
        return kept;
    }
    function restoreTyping(el, kept) {
        el.querySelectorAll('textarea').forEach(t => {
            const key = t.id || (t.dataset.reply ? 'r' + t.dataset.reply : t.dataset.edit ? 'e' + t.dataset.edit : '');
            if (key && kept[key] !== undefined) {
                t.value = kept[key];
                if (t.dataset.reply) t.nextElementSibling.hidden = false;
            }
        });
    }

    // ── Actions ──
    const fail = err => { alert(err.message); };

    function add() {
        const box = document.getElementById('wbrNewComment');
        const body = box.value.trim();
        if (!body) return;
        const assign = !document.getElementById('wbrAssignRow').hidden && document.getElementById('wbrAssign').checked;
        box.value = '';
        post('', { target: panelTarget, body, assign }).catch(err => { box.value = body; fail(err); });
    }
    function clearNew() {
        const box = document.getElementById('wbrNewComment');
        if (box) box.value = '';
        updateAssign();
    }
    function reply(id) {
        const box = document.querySelector(`[data-reply="${id}"]`);
        const body = box.value.trim();
        if (!body) return;
        box.value = '';
        post('', { parent_id: id, body }).catch(err => { box.value = body; fail(err); });
    }
    function clearReply(id) {
        const box = document.querySelector(`[data-reply="${id}"]`);
        if (!box) return;
        box.value = '';
        box.nextElementSibling.hidden = true;
    }
    function resolve(id, resolved) { post(`/${id}/resolve`, { resolved }).catch(fail); }
    function remove(id) {
        const c = comments.find(x => x.id === id);
        if (!confirm(c && c.parent_id === null ? 'Delete this comment and its replies?' : 'Delete this reply?')) return;
        post(`/${id}/delete`).catch(fail);
    }
    function startEdit(id) { editing = id; renderPanel(); }
    function cancelEdit() { editing = null; renderPanel(); }
    function saveEdit(id) {
        const box = document.querySelector(`[data-edit="${id}"]`);
        const body = box.value.trim();
        if (!body) return;
        editing = null;
        post(`/${id}/edit`, { body }).catch(err => { editing = id; renderPanel(); fail(err); });
    }
    function toggleMenu(btn) {
        const list = btn.nextElementSibling;
        const wasHidden = list.hidden;
        document.querySelectorAll('.wbr-c-menu-list').forEach(l => { l.hidden = true; });
        list.hidden = !wasHidden;
    }
    function setShowResolved(v) { showResolved = v; renderPanel(); }

    // "Assign to <name>" under a new comment that mentions someone.
    function updateAssign() {
        const box = document.getElementById('wbrNewComment');
        const row = document.getElementById('wbrAssignRow');
        if (!box || !row) return;
        const first = mentionedIn(box.value)[0];
        row.hidden = !first;
        if (first) document.getElementById('wbrAssignText').textContent = 'Assign to ' + (first.email || first.name);
    }
    function mentionedIn(text) {
        const found = [];
        (text.match(/@([A-Za-z0-9._\-]+(?:@[A-Za-z0-9.\-]+)?)/g) || []).forEach(m => {
            const p = people.find(x => x.username.toLowerCase() === m.slice(1).replace(/\.$/, '').toLowerCase());
            if (p && !found.includes(p)) found.push(p);
        });
        return found;
    }

    // ── @ suggestions ──
    let mention = null;   // { box, start, matches, index }

    function onInput(event) {
        const box = event.target;
        if (box.tagName !== 'TEXTAREA') return;
        if (box.dataset.reply) box.nextElementSibling.hidden = !box.value && document.activeElement !== box;
        if (box.id === 'wbrNewComment') updateAssign();
        const before = box.value.slice(0, box.selectionStart);
        const m = /(^|\s)@([A-Za-z0-9._\-@]*)$/.exec(before);
        if (!m) { hideMentions(); return; }
        const q = m[2].toLowerCase();
        const matches = people.filter(p => p.username !== me && [p.username, p.name, p.email].some(v => (v || '').toLowerCase().includes(q))).slice(0, 6);
        if (!matches.length) { hideMentions(); return; }
        mention = { box, start: before.length - m[2].length - 1, matches, index: 0 };
        showMentions();
    }

    function showMentions() {
        const list = panel().querySelector('.wbr-mention-list');
        list.innerHTML = mention.matches.map((p, i) => `
            <div class="wbr-mention-opt ${i === mention.index ? 'active' : ''}" onmousedown="event.preventDefault(); WbrComments.pickMention(${i})">
                ${avatarHtml(p.username, p.name)}
                <span><span class="wbr-mention-name">${escHtml(p.name)}</span><br><span class="wbr-mention-email">${escHtml(p.email || p.username)}</span></span>
            </div>`).join('');
        const boxRect = mention.box.getBoundingClientRect();
        const panelRect = panel().getBoundingClientRect();
        list.style.top = (boxRect.bottom - panelRect.top + panel().scrollTop + 2) + 'px';
        list.style.left = (boxRect.left - panelRect.left) + 'px';
        list.style.width = boxRect.width + 'px';
        list.hidden = false;
    }
    function hideMentions() {
        mention = null;
        const list = document.querySelector('#wbrCommentsPanel .wbr-mention-list');
        if (list) list.hidden = true;
    }
    function pickMention(i) {
        if (!mention) return;
        const { box, start } = mention;
        const p = mention.matches[i];
        const end = box.selectionStart;
        box.value = box.value.slice(0, start) + '@' + p.username + ' ' + box.value.slice(end);
        const caret = start + p.username.length + 2;
        box.focus();
        box.setSelectionRange(caret, caret);
        hideMentions();
        if (box.id === 'wbrNewComment') updateAssign();
    }
    function onKeydown(event) {
        if (!mention) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            mention.index = (mention.index + (event.key === 'ArrowDown' ? 1 : -1) + mention.matches.length) % mention.matches.length;
            showMentions();
            event.preventDefault();
        } else if (event.key === 'Enter' || event.key === 'Tab') {
            pickMention(mention.index);
            event.preventDefault();
        } else if (event.key === 'Escape') {
            hideMentions();
            event.preventDefault();
        }
    }

    document.addEventListener('focusin', event => {
        const box = event.target.closest && event.target.closest('[data-reply]');
        if (box) box.nextElementSibling.hidden = false;
    });
    document.addEventListener('click', event => {
        if (!event.target.closest('.wbr-c-menu')) document.querySelectorAll('.wbr-c-menu-list').forEach(l => { l.hidden = true; });
    });

    // A link from the bell (?comment=<thread id>): open that thread's chart
    // and show the thread.
    async function openFromLink() {
        const id = Number(new URLSearchParams(location.search).get('comment'));
        if (!id || !on()) return;
        if (!loaded) await (loading || load());
        const c = comments.find(x => x.id === id);
        if (!c) { document.getElementById('statusText').textContent = 'That comment has been deleted.'; return; }
        const root = c.parent_id ? comments.find(x => x.id === c.parent_id) || c : c;
        open(root.target);
        if (root.resolved_at) setShowResolved(true);
        const el = document.querySelector(`[data-thread="${root.id}"]`);
        if (el) {
            el.scrollIntoView({ block: 'center' });
            el.classList.add('flash');
            setTimeout(() => el.classList.remove('flash'), 2500);
        }
    }

    return {
        refreshButtons, open, openAll, close, add, clearNew, reply, clearReply, resolve, remove,
        startEdit, cancelEdit, saveEdit, toggleMenu, setShowResolved, pickMention, openFromLink,
        slotHtml: target => `<span class="wbr-comment-slot" data-comment-target="${escHtml(target)}"></span>`,
    };
})();
