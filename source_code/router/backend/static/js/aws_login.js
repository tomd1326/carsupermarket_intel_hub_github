/* "Sign In To AWS" in the account menu (top right). Only on the app running
 * on your own computer (see _navigation.html). Clicking it asks the server to
 * run `aws sso login`, which opens the sign-in in a browser, then checks every
 * 2 seconds until it has finished. The page is not reloaded, so nothing on
 * screen is lost: retry whatever failed once it says Signed In.
 */
(function () {
    // The app's base path, taken from where this script was loaded.
    const BASE = new URL(document.currentScript.src).pathname.replace(/\/static\/js\/aws_login\.js$/, '');

    function start(item) {
        const text = item.querySelector('.aws-login-text');
        const set = t => { text.textContent = t; };
        if (item.dataset.busy) return;
        item.dataset.busy = '1';
        set('Signing In - Complete It In The Browser...');
        fetch(BASE + '/api/aws-sso-login', {method: 'POST'})
            .then(async r => {
                // An error page comes back as HTML, not JSON, so read it as text first.
                const body = await r.text();
                let j = {};
                try { j = JSON.parse(body); } catch (e) { /* not JSON */ }
                if (!r.ok) throw new Error(j.detail || 'Could not start the sign-in (error ' + r.status + ')');
                const timer = setInterval(async () => {
                    try {
                        const s = await (await fetch(BASE + '/api/aws-sso-login')).json();
                        if (s.status === 'ok' || s.status === 'failed') {
                            clearInterval(timer);
                            delete item.dataset.busy;
                            set(s.status === 'ok' ? 'Signed In To AWS - Try Again' : 'Sign-In Did Not Finish - Sign In To AWS');
                        }
                    } catch (e) { /* keep waiting */ }
                }, 2000);
            })
            .catch(e => {
                delete item.dataset.busy;
                set('Sign In To AWS (' + e.message + ')');
            });
    }

    document.addEventListener('click', e => {
        const item = e.target.closest('.aws-login-item');
        if (!item) return;
        e.preventDefault();
        start(item);
    });
})();
