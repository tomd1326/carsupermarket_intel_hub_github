/* Orders & Deliveries - Settings popup (admin only).
 *
 * Two tabs, one Save:
 *   Bands        - upper limits for the price, mileage and age bands
 *                  (utils/od_bands.py builds the labels and bands each row)
 *   Highlighting - which summary columns are coloured, which way round, the
 *                  colour scheme and the shading options (utils/od_shading.py)
 * Both are saved in kpi_layout, so they apply to everyone.
 *
 * Loaded after orders_deliveries.js, whose SUMMARY_COLS, applyShadingSettings()
 * and load() this uses.
 */
(function () {
    const URL_SETTINGS = BASE_URL + '/reports/orders-deliveries/settings';
    const URL_PREVIEW = BASE_URL + '/reports/orders-deliveries/bands/preview';
    const BAND_KINDS = ['price', 'mileage', 'age'];
    const BAND_HELP = {
        price: 'Pounds. A price exactly on a limit goes in the lower band. Example: 10000, 12500, 15000',
        mileage: 'Miles. A mileage exactly on a limit goes in the higher band. Example: 10000, 20000, 30000',
        age: 'Whole years. A car exactly on a limit goes in the older band. Example: 1, 2, 3, 4, 5, 6, 8',
    };

    const modal = document.getElementById('od-settings-modal');
    const errorEl = document.getElementById('od-settings-error');
    const noteEl = document.getElementById('od-settings-note');
    let defaults = null, previewTimer = null;

    const $ = id => document.getElementById(id);
    function showError(msg) {
        errorEl.textContent = msg || '';
        errorEl.classList.toggle('show', !!msg);
    }

    // ── Tabs ────────────────────────────────────────────────────────────────
    modal.querySelectorAll('.od-settings-tabs button').forEach(btn => {
        btn.addEventListener('click', () => {
            modal.querySelectorAll('.od-settings-tabs button').forEach(b => b.classList.toggle('active', b === btn));
            modal.querySelectorAll('.od-settings-pane').forEach(p => p.classList.toggle('active', p.id === btn.dataset.tab));
        });
    });
    const activeTab = () => modal.querySelector('.od-settings-tabs button.active').dataset.tab;

    // ── Bands tab ───────────────────────────────────────────────────────────
    function buildBandRows(titles) {
        const host = $('od-bands-rows');
        host.innerHTML = '';
        BAND_KINDS.forEach(k => {
            const div = document.createElement('div');
            div.className = 'mb-4';
            div.innerHTML = `<label class="od-label" for="od-band-${k}"></label>
                <input type="text" class="form-control form-control-sm" id="od-band-${k}">
                <div class="od-hint">${BAND_HELP[k]}</div>
                <div class="od-band-labels" id="od-band-${k}-labels"></div>`;
            div.querySelector('label').textContent = titles[k];
            div.querySelector('input').addEventListener('input', () => {
                clearTimeout(previewTimer);
                previewTimer = setTimeout(previewBands, 400);
            });
            host.appendChild(div);
        });
    }
    function readBands() {
        const out = {};
        BAND_KINDS.forEach(k => {
            out[k] = $('od-band-' + k).value.split(',').map(s => s.trim()).filter(Boolean)
                .map(s => (isNaN(Number(s)) ? s : Number(s)));
        });
        return out;
    }
    function showBandLabels(labels) {
        BAND_KINDS.forEach(k => {
            const host = $('od-band-' + k + '-labels');
            host.innerHTML = '';
            ((labels && labels[k]) || []).forEach(l => {
                const s = document.createElement('span');
                s.textContent = l;
                host.appendChild(s);
            });
        });
    }
    function fillBands(limits, labels) {
        BAND_KINDS.forEach(k => { $('od-band-' + k).value = limits[k].join(', '); });
        showBandLabels(labels);
    }
    async function previewBands() {
        try {
            const resp = await fetch(URL_PREVIEW, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(readBands()),
            });
            const d = await resp.json();
            if (d.status === 'ok') { showBandLabels(d.labels); showError(''); }
            else { showBandLabels(null); showError(d.message); }
        } catch (e) { showError(String(e.message || e)); }
    }

    // ── Highlighting tab ────────────────────────────────────────────────────
    // One row per summary column: highlight direction, its colour scheme (the
    // default or its own), that scheme's colour boxes, and a preview strip.
    // A column's own colour boxes start from the default's current colours.
    function buildColumnRows(columns, def) {
        const body = $('od-shade-cols');
        body.innerHTML = '';
        SUMMARY_COLS.forEach(c => {
            const saved = columns[c.key] || {};
            const col = { rag_low: def.rag_low, rag_mid: def.rag_mid, rag_high: def.rag_high,
                          single_colour: def.single_colour, ...saved };
            const tr = document.createElement('tr');
            tr.dataset.col = c.key;
            tr.innerHTML = `<td></td>
                <td><select class="form-select form-select-sm od-c-dir" style="width: 170px;">
                    <option value="">Not Highlighted</option>
                    <option value="high-good">Higher Is Better</option>
                    <option value="high-bad">Higher Is Worse</option>
                </select></td>
                <td><select class="form-select form-select-sm od-c-scheme" style="width: 190px;">
                    <option value="default">Use Default</option>
                    <option value="rag">Red, Amber, Green</option>
                    <option value="single">Shades Of One Colour</option>
                </select></td>
                <td class="od-c-colours">
                    <span class="od-c-rag d-flex gap-1">
                        <input type="color" class="form-control form-control-color od-c-low" value="${col.rag_low}">
                        <input type="color" class="form-control form-control-color od-c-mid" value="${col.rag_mid}">
                        <input type="color" class="form-control form-control-color od-c-high" value="${col.rag_high}">
                    </span>
                    <span class="od-c-single"><input type="color" class="form-control form-control-color od-c-one" value="${col.single_colour}"></span>
                </td>
                <td><div class="od-swatch od-c-swatch" style="width: 120px;"></div></td>`;
            tr.querySelector('td').textContent = c.label;
            tr.querySelector('.od-c-dir').value = saved.direction || '';
            tr.querySelector('.od-c-scheme').value = saved.scheme || 'default';
            tr.querySelectorAll('select, input').forEach(el => el.addEventListener('input', () => updateColumnRow(tr)));
            body.appendChild(tr);
            updateColumnRow(tr);
        });
    }
    // The scheme this row actually uses, as an object makeColourFn() accepts.
    function rowScheme(tr) {
        const scheme = tr.querySelector('.od-c-scheme').value;
        if (scheme === 'default') return readDefaultScheme();
        return { scheme, rag_low: tr.querySelector('.od-c-low').value, rag_mid: tr.querySelector('.od-c-mid').value,
                 rag_high: tr.querySelector('.od-c-high').value, single_colour: tr.querySelector('.od-c-one').value };
    }
    function stripGradient(s) {
        const fn = makeColourFn(s);
        return `linear-gradient(to right, ${[0, 0.25, 0.5, 0.75, 1].map(r => `${fn(r)} ${r * 100}%`).join(', ')})`;
    }
    function updateColumnRow(tr) {
        const on = !!tr.querySelector('.od-c-dir').value;
        const scheme = tr.querySelector('.od-c-scheme').value;
        tr.querySelector('.od-c-scheme').disabled = !on;
        tr.querySelector('.od-c-rag').style.display = on && scheme === 'rag' ? '' : 'none';
        tr.querySelector('.od-c-single').style.display = on && scheme === 'single' ? '' : 'none';
        const swatch = tr.querySelector('.od-c-swatch');
        swatch.style.visibility = on ? '' : 'hidden';
        if (on) swatch.style.background = stripGradient(rowScheme(tr));
    }
    function readDefaultScheme() {
        return { scheme: $('od-scheme').value, rag_low: $('od-rag-low').value, rag_mid: $('od-rag-mid').value,
                 rag_high: $('od-rag-high').value, single_colour: $('od-single-colour').value };
    }
    function fillShading(s) {
        $('od-scheme').value = s.scheme;
        $('od-rag-low').value = s.rag_low;
        $('od-rag-mid').value = s.rag_mid;
        $('od-rag-high').value = s.rag_high;
        $('od-single-colour').value = s.single_colour;
        $('od-strength').value = s.strength;
        $('od-day-cap').value = s.day_cap;
        $('od-min-rows').value = s.min_rows;
        $('od-shade-sales-type').checked = s.shade_sales_type;
        $('od-shade-sub-rows').checked = s.shade_sub_rows;
        $('od-shade-popup').checked = s.shade_popup;
        buildColumnRows(s.columns, s);
        updateSchemeView();
    }
    function readShading() {
        const columns = {};
        modal.querySelectorAll('#od-shade-cols tr').forEach(tr => {
            const direction = tr.querySelector('.od-c-dir').value;
            if (!direction) return;
            const scheme = tr.querySelector('.od-c-scheme').value;
            const col = { direction, scheme };
            if (scheme === 'rag') {
                col.rag_low = tr.querySelector('.od-c-low').value;
                col.rag_mid = tr.querySelector('.od-c-mid').value;
                col.rag_high = tr.querySelector('.od-c-high').value;
            } else if (scheme === 'single') {
                col.single_colour = tr.querySelector('.od-c-one').value;
            }
            columns[tr.dataset.col] = col;
        });
        return {
            columns,
            scheme: $('od-scheme').value,
            rag_low: $('od-rag-low').value, rag_mid: $('od-rag-mid').value, rag_high: $('od-rag-high').value,
            single_colour: $('od-single-colour').value,
            strength: Number($('od-strength').value),
            day_cap: Number($('od-day-cap').value),
            min_rows: Number($('od-min-rows').value),
            shade_sales_type: $('od-shade-sales-type').checked,
            shade_sub_rows: $('od-shade-sub-rows').checked,
            shade_popup: $('od-shade-popup').checked,
        };
    }
    // Shows only the colour boxes for the chosen scheme, and a strip of the
    // scale from worst to best using the same colour function the tables use.
    function updateSchemeView() {
        const single = $('od-scheme').value === 'single';
        modal.querySelectorAll('.od-rag-only').forEach(el => { el.style.display = single ? 'none' : ''; });
        modal.querySelectorAll('.od-single-only').forEach(el => { el.style.display = single ? '' : 'none'; });
        $('od-swatch').style.background = stripGradient(readDefaultScheme());
        // Columns on Use Default follow the default, so their strips change too.
        modal.querySelectorAll('#od-shade-cols tr').forEach(updateColumnRow);
    }
    ['od-scheme', 'od-rag-low', 'od-rag-mid', 'od-rag-high', 'od-single-colour'].forEach(id =>
        $(id).addEventListener('input', updateSchemeView));

    // ── Open, save, reset ───────────────────────────────────────────────────
    window.openOdSettings = async function () {
        showError('');
        noteEl.textContent = 'Loading...';
        modal.classList.add('open');
        try {
            const resp = await fetch(URL_SETTINGS);
            const d = await resp.json();
            if (d.status !== 'ok') throw new Error(d.message || 'Could not load the settings');
            defaults = { bands: d.bands.defaults, shading: d.shading.defaults };
            buildBandRows(d.bands.titles);
            fillBands(d.bands.limits, d.bands.labels);
            fillShading(d.shading.saved);
            noteEl.textContent = '';
        } catch (e) {
            noteEl.textContent = '';
            showError(String(e.message || e));
        }
    };
    window.closeOdSettings = function () { modal.classList.remove('open'); };
    modal.addEventListener('mousedown', e => { if (e.target === modal) closeOdSettings(); });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && modal.classList.contains('open')) closeOdSettings();
    });

    $('od-settings-reset').addEventListener('click', () => {
        if (!defaults) return;
        if (activeTab() === 'od-tab-bands') {
            fillBands(defaults.bands, null);
            previewBands();
        } else {
            fillShading(defaults.shading);
        }
        noteEl.textContent = 'Defaults filled in. Press Save to keep them.';
    });

    $('od-settings-save').addEventListener('click', async () => {
        const btn = $('od-settings-save');
        btn.disabled = true;
        showError('');
        noteEl.textContent = 'Saving...';
        try {
            const resp = await fetch(URL_SETTINGS, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bands: readBands(), shading: readShading() }),
            });
            const d = await resp.json();
            if (d.status !== 'ok') throw new Error(d.message || 'Save failed');
            noteEl.textContent = '';
            closeOdSettings();
            // New highlighting applies at once; the data is fetched again so
            // the rows come back banded with the new limits.
            applyShadingSettings(d.shading);
            load(false);
        } catch (e) {
            noteEl.textContent = '';
            showError(String(e.message || e));
        } finally {
            btn.disabled = false;
        }
    });
})();
