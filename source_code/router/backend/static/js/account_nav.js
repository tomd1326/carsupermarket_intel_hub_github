/* Opens and closes the two menus drawn by page_header (_navigation.html):
 *
 *  - the account menu: the email button at the top right and the panel under it
 *  - the page menu: the button at the top left and the panel down the left of
 *    the window. The panel is drawn after the header rather than inside it, so
 *    it is paired with its button here, and has its own close button.
 *
 * For both: clicking outside or pressing Escape closes the menu, and Escape
 * (or the page menu's close button) puts focus back on the button that opened
 * it. Opening the page menu moves focus to its first link.
 */
(function () {
    // Guard against the script being loaded twice on one page.
    if (window.accountNavStarted) return;
    window.accountNavStarted = true;

    function startAccountMenus() {
        document.querySelectorAll('.account-menu').forEach((menu, i) => {
            const btn = menu.querySelector('.account-menu-btn');
            const panel = menu.querySelector('.account-menu-panel');
            if (!btn || !panel) return;
            const setOpen = open => {
                panel.hidden = !open;
                btn.setAttribute('aria-expanded', open ? 'true' : 'false');
                menu.classList.toggle('open', open);
            };
            panel.id = 'account-menu-panel-' + i;
            btn.setAttribute('aria-controls', panel.id);
            btn.addEventListener('click', () => setOpen(panel.hidden));
            document.addEventListener('click', e => { if (!menu.contains(e.target)) setOpen(false); });
            document.addEventListener('keydown', e => {
                if (e.key !== 'Escape' || panel.hidden) return;
                setOpen(false);
                btn.focus();
            });
        });
    }

    function startPageMenu() {
        const btn = document.querySelector('.side-menu-btn');
        const panel = document.querySelector('.side-menu-panel');
        if (!btn || !panel) return;
        const close = panel.querySelector('.side-menu-close');
        const setOpen = (open, refocus) => {
            if (open) {
                // Start the panel just below the header so the burger stays visible.
                const head = document.querySelector('.page-head');
                panel.style.top = (head ? Math.max(0, head.getBoundingClientRect().bottom) : 0) + 'px';
            }
            panel.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) {
                const first = panel.querySelector('.side-menu-item');
                if (first) first.focus();
            } else if (refocus) {
                btn.focus();
            }
        };
        panel.id = 'side-menu-panel';
        btn.setAttribute('aria-controls', panel.id);
        btn.addEventListener('click', () => setOpen(panel.hidden, true));
        if (close) close.addEventListener('click', () => setOpen(false, true));
        document.addEventListener('click', e => {
            if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) setOpen(false, false);
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape' && !panel.hidden) setOpen(false, true);
        });
    }

    function start() {
        startAccountMenus();
        startPageMenu();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
