// Sends any error the browser hits on a page to the server log, so it shows in
// the terminal / CloudWatch and not only in the browser console. Added to the top
// of <head> on every page by routers/core.py, so it is running before any page's
// own script, and still reports an error that stops that script loading at all.
(function () {
    const script = document.currentScript;
    const url = (script && script.dataset.base || '') + '/api/client-error';
    let sent = 0;
    function report(message, stack) {
        if (sent++ >= 10) return;  // one fault in a loop must not flood the log
        fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page: location.pathname, message: message, stack: stack || '' }),
        }).catch(() => {});
    }
    window.addEventListener('error', function (e) {
        // A failed <script>/<link>/<img> load has no message, only a target.
        if (!e.message && e.target && e.target !== window) {
            report('Could not load ' + (e.target.src || e.target.href || e.target.tagName));
            return;
        }
        report(e.message + ' (' + (e.filename || 'unknown file') + ' line ' + e.lineno + ':' + e.colno + ')',
               e.error && e.error.stack);
    }, true);
    window.addEventListener('unhandledrejection', function (e) {
        const r = e.reason || {};
        report('Unhandled promise error: ' + (r.message || String(r)), r.stack);
    });
})();
