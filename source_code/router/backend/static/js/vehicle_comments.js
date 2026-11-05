// Vehicle notes (server: routers/vehicle_comments.py and routers/wbr_comments.py).
// One popup that any page can open for a vehicle. Notes belong to the vehicle's
// Autoview stock id, so the whole history is the same on every page, with any
// site changes in the same timeline. People see the VRM.
//
// To use it on a page: load vehicle_comments.css and this file, set
// window.VEHICLE_COMMENTS_PAGE to the page key (it must be in VEHICLE_PAGES in
// routers/wbr_comments.py), and put VehicleComments.button(stockId, vrm) inside
// the VRM cell, as a comment bubble. Clicking it opens the popup, the counts fill
// in by themselves, and ?vehicle=<id>&vrm=<vrm> (an alert's link) opens that car.
const VehicleComments = (function () {
    'use strict';
    const BASE = window.BASE_URL || '';
    let counts = {};
    let ctx = null;               // { stockId, vrm, data, replyTo, editing }
    let modal = null;
    let activeBox = null;         // the note box last typed in, for @ names

    const esc = s => String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
    const when = iso => iso ? new Date(iso).toLocaleString('en-GB', {
        timeZone: 'Europe/London', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    }) : '';
    const page = () => window.VEHICLE_COMMENTS_PAGE || '';

    async function call(url, body) {
        const opts = body === undefined ? {} : {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        };
        const resp = await fetch(BASE + url, opts).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Something went wrong');
        return resp;
    }

    const count = stockId => counts[String(stockId)] || 0;

    function buttonInner(stockId) {
        const n = count(stockId);
        // A bubble with the count when there are notes; a + (on hover only) when there are none.
        return n ? `<i class="fas fa-comment"></i> ${n}` : '<i class="fas fa-plus"></i>';
    }

    function showCounts() {
        document.querySelectorAll('.vc-bubble[data-stock]').forEach(b => {
            b.innerHTML = buttonInner(b.dataset.stock);
            b.classList.toggle('vc-bubble-empty', count(b.dataset.stock) === 0);
        });
    }

    async function loadCounts() {
        try { counts = (await call('/api/vehicle-comments/counts')).counts || {}; } catch (e) { counts = {}; }
        showCounts();
    }

    // The comment bubble for a VRM cell. With no notes it only shows on hover of
    // the row. A car with no Autoview id has none.
    function button(stockId, vrm) {
        if (stockId === null || stockId === undefined || stockId === '') return '';
        return `<button type="button" class="vc-btn vc-bubble${count(stockId) ? '' : ' vc-bubble-empty'}" data-stock="${esc(stockId)}" data-vrm="${esc(vrm)}" title="Notes On ${esc(vrm || 'This Vehicle')}">${buttonInner(stockId)}</button>`;
    }

    // The same bubble beside a changed site (and the same Neutral colour on the
    // cell). Opens the same history.
    function siteButton(stockId, vrm, title) {
        return `<button type="button" class="vc-btn vc-bubble vc-site" data-stock="${esc(stockId)}" data-vrm="${esc(vrm)}" title="${esc(title)}">${buttonInner(stockId)}</button>`;
    }

    function ensureModal() {
        if (modal) return;
        modal = document.createElement('div');
        modal.className = 'modal-overlay';
        modal.id = 'vcModal';
        modal.innerHTML = `
            <div class="modal-box vc-box">
                <div class="chart-modal-head">
                    <h2 id="vcTitle"></h2>
                    <button type="button" class="modal-close-x" data-vc="close" aria-label="Close">&times;</button>
                </div>
                <div class="modal-error" id="vcError"></div>
                <div class="vc-success" id="vcSuccess"></div>
                <div class="vc-history" id="vcHistory"></div>
                <div class="vc-composer" id="vcComposer"></div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', onClick);
        // A name picked from the @ list keeps the cursor in the box it goes into.
        modal.addEventListener('mousedown', e => { if (e.target.closest('.vc-suggest-item')) e.preventDefault(); });
        modal.addEventListener('input', e => {
            if (!e.target.classList.contains('vc-text')) return;
            activeBox = e.target;
            showSuggest(e.target);
        });
    }

    function showError(message) {
        const box = document.getElementById('vcError');
        box.textContent = message || '';
        box.classList.toggle('show', !!message);
    }

    // The green message after something is saved. '' hides it.
    function showSuccess(message) {
        const box = document.getElementById('vcSuccess');
        box.textContent = message || '';
        box.classList.toggle('show', !!message);
    }

    async function reload() {
        const data = await call('/api/vehicle-comments?stock_id=' + encodeURIComponent(ctx.stockId));
        ctx.data = data;
        counts[ctx.stockId] = data.comments.length + data.site_changes.length;
        render();
        showCounts();
    }

    async function open({ stockId, vrm }) {
        ensureModal();
        ctx = { stockId: String(stockId), vrm: vrm || '', data: null, replyTo: null, editing: null };
        document.getElementById('vcTitle').textContent = `Notes - ${ctx.vrm || 'Vehicle'}`;
        document.getElementById('vcHistory').innerHTML = '<div class="vc-empty">Loading...</div>';
        document.getElementById('vcComposer').innerHTML = '';
        showError('');
        showSuccess('');
        modal.classList.add('open');
        try { await reload(); } catch (e) { showError(e.message); }
    }

    function close() {
        if (modal) modal.classList.remove('open');
        ctx = null;
    }

    // @name in a note, picked out.
    const bodyHtml = text => esc(text).replace(/(^|\s)(@[A-Za-z0-9._\-]+(?:@[A-Za-z0-9.\-]+)?)/g, '$1<span class="vc-mention">$2</span>');

    function head(c) {
        return `<div class="vc-head"><span class="vc-author">${esc(c.author_name)}</span>
            <span class="vc-when">${esc(when(c.created_at))}${c.edited_at ? ' (Edited)' : ''}</span></div>`;
    }

    function commentHtml(c, isReply) {
        const mine = c.author === ctx.data.me;
        const canDelete = mine || ctx.data.is_admin;
        if (ctx.editing === c.id) {
            return `<div class="vc-comment${isReply ? ' reply' : ''}">${head(c)}
                <textarea class="vc-text" id="vcEdit" maxlength="2000">${esc(c.body)}</textarea>
                                <div class="vc-actions">
                    <button type="button" class="btn btn-sm btn-primary" data-vc="save-edit" data-id="${c.id}">Save</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-vc="cancel">Cancel</button>
                </div></div>`;
        }
        const resolveBtn = !isReply && (mine || c.assigned_to === ctx.data.me || ctx.data.is_admin)
            ? `<button type="button" class="vc-link" data-vc="resolve" data-id="${c.id}" data-resolved="${c.resolved_at ? 0 : 1}">${c.resolved_at ? 'Reopen' : 'Resolve'}</button>` : '';
        return `<div class="vc-comment${isReply ? ' reply' : ''}">${head(c)}
            <div class="vc-body">${bodyHtml(c.body)}</div>
            ${c.assigned_to_name ? `<div class="vc-assigned">Assigned To ${esc(c.assigned_to_name)}</div>` : ''}
            <div class="vc-actions">
                ${!isReply ? `<button type="button" class="vc-link" data-vc="reply" data-id="${c.id}">Reply</button>` : ''}
                ${resolveBtn}
                ${mine ? `<button type="button" class="vc-link" data-vc="edit" data-id="${c.id}">Edit</button>` : ''}
                ${canDelete ? `<button type="button" class="vc-link" data-vc="delete" data-id="${c.id}">Delete</button>` : ''}
            </div></div>`;
    }

    function threadHtml(t) {
        const replies = ctx.data.comments.filter(c => c.parent_id === t.id);
        return `<div class="vc-thread${t.resolved_at ? ' resolved' : ''}">
                ${commentHtml(t, false)}
                ${replies.map(r => commentHtml(r, true)).join('')}
                ${t.resolved_at ? `<div class="vc-resolved">Resolved By ${esc(t.resolved_by_name || t.resolved_by)} On ${esc(when(t.resolved_at))}</div>` : ''}
                ${ctx.replyTo === t.id ? `<div class="vc-comment reply">
                    <textarea class="vc-text" id="vcReply" maxlength="2000" placeholder="Reply"></textarea>
                    <div class="vc-actions">
                        <button type="button" class="btn btn-sm btn-primary" data-vc="send-reply" data-id="${t.id}">Save Reply</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-vc="cancel">Cancel</button>
                    </div></div>` : ''}
            </div>`;
    }

    function siteChangeHtml(s) {
        return `<div class="vc-thread vc-site-change${s.removed_at ? ' resolved' : ''}">
                <div class="vc-head"><span class="vc-author">${esc(s.changed_by_name)}</span><span class="vc-when">${esc(when(s.changed_at))}</span></div>
                <div class="vc-site-line">Site Changed: ${esc(s.old_site || 'Unknown')} To ${esc(s.new_site)}</div>
                <div class="vc-body">${esc(s.reason)}</div>
                ${s.removed_at ? `<div class="vc-resolved">Removed By ${esc(s.removed_by_name || s.removed_by)} On ${esc(when(s.removed_at))}</div>` : ''}
            </div>`;
    }

    function render() {
        clearSuggest();
        const items = [
            ...ctx.data.comments.filter(c => c.parent_id === null).map(t => ({ at: t.created_at, html: threadHtml(t) })),
            ...ctx.data.site_changes.map(s => ({ at: s.changed_at, html: siteChangeHtml(s) })),
        ].sort((a, b) => new Date(a.at) - new Date(b.at));
        document.getElementById('vcHistory').innerHTML = items.length
            ? items.map(i => i.html).join('') : '<div class="vc-empty">No notes on this vehicle yet.</div>';
        // What is being typed stays through a redraw.
        const draft = (document.getElementById('vcNew') || {}).value || '';
        document.getElementById('vcComposer').innerHTML = `
            <textarea class="vc-text" id="vcNew" maxlength="2000" placeholder="Add A Note. Type @ And A Name To Mention Someone"></textarea>
                        <div class="vc-actions">
                <label class="vc-assign"><input type="checkbox" id="vcAssign"> Assign To The First Person Mentioned</label>
                <button type="button" class="btn btn-sm btn-primary" data-vc="add">Save Note</button>
            </div>`;
        document.getElementById('vcNew').value = draft;
    }

    // @ names: once a name starts after an @, the people it matches pop up under
    // that box. Picking one puts the full @name in. Nothing shows otherwise.
    function mentionQuery(box) {
        const before = box.value.slice(0, box.selectionStart ?? box.value.length);
        const m = before.match(/(^|\s)@([A-Za-z0-9._\-]*)$/);
        return m ? { at: before.length - m[2].length - 1, text: m[2].toLowerCase() } : null;
    }

    function clearSuggest() {
        document.querySelectorAll('.vc-suggest').forEach(s => s.remove());
    }

    function showSuggest(box) {
        clearSuggest();
        const q = mentionQuery(box);
        if (!q || !ctx || !ctx.data) return;
        const people = ctx.data.people
            .filter(p => p.username.toLowerCase().startsWith(q.text) || p.name.toLowerCase().includes(q.text))
            .slice(0, 6);
        if (!people.length) return;
        const list = document.createElement('div');
        list.className = 'vc-suggest';
        list.innerHTML = people.map(p =>
            `<button type="button" class="vc-suggest-item" data-vc="pick" data-user="${esc(p.username)}">${esc(p.name)} <span class="vc-suggest-user">${esc(p.username)}</span></button>`).join('');
        box.insertAdjacentElement('afterend', list);
    }

    function pickMention(username) {
        const box = activeBox;
        if (!box) return;
        const q = mentionQuery(box);
        const end = box.selectionStart ?? box.value.length;
        const at = q ? q.at : end;
        const text = `@${username} `;
        box.value = box.value.slice(0, at) + text + box.value.slice(end);
        box.focus();
        box.setSelectionRange(at + text.length, at + text.length);
        clearSuggest();
    }

    async function onClick(e) {
        if (e.target === modal) return close();
        const el = e.target.closest('[data-vc]');
        if (!el || !ctx) return;
        const id = Number(el.dataset.id);
        const action = el.dataset.vc;
        showError('');
        showSuccess('');
        try {
            if (action === 'close') return close();
            if (action === 'pick') return pickMention(el.dataset.user);
            if (action === 'reply') { ctx.replyTo = id; ctx.editing = null; return render(); }
            if (action === 'edit') { ctx.editing = id; ctx.replyTo = null; return render(); }
            if (action === 'cancel') { ctx.replyTo = null; ctx.editing = null; return render(); }
            if (action === 'add') {
                await call('/api/vehicle-comments', {
                    stock_id: ctx.stockId, vrm: ctx.vrm, page: page(), body: document.getElementById('vcNew').value,
                    assign: document.getElementById('vcAssign').checked,
                });
                document.getElementById('vcNew').value = '';
            } else if (action === 'send-reply') {
                await call('/api/vehicle-comments', {
                    stock_id: ctx.stockId, vrm: ctx.vrm, page: page(), parent_id: id, body: document.getElementById('vcReply').value,
                });
                ctx.replyTo = null;
            } else if (action === 'save-edit') {
                await call(`/api/comments/${id}/edit`, { body: document.getElementById('vcEdit').value, vrm: ctx.vrm, page: page() });
                ctx.editing = null;
            } else if (action === 'resolve') {
                await call(`/api/comments/${id}/resolve`, { resolved: el.dataset.resolved === '1' });
            } else if (action === 'delete') {
                if (!confirm('Delete this note? A thread takes its replies with it.')) return;
                await call(`/api/comments/${id}/delete`, {});
            } else return;
            await reload();
            showSuccess({
                'add': 'Note saved successfully',
                'send-reply': 'Reply saved successfully',
                'save-edit': 'Note updated successfully',
                'resolve': el.dataset.resolved === '1' ? 'Thread marked as resolved' : 'Thread reopened',
                'delete': 'Note deleted',
            }[action]);
        } catch (err) {
            showError(err.message);
        }
    }

    // A click on any button made by button() or siteButton() opens its vehicle.
    document.addEventListener('click', e => {
        const btn = e.target.closest('.vc-btn[data-stock]');
        if (btn) open({ stockId: btn.dataset.stock, vrm: btn.dataset.vrm });
    });

    document.addEventListener('DOMContentLoaded', () => {
        loadCounts();
        const params = new URLSearchParams(location.search);
        if (params.get('vehicle')) open({ stockId: params.get('vehicle'), vrm: params.get('vrm') || '' });
    });

    return { open, close, button, siteButton, count, loadCounts };
})();
