/* Page settings popups: sections down the left, one showing on the right. The
 * look is in theme.css. A page only writes its sections:
 *
 *   <div class="settings-body" data-auto-tabs>
 *       <nav class="settings-tabs"></nav>
 *       <div class="settings-panes">
 *           <div class="settings-pane" data-title="Columns">...</div>
 *           <div class="settings-pane" data-title="Filters">...</div>
 *       </div>
 *   </div>
 *
 * The left-hand list is built from data-title, with the first section open.
 * KPI and Weekly Business Review use the same look but open their own tabs in
 * kpi.js, because they also have sub-sections.
 */
(function () {
    function build(body) {
        const nav = body.querySelector(':scope > .settings-tabs');
        const panes = [...body.querySelectorAll(':scope > .settings-panes > .settings-pane')];
        const buttons = panes.map(pane => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = pane.dataset.title;
            btn.dataset.tab = pane.id;
            btn.addEventListener('click', () => {
                open(pane);
                // A section always opens at its top, whichever part of the popup scrolls.
                for (let el = body; el && el !== document.body; el = el.parentElement) el.scrollTop = 0;
            });
            nav.appendChild(btn);
            return btn;
        });
        function open(pane) {
            panes.forEach((p, i) => {
                p.classList.toggle('active', p === pane);
                buttons[i].classList.toggle('active', p === pane);
            });
            pane.dispatchEvent(new Event('settings-pane-open'));
        }
        if (panes.length) open(panes[0]);
    }

    function start() {
        document.querySelectorAll('.settings-body[data-auto-tabs]').forEach(build);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
