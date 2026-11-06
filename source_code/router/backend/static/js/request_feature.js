/* Request A Feature: the popup opened from the account menu (and the page menu)
   on every page. The page list comes from the server and holds only the pages
   this user may open; the server checks the choice again on submit. While it
   sends, the popup is greyed and locked and the button says "Sending...". */
(function () {
    if (window.requestFeatureStarted) return;
    window.requestFeatureStarted = true;

    function start() {
        const overlay = document.getElementById('rf-overlay');
        if (!overlay) return;
        const base = overlay.dataset.base || '';
        const box = overlay.querySelector('.rf-box');
        const $ = id => document.getElementById(id);
        const submitBtn = $('rf-submit');
        let opener = null;
        let busy = false;

        function show(id, text) {
            $(id).textContent = text || '';
            $(id).hidden = !text;
        }

        function setBusy(on) {
            busy = on;
            box.classList.toggle('busy', on);
            submitBtn.disabled = on;
            submitBtn.textContent = on ? 'Sending...' : 'Submit';
        }

        async function fillChoices() {
            const select = $('rf-page');
            select.textContent = '';
            select.disabled = true;
            const resp = await fetch(base + '/request-feature/options');
            let data = {};
            try { data = await resp.json(); } catch (e) { /* the message below covers it */ }
            if (!resp.ok) throw new Error(data.detail || `The server answered ${resp.status}`);
            const choose = document.createElement('option');
            choose.value = '';
            choose.textContent = 'Choose...';
            choose.disabled = true;
            choose.selected = true;
            select.appendChild(choose);
            const general = document.createElement('option');
            general.value = '__general__';
            general.textContent = data.general_label;
            select.appendChild(general);
            data.pages.forEach(page => {
                const option = document.createElement('option');
                option.value = page.key;
                option.textContent = page.name;
                select.appendChild(option);
            });
            select.disabled = false;
            $('rf-intro').textContent = data.intro || '';
        }

        async function open(trigger) {
            opener = trigger;
            show('rf-error', '');
            show('rf-done', '');
            $('rf-title-box').value = '';
            $('rf-title-box').disabled = false;
            $('rf-text').value = '';
            $('rf-text').disabled = false;
            submitBtn.hidden = false;
            $('rf-cancel').textContent = 'Cancel';
            setBusy(false);
            overlay.hidden = false;
            try {
                await fillChoices();
                $('rf-page').focus();
            } catch (e) {
                show('rf-error', 'Could not load the page list: ' + e.message);
            }
        }

        function close() {
            if (busy) return;
            overlay.hidden = true;
            if (opener) opener.focus();
        }

        async function submit() {
            show('rf-error', '');
            const page = $('rf-page').value;
            const title = $('rf-title-box').value.trim();
            const text = $('rf-text').value.trim();
            if (!page) { show('rf-error', 'Pick the page it is about, or General.'); return; }
            if (!title) { show('rf-error', 'Give your request a short title.'); return; }
            if (!text) { show('rf-error', 'Describe what you would like.'); return; }
            setBusy(true);
            try {
                const resp = await fetch(base + '/request-feature/submit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ page_key: page === '__general__' ? '' : page, title, text }),
                });
                let data = {};
                try { data = await resp.json(); } catch (e) { /* the message below covers it */ }
                if (!resp.ok) throw new Error(data.detail || `The server answered ${resp.status}`);
                if (data.status === 'error') throw new Error(data.message);
                setBusy(false);
                show('rf-done', data.message);
                $('rf-title-box').disabled = true;
                $('rf-text').disabled = true;
                submitBtn.hidden = true;
                $('rf-cancel').textContent = 'Close';
            } catch (e) {
                setBusy(false);
                show('rf-error', e.message);
            }
        }

        document.querySelectorAll('.request-feature-item').forEach(item => {
            item.addEventListener('click', e => {
                e.preventDefault();
                open(item);
            });
        });
        submitBtn.addEventListener('click', submit);
        $('rf-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
        document.addEventListener('keydown', e => { if (e.key === 'Escape' && !overlay.hidden) close(); });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
