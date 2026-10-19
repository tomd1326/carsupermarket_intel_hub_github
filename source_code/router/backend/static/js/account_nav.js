/* Keeps page content clear of the top-right account strip (.account-nav).
 *
 * The strip's width changes: live users circles come and go, names differ in
 * length, and the Settings cog is admin only. A fixed gap cannot cover every
 * case, so this measures the strip and sets --account-nav-space on <html>:
 * the distance from the right edge of the window to the strip's left edge,
 * plus a small gap. .account-nav-clear (theme.css) pads by that amount, so a
 * top row such as Orders & Deliveries' font controls always stops before it.
 */
(function () {
    // A page that calls account_nav twice loads this twice.
    if (window.accountNavSpaceStarted) return;
    window.accountNavSpaceStarted = true;
    const GAP_PX = 16;

    function measure() {
        // Use the first strip that is actually drawn at the top right.
        const nav = Array.from(document.querySelectorAll('.account-nav'))
            .find(el => getComputedStyle(el).position === 'absolute');
        if (!nav) return;
        const space = document.documentElement.clientWidth - nav.getBoundingClientRect().left + GAP_PX;
        document.documentElement.style.setProperty('--account-nav-space', space + 'px');
    }

    function start() {
        measure();
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(measure);
            document.querySelectorAll('.account-nav').forEach(el => ro.observe(el));
        }
        window.addEventListener('resize', measure);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
