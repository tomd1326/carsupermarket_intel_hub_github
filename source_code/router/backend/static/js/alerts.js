// The bell in the page header (templates/_navigation.html), on every page.
// Lists this user's alerts from /api/alerts (routers/wbr_comments.py): an
// @mention in a Weekly Business Review comment, or a reply to a thread they
// started or were assigned. Someone who has never had an alert sees no bell.
// Clicking an alert marks it read and opens the comment.
(() => {
    const POLL_MS = 120000;

    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    function whenText(iso) {
        const d = new Date(iso);
        const now = new Date();
        const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time + ' Today';
        return time + ' ' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }

    function start() {
        const bell = document.querySelector('.alerts-bell');
        if (!bell) return;
        const base = bell.dataset.base || '';
        const btn = bell.querySelector('.alerts-bell-btn');
        const count = bell.querySelector('.alerts-bell-count');
        const panel = bell.querySelector('.alerts-panel');
        const list = bell.querySelector('.alerts-list');
        let alerts = [];

        const post = body => fetch(base + '/api/alerts/read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        function render() {
            list.innerHTML = alerts.length ? alerts.map(a => `
                <a class="alerts-item ${a.read ? '' : 'unread'}" href="${esc(base + a.link)}" data-id="${a.id}">
                    <span class="alerts-item-text">${esc(a.text)}</span>
                    <span class="alerts-item-time">${esc(whenText(a.created_at))}</span>
                </a>`).join('') : '<div class="alerts-empty">No alerts.</div>';
        }

        function refresh() {
            fetch(base + '/api/alerts')
                .then(r => (r.ok ? r.json() : null))
                .then(d => {
                    if (!d || d.status !== 'ok') return;
                    alerts = d.alerts || [];
                    bell.hidden = !d.total;
                    count.hidden = !d.unread;
                    count.textContent = d.unread > 99 ? '99+' : String(d.unread);
                    btn.title = d.unread ? `${d.unread} Unread Alert${d.unread === 1 ? '' : 's'}` : 'Alerts';
                    if (!panel.hidden) render();
                })
                .catch(() => {});
        }

        btn.addEventListener('click', event => {
            event.stopPropagation();
            panel.hidden = !panel.hidden;
            if (!panel.hidden) render();
        });
        bell.querySelector('.alerts-read-all').addEventListener('click', event => {
            event.stopPropagation();
            post({ all: true }).then(refresh);
        });
        list.addEventListener('click', event => {
            const item = event.target.closest('.alerts-item');
            if (!item) return;
            event.preventDefault();
            const go = () => { location.href = item.getAttribute('href'); };
            post({ ids: [Number(item.dataset.id)] }).then(go, go);
        });
        document.addEventListener('click', event => {
            if (!panel.hidden && !event.target.closest('.alerts-bell')) panel.hidden = true;
        });

        refresh();
        setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
