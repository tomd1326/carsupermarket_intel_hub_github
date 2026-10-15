/* Live users circles beside the Home button.
 *
 * Each page load gets its own tab id, so two windows on one login are tracked
 * separately. The tab sends a heartbeat every interval while it is visible,
 * and tells the server straight away when it is hidden or closed. The server
 * (routers/presence.py) decides who is listed; this only draws what it sends.
 *
 * Every message carries seq, one higher than the last. The server ignores a
 * message whose seq is not higher than the one it has, so a heartbeat and a
 * leave that arrive in the wrong order cannot leave the wrong result.
 */
(function () {
    const box = document.getElementById('live-users');
    if (!box) return;

    const base = box.dataset.base || '';
    let intervalMs = Math.max(5, parseInt(box.dataset.interval, 10) || 20) * 1000;
    let maxCircles = Math.max(1, parseInt(box.dataset.max, 10) || 5);
    const heartbeatUrl = base + '/api/presence';
    const leaveUrl = base + '/api/presence/leave';

    const tabId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
    let page = location.pathname.startsWith(base) ? location.pathname.slice(base.length) : location.pathname;
    page = (page || '/').slice(0, 200);

    let seq = 0;
    let timer = null;
    let stopped = false;

    // Same colour for a person every time, on every page.
    function colourFor(username) {
        let h = 0;
        for (let i = 0; i < username.length; i++) h = (h * 31 + username.charCodeAt(i)) >>> 0;
        return 'hsl(' + (h % 360) + ', 55%, 42%)';
    }

    function circle(text, title, colour, extraClass) {
        const el = document.createElement('span');
        el.className = 'live-user' + (extraClass ? ' ' + extraClass : '');
        el.textContent = text;
        el.title = title;
        if (colour) el.style.background = colour;
        return el;
    }

    // Settings > Live Users, sent with every reply so open pages follow changes.
    function applyConfig(c) {
        if (!c) return;
        intervalMs = Math.max(5, parseInt(c.presence_interval_seconds, 10) || 20) * 1000;
        maxCircles = Math.max(1, parseInt(c.presence_max_circles, 10) || 5);
        const s = box.style;
        s.setProperty('--live-user-size', c.presence_circle_size + 'px');
        s.setProperty('--live-user-border-width', c.presence_border_width + 'px');
        s.setProperty('--live-user-gap', c.presence_gap + 'px');
        s.setProperty('--live-user-border-colour', c.presence_border_colour);
        s.setProperty('--live-user-text-colour', c.presence_text_colour);
        s.setProperty('--live-user-more-colour', c.presence_more_colour);
    }

    // The viewer's own circle is always shown and not counted: Max Circles
    // limits other people only, so it can never hide you inside "+N".
    function render(users, me) {
        box.replaceChildren();
        const own = users.filter(u => u.username === me);
        const others = users.filter(u => u.username !== me);
        const shown = others.length > maxCircles ? others.slice(0, maxCircles - 1) : others;
        own.concat(shown).forEach(u => box.appendChild(circle(u.initials, u.name, colourFor(u.username))));
        const rest = others.slice(shown.length);
        if (rest.length) {
            box.appendChild(circle('+' + rest.length, rest.map(u => u.name).join('\n'), null, 'live-user-more'));
        }
    }

    function schedule() {
        clearTimeout(timer);
        if (!stopped && document.visibilityState === 'visible') timer = setTimeout(beat, intervalMs);
    }

    async function beat() {
        clearTimeout(timer);
        const mySeq = ++seq;
        // A later heartbeat or leave was sent while this one was on its way:
        // its reply is out of date, and the later message handles what comes next.
        const superseded = () => mySeq !== seq;
        try {
            const resp = await fetch(heartbeatUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tab_id: tabId, seq: mySeq, page: page }),
                credentials: 'same-origin',
            });
            if (superseded()) return;
            if (resp.status === 401) {          // signed out: nothing more to send
                stopped = true;
                render([]);
                return;
            }
            if (resp.ok) {
                const data = await resp.json();
                if (superseded()) return;
                applyConfig(data.config);
                render(data.users || [], data.me);
            } else {
                render([]);                     // clear rather than leave old circles showing
            }
        } catch (e) {
            if (superseded()) return;
            render([]);
        }
        schedule();
    }

    function leave() {
        clearTimeout(timer);
        const data = JSON.stringify({ tab_id: tabId, seq: ++seq });
        // sendBeacon returns false when the browser will not queue it.
        if (!(navigator.sendBeacon && navigator.sendBeacon(leaveUrl, data))) {
            fetch(leaveUrl, { method: 'POST', body: data, keepalive: true, credentials: 'same-origin' })
                .catch(() => {});
        }
    }

    document.addEventListener('visibilitychange', () => {
        if (stopped) return;
        if (document.visibilityState === 'hidden') {
            leave();
            render([]);
        } else {
            beat();
        }
    });
    window.addEventListener('pagehide', leave);
    // Back/Forward can restore this page from the browser's memory after the
    // leave above; check in again so the circle comes back.
    window.addEventListener('pageshow', e => { if (e.persisted && !stopped) beat(); });

    if (document.visibilityState === 'visible') beat();
})();
