/* "Sign In To AWS" button for pages that fail because the AWS session expired.
 *
 *   awsLoginIfExpired(containerElement, errorMessage)
 *
 * Does nothing unless the message is the expired-credentials one. Otherwise it
 * adds a button to the container. Clicking it asks the server to run
 * `aws sso login` (only possible when the app runs on your own computer), then
 * checks every 2 seconds and reloads the page once the sign-in has finished.
 */
(function () {
    // The app's base path (e.g. /dev), taken from where this script was loaded.
    const BASE = new URL(document.currentScript.src).pathname.replace(/\/static\/js\/aws_login\.js$/, '');

    function isExpired(message) {
        return /aws (sso|credentials)|sso session|credentials expired/i.test(message || '');
    }

    window.awsLoginIfExpired = function (container, message) {
        if (!container || !isExpired(message) || container.querySelector('.aws-login-btn')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-primary btn-sm aws-login-btn ms-2';
        btn.innerHTML = '<i class="fas fa-key me-1"></i>Sign In To AWS';
        const note = document.createElement('span');
        note.className = 'ms-2';
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            note.textContent = 'Complete the sign-in in the browser window that opens...';
            try {
                const r = await fetch(BASE + '/api/aws-sso-login', { method: 'POST' });
                // An error page comes back as HTML, not JSON, so read it as text first.
                const text = await r.text();
                let j = {};
                try { j = JSON.parse(text); } catch (e) { /* not JSON */ }
                if (!r.ok) {
                    throw new Error(j.detail || (r.status === 404
                        ? 'The sign-in route was not found - restart the app so it picks up the new code.'
                        : 'Could not start the sign-in (error ' + r.status + ').'));
                }
            } catch (e) {
                note.textContent = e.message;
                btn.disabled = false;
                return;
            }
            const timer = setInterval(async () => {
                try {
                    const s = await (await fetch(BASE + '/api/aws-sso-login')).json();
                    if (s.status === 'ok') { clearInterval(timer); location.reload(); }
                    else if (s.status === 'failed') {
                        clearInterval(timer);
                        note.textContent = 'The sign-in did not finish. Try again.';
                        btn.disabled = false;
                    }
                } catch (e) { /* keep waiting */ }
            }, 2000);
        });
        container.appendChild(btn);
        container.appendChild(note);
    };
})();
