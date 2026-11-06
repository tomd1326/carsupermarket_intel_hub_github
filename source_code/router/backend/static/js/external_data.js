/* Settings > External Data (templates/_settings_external_data.html). Uses the
   BASE_URL set just before this file is loaded. The server does the work in small
   steps (scan one page, fetch one file) and this file runs them in order, holding
   the one 'check' lock (POST /lease) for the whole run so two checks never overlap. */
const XD_API = BASE_URL + '/api/external-data';
const XD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const XD_HOW = {
    last_year: 'Restated In A Later File',
    own_file: 'Its Own File',
    tracker: 'Your Tracker',
    worked_out: 'Worked Out From Year To Date',
    missing: 'Missing',
};
const XD_COLOUR_LABELS = { ...XD_HOW, cap_up: 'CAP HPI Rise', cap_down: 'CAP HPI Fall' };
const XD_KIND = {
    marque_sheet: 'By Marque', tracker: 'Monthly Totals', used_sheet: 'Used Car Sales',
    outlook: 'Outlook', news_release: 'News Release', cap_overview: 'Values And Movers',
    cap_future: 'Report Text', other: 'Nothing Read',
};
const XD_BANDS = { '1yr_10k': '1 Yr / 10k', '3yr_60k': '3 Yr / 60k', '5yr_80k': '5 Yr / 80k', '10yr_100k': '10 Yr / 100k' };
let xdData = null;
let xdBusy = false;
let xdSources = [];

function xdEsc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function xdShowError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg || '';
    el.classList.toggle('d-none', !msg);
}

function xdStatus(msg) {
    document.getElementById('xd-status').textContent = msg || '';
}

async function xdCall(url, method = 'GET', body, isForm = false) {
    const opts = { method };
    if (body && isForm) {
        opts.body = body;
    } else if (body) {
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'Failed');
    return data;
}

function xdNum(v) {
    return v === null || v === undefined ? '' : Number(v).toLocaleString('en-GB');
}

function xdPct(v) {
    return v === null || v === undefined ? '' : (v > 0 ? '+' : '') + Number(v).toFixed(1) + '%';
}

function xdGbp(v) {
    if (v === null || v === undefined) return '';
    return (v < 0 ? '-£' : '£') + Math.abs(v).toLocaleString('en-GB');
}

function xdDate(v) {
    if (!v) return '';
    const [d, t] = String(v).replace('T', ' ').split(' ');
    return d.split('-').reverse().join('/') + (t ? ' ' + t.slice(0, 5) : '');
}

function xdMonthLabel(m) {
    return XD_MONTHS[parseInt(m.slice(5, 7), 10) - 1] + ' ' + m.slice(0, 4);
}

// Lock the action buttons while a job runs, and show it on the button pressed.
function xdLock(on, btnId, label) {
    xdBusy = on;
    ['xd-check-btn', 'xd-full-btn', 'xd-s3-btn', 'xd-upload-btn'].forEach(id => {
        const el = document.getElementById(id);
        el.classList.toggle('disabled', on);
        if (el.tagName === 'BUTTON') el.disabled = on;
    });
    document.getElementById('xd-upload').disabled = on;
    if (btnId) {
        const btn = document.getElementById(btnId);
        if (on) { btn.dataset.label = btn.innerHTML; btn.textContent = label; }
        else if (btn.dataset.label) { btn.innerHTML = btn.dataset.label; }
    }
}

async function xdLoad() {
    try {
        xdData = await xdCall(XD_API);
        xdShowError('xd-error', '');
        xdRender();
        return xdData;
    } catch (e) {
        xdShowError('xd-error', 'Could not load the external data: ' + e.message);
        return null;
    }
}

function xdRender() {
    const cfg = xdData.config;
    const c = cfg.colours;
    document.getElementById('xd-legend').innerHTML = Object.keys(XD_HOW).map(k =>
        `<span style="--xd-c: ${xdEsc(c[k])}">${XD_HOW[k]}</span>`).join('');
    const cov = xdData.coverage;
    document.getElementById('xd-grid-totals').innerHTML = xdGrid(cov.totals, c, 'units', null);
    document.getElementById('xd-grid-marques').innerHTML = xdGrid(cov.marques, c, 'marques', 'xdPickMonth');
    document.getElementById('xd-grid-used').innerHTML = xdGrid(cov.used, c, 'units', null);
    document.getElementById('xd-grid-cap').innerHTML = xdCapGrid(cov.cap, c);
    document.getElementById('xd-missing').textContent = xdMissingText(cov);
    const st = xdData.state || {};
    const checked = st.last_checked ? 'Last checked ' + xdDate(st.last_checked) + '.' : 'Not checked yet.';
    const running = xdData.running ? ' A check is running now.' : '';
    xdStatus(checked + ' ' + xdData.files.length + ' files stored.' + running);
    xdRenderFeeds();
    xdRenderFiles();
    xdRenderOutlook();
    xdRenderSettings();
    const month = document.getElementById('xd-month');
    if (!month.value && xdData.latest_month) {
        month.value = xdData.latest_month;
        xdLoadMonth(month.value);
    }
    const capSel = document.getElementById('xd-cap-month');
    const chosen = capSel.value;
    capSel.innerHTML = (xdData.cap_months || []).map(m => `<option value="${m}">${xdMonthLabel(m)}</option>`).join('');
    if (xdData.cap_months && xdData.cap_months.length) {
        capSel.value = chosen && xdData.cap_months.includes(chosen) ? chosen : xdData.cap_months[0];
        if (!chosen) xdLoadCap(capSel.value);
    }
}

function xdGrid(years, colours, what, onClick) {
    if (!years || !years.length) return '<span class="text-muted">No data yet.</span>';
    let html = '<table class="xd-grid"><thead><tr><th></th>' + XD_MONTHS.map(m => `<th>${m}</th>`).join('') + '</tr></thead><tbody>';
    years.forEach(y => {
        html += `<tr><td class="xd-year">${y.year}</td>`;
        y.months.forEach(cell => {
            if (cell.how === 'n/a') { html += '<td class="xd-cell xd-na"></td>'; return; }
            const how = cell.how || 'missing';
            let tip = xdMonthLabel(cell.month) + ': ' + XD_HOW[how];
            if (cell.units) tip += what === 'marques' ? ` (${cell.units} marques)` : ` (${xdNum(cell.units)})`;
            const click = onClick && cell.how ? ` xd-click" onclick="${onClick}('${cell.month}')` : '';
            html += `<td class="xd-cell${click}" style="background: ${xdEsc(colours[how])}" title="${xdEsc(tip)}"></td>`;
        });
        html += '</tr>';
    });
    return html + '</tbody></table>';
}

// The cap hpi month grid shows the 3 year / 60k movement in each cell, coloured rise or fall.
function xdCapGrid(years, colours) {
    if (!years || !years.length) return '<span class="text-muted">No CAP HPI reports yet. Press Full Scan to read past ones.</span>';
    let html = '<table class="xd-grid"><thead><tr><th></th>' + XD_MONTHS.map(m => `<th>${m}</th>`).join('') + '</tr></thead><tbody>';
    years.forEach(y => {
        html += `<tr><td class="xd-year">${y.year}</td>`;
        y.months.forEach(cell => {
            if (cell.how === 'n/a') { html += '<td class="xd-cell xd-na"></td>'; return; }
            if (!cell.how) {
                html += `<td class="xd-cell xd-val" style="background: ${xdEsc(colours.missing)}" title="${xdMonthLabel(cell.month)}: Missing"></td>`;
                return;
            }
            const v = cell.units;
            const bg = v === null || v === undefined ? colours.missing : (v < 0 ? colours.cap_down : colours.cap_up);
            html += `<td class="xd-cell xd-val xd-click" style="background: ${xdEsc(bg)}" title="${xdMonthLabel(cell.month)}"
                onclick="xdPickCap('${cell.month}')">${xdPct(v)}</td>`;
        });
        html += '</tr>';
    });
    return html + '</tbody></table>';
}

function xdMissingText(cov) {
    const list = grid => (grid || []).flatMap(y => y.months.filter(c => !c.how).map(c => xdMonthLabel(c.month)));
    const t = list(cov.totals), m = list(cov.marques), cap = list(cov.cap);
    const parts = [];
    parts.push(t.length ? 'Market total missing: ' + t.join(', ') + '.' : 'Market total: no months missing.');
    parts.push(m.length ? 'By marque missing: ' + m.join(', ') + '.' : 'By marque: no months missing.');
    if (cov.cap && cov.cap.length) parts.push(cap.length ? 'CAP HPI missing: ' + cap.join(', ') + '.' : 'CAP HPI: no months missing.');
    return parts.join(' ');
}

function xdPickMonth(month) {
    document.getElementById('xd-month').value = month;
    xdLoadMonth(month);
}

function xdPickCap(month) {
    document.getElementById('xd-cap-month').value = month;
    xdLoadCap(month);
}

async function xdLoadMonth(month) {
    if (!month) return;
    const body = document.getElementById('xd-month-body');
    body.innerHTML = '<tr><td colspan="5" class="text-muted">Loading...</td></tr>';
    try {
        const d = await xdCall(XD_API + '/month/' + month);
        const tot = d.total, last = d.total_last_year;
        let line = tot ? `Market total ${xdNum(tot.units)} (${XD_HOW[tot.how]})` : 'No market total for this month';
        if (tot && last && last.units) line += `, ${xdChange(tot.units, last.units)} on ${xdNum(last.units)} a year before`;
        if (tot && tot.private) line += `. Private ${xdNum(tot.private)}, fleet ${xdNum(tot.fleet)} (your tracker)`;
        document.getElementById('xd-month-total').textContent = line + '.';
        const rows = d.rows.filter(r => r.units !== null || r.last_year);
        body.innerHTML = rows.length ? rows.map(r => `<tr><td>${xdEsc(r.marque)}</td>
            <td class="num">${xdNum(r.units)}</td><td class="num">${xdNum(r.last_year)}</td>
            <td class="num">${r.units !== null && r.last_year ? xdChange(r.units, r.last_year) : ''}</td>
            <td class="xd-sub">${r.how ? XD_HOW[r.how] : ''}</td></tr>`).join('')
            : '<tr><td colspan="5" class="text-muted">No marque figures for this month.</td></tr>';
    } catch (e) {
        body.innerHTML = `<tr><td colspan="5" class="xd-err">${xdEsc(e.message)}</td></tr>`;
    }
}

function xdChange(now, before) {
    const p = (now - before) / before * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(1) + '%';
}

async function xdLoadCap(month) {
    if (!month) return;
    const sector = document.getElementById('xd-cap-sector');
    sector.innerHTML = '<tbody><tr><td class="text-muted">Loading...</td></tr></tbody>';
    try {
        const d = await xdCall(XD_API + '/cap/' + month);
        const link = document.getElementById('xd-cap-file');
        if (d.file) { link.href = XD_API + '/file/' + d.file.id; link.textContent = 'Download The Report'; }
        else { link.removeAttribute('href'); link.textContent = ''; }
        const table = (name, label) => {
            const segs = [];
            d.movements.filter(m => m.table === name).forEach(m => { if (!segs.includes(m.segment)) segs.push(m.segment); });
            if (!segs.length) return '<tbody><tr><td class="text-muted">Not in this report.</td></tr></tbody>';
            return `<thead><tr><th>${label}</th>` + d.bands.map(b => `<th class="num">${XD_BANDS[b]}</th>`).join('') + '</tr></thead><tbody>'
                + segs.map(s => {
                    const cells = d.bands.map(b => d.movements.find(m => m.table === name && m.segment === s && m.age_band === b));
                    const partial = cells.some(c => c && c.partial) ? ' *' : '';
                    const strong = s.startsWith('Overall') ? ' class="fw-semibold"' : '';
                    return `<tr${strong}><td>${xdEsc(s)}${partial}</td>` + cells.map(c => `<td class="num">${c ? xdPct(c.pct) : ''}</td>`).join('') + '</tr>';
                }).join('') + '</tbody>';
        };
        sector.innerHTML = table('sector', 'Sector');
        document.getElementById('xd-cap-sub').innerHTML = table('sub_sector', 'Sub-Sector');
        const movers = band => {
            const rows = d.movers.filter(m => m.band === band);
            if (!rows.length) return '<tbody><tr><td class="text-muted">Not in this report.</td></tr></tbody>';
            return '<thead><tr><th>Model</th><th class="num">Min</th><th class="num">Max</th><th class="num">Average</th></tr></thead><tbody>'
                + rows.map(m => `<tr><td>${xdEsc(m.model)}</td><td class="num">${xdGbp(m.min)}</td><td class="num">${xdGbp(m.max)}</td>
                    <td class="num">${xdGbp(m.avg)}</td></tr>`).join('') + '</tbody>';
        };
        document.getElementById('xd-cap-movers-1').innerHTML = movers('1yr_20k');
        document.getElementById('xd-cap-movers-3').innerHTML = movers('3yr_60k');
    } catch (e) {
        sector.innerHTML = `<tbody><tr><td class="xd-err">${xdEsc(e.message)}</td></tr></tbody>`;
    }
}

function xdRenderFeeds() {
    const feeds = xdData.feeds;
    const opts = Object.keys(feeds).map(k => `<option value="${k}">${xdEsc(feeds[k])}</option>`).join('');
    const up = document.getElementById('xd-upload-feed');
    if (!up.options.length) up.innerHTML = opts;
    const ff = document.getElementById('xd-files-feed');
    if (!ff.options.length) ff.innerHTML = '<option value="">All Feeds</option>' + opts;
}

function xdRenderFiles() {
    if (!xdData) return;
    const feed = document.getElementById('xd-files-feed').value;
    const files = xdData.files.filter(f => !feed || f.feed === feed);
    const body = document.getElementById('xd-files-body');
    if (!files.length) { body.innerHTML = '<tr><td colspan="9" class="text-muted">No files yet.</td></tr>'; return; }
    body.innerHTML = files.map(f => {
        const replaced = f.superseded ? '<div class="xd-sub">Replaced by a newer copy</div>' : '';
        const note = f.page_note ? `<div class="xd-sub">${xdEsc(f.page_note)}</div>` : '';
        const err = f.error ? `<div class="xd-err">${xdEsc(f.error)}</div>` : '';
        const src = f.post_url ? `<a href="${xdEsc(f.post_url)}" target="_blank" rel="noopener">Post</a>`
            : (f.source_url ? '' : '<span class="xd-sub">Uploaded</span>');
        const head = f.headline ? ` title="${xdEsc(f.headline)}"` : '';
        const title = f.title && f.title !== f.filename ? `<div class="xd-sub">${xdEsc(f.title)}</div>` : '';
        return `<tr><td>${xdEsc(f.period)}</td><td>${xdEsc(f.feed_label)}</td>
            <td${head}><a href="${XD_API}/file/${f.id}">${xdEsc(f.filename)}</a>${title}${replaced}${note}${err}</td>
            <td>${xdEsc(XD_KIND[f.kind] || f.kind || '')}</td><td class="num">${f.rows_loaded || ''}</td>
            <td>${xdDate(f.published)}</td><td>${xdDate(f.fetched_at)}</td><td>${xdDate(f.checked_at)}</td><td>${src}</td></tr>`;
    }).join('');
}

function xdRenderOutlook() {
    const o = xdData.outlook;
    const table = document.getElementById('xd-outlook');
    if (!o || !o.years) { table.innerHTML = '<tbody><tr><td class="text-muted">No outlook yet.</td></tr></tbody>'; return; }
    document.getElementById('xd-outlook-date').textContent = o.published ? '(published ' + xdDate(o.published) + ')' : '';
    table.innerHTML = '<thead><tr><th>Year</th>' + o.measures.map(m => `<th class="num">${xdEsc(m)}</th>`).join('') + '</tr></thead><tbody>'
        + o.years.map(y => `<tr><td>${y.year}${y.forecast ? ' (f)' : ''}</td>`
            + o.measures.map(m => `<td class="num">${xdNum(y.values[m])}</td>`).join('') + '</tr>').join('') + '</tbody>';
}

// ---------------------------------------------------------------- settings

function xdRenderSettings() {
    const cfg = xdData.config;
    xdSources = cfg.sources.map(s => ({ ...s }));
    xdRenderSources();
    document.getElementById('xd-skip').value = (cfg.skip_words || []).join(', ');
    document.getElementById('xd-hours').value = cfg.auto_check_hours;
    document.getElementById('xd-auto-from').value = cfg.auto_hours.from;
    document.getElementById('xd-auto-to').value = cfg.auto_hours.to;
    const last = (xdData.state || {}).last_auto;
    document.getElementById('xd-auto-last').textContent = last
        ? `Last automatic run ${xdDate(last.at)}: ${last.ran.length ? 'checked ' + last.ran.join(', ') : 'nothing due'}, ${last.new} new files`
          + (last.errors && last.errors.length ? `, problems: ${last.errors.join(' | ')}` : '') + '.'
        : 'No automatic run yet (it runs on the live site only).';
    document.getElementById('xd-news-on').checked = !!cfg.home_news_enabled;
    document.getElementById('xd-news-admin').checked = !!cfg.home_news_admin_only;
    document.getElementById('xd-colours').innerHTML = Object.keys(XD_COLOUR_LABELS).map(k =>
        `<label><input type="color" data-colour="${k}" value="${xdEsc(cfg.colours[k])}">${XD_COLOUR_LABELS[k]}</label>`).join('');
}

function xdRenderSources() {
    const feeds = xdData.feeds, modes = xdData.auto_modes;
    const runs = (xdData.state || {}).auto_runs || {};
    document.getElementById('xd-sources').innerHTML = xdSources.map((s, i) => {
        const r = runs[s.url];
        let state = '';
        if (r) {
            state = `Last automatic check ${xdDate(r.at)}` + (r.target ? `, looking for ${xdMonthLabel(r.target)}` : '')
                + (r.done ? ', found' : '') + (r.error ? `, failed: ${r.error}` : '') + '.';
        }
        return `
        <div class="xd-src-row">
            <input class="form-control form-control-sm" value="${xdEsc(s.name)}" placeholder="Name" oninput="xdSources[${i}].name = this.value">
            <input class="form-control form-control-sm" value="${xdEsc(s.url)}" placeholder="https://" oninput="xdSources[${i}].url = this.value">
            <select class="form-select form-select-sm" onchange="xdSources[${i}].feed = this.value">
                ${Object.keys(feeds).map(k => `<option value="${k}"${k === s.feed ? ' selected' : ''}>${xdEsc(feeds[k])}</option>`).join('')}
            </select>
            <select class="form-select form-select-sm" onchange="xdSources[${i}].type = this.value">
                <option value="page"${s.type === 'page' ? ' selected' : ''}>Page</option>
                <option value="listing"${s.type === 'listing' ? ' selected' : ''}>Listing</option>
            </select>
            <select class="form-select form-select-sm" onchange="xdSources[${i}].auto = this.value">
                ${Object.keys(modes).map(k => `<option value="${k}"${k === s.auto ? ' selected' : ''}>${xdEsc(modes[k])}</option>`).join('')}
            </select>
            <input type="number" min="1" max="15" class="form-control form-control-sm" value="${xdEsc(s.auto_days)}"
                   oninput="xdSources[${i}].auto_days = this.value">
            <button type="button" class="btn btn-sm btn-outline-danger" onclick="xdSources.splice(${i}, 1); xdRenderSources()">Remove</button>
        </div>
        <div class="xd-src-state">${xdEsc(state)}</div>`;
    }).join('');
}

function xdAddSource() {
    xdSources.push({ name: '', url: '', feed: 'new_car_registrations', type: 'page', auto: 'off', auto_days: 3 });
    xdRenderSources();
}

async function xdSaveSettings() {
    const btn = document.getElementById('xd-save-btn');
    const card = document.getElementById('xd-settings');
    btn.disabled = true; btn.textContent = 'Saving...'; card.classList.add('busy');
    try {
        const colours = {};
        document.querySelectorAll('#xd-colours input[data-colour]').forEach(el => { colours[el.dataset.colour] = el.value; });
        const d = await xdCall(XD_API + '/config', 'POST', {
            sources: xdSources,
            skip_words: document.getElementById('xd-skip').value.split(','),
            auto_check_hours: document.getElementById('xd-hours').value,
            auto_hours: { from: document.getElementById('xd-auto-from').value, to: document.getElementById('xd-auto-to').value },
            home_news_enabled: document.getElementById('xd-news-on').checked,
            home_news_admin_only: document.getElementById('xd-news-admin').checked,
            colours,
        });
        xdData.config = d.config;
        xdShowError('xd-settings-error', '');
        xdRender();
    } catch (e) {
        xdShowError('xd-settings-error', 'Not saved: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = 'Save'; card.classList.remove('busy');
    }
}

// ---------------------------------------------------------------- jobs

// Run fn while holding the server's check lock; refuses if another check holds it.
async function xdWithLease(fn) {
    const job = Math.random().toString(36).slice(2, 12);
    await xdCall(XD_API + '/lease', 'POST', { job });
    try {
        return await fn(job);
    } finally {
        try { await xdCall(XD_API + '/lease', 'POST', { job, release: true }); } catch (e) { /* runs out by itself */ }
    }
}

async function xdFetchAll(links, job) {
    const savedIds = [], failed = [];
    for (let i = 0; i < links.length; i++) {
        xdStatus(`Fetching ${i + 1} of ${links.length}: ${links[i].filename || links[i].url}`);
        try {
            const r = await xdCall(XD_API + '/fetch', 'POST', { ...links[i], job });
            if (r.outcome === 'saved') savedIds.push(r.id);
        } catch (e) {
            failed.push((links[i].filename || links[i].url) + ': ' + e.message);
        }
    }
    return { saved: savedIds.length, savedIds, failed };
}

async function xdRebuild() {
    xdStatus('Rebuilding the tables...');
    await xdCall(XD_API + '/rebuild', 'POST');
    await xdLoad();
}

async function xdCheck(full) {
    if (xdBusy) return;
    xdLock(true, full ? 'xd-full-btn' : 'xd-check-btn', full ? 'Scanning...' : 'Checking...');
    const errors = [];
    let summary = '';
    try {
        await xdWithLease(async job => {
            const links = [];
            const sources = xdData ? xdData.config.sources : [];
            for (let s = 0; s < sources.length; s++) {
                let page = 1;
                while (page) {
                    xdStatus(`Reading ${sources[s].name}${page > 1 ? ', page ' + page : ''}...`);
                    try {
                        const d = await xdCall(XD_API + '/scan', 'POST', { source: s, page, job });
                        d.links.forEach(l => { if (!links.some(x => x.url === l.url)) links.push(l); });
                        page = full && d.next_page ? d.next_page : null;
                    } catch (e) {
                        errors.push(e.message);
                        page = null;
                    }
                }
            }
            xdStatus('Checking whether recent files were replaced...');
            try {
                const r = await xdCall(XD_API + '/refresh', 'POST', { job });
                r.links.forEach(l => { if (!links.some(x => x.url === l.url)) links.push(l); });
            } catch (e) {
                errors.push('Replaced-file check: ' + e.message);
            }
            const r = await xdFetchAll(links, job);
            errors.push(...r.failed);
            await xdRebuild();
            if (r.savedIds.length) {
                // Email alerts (Settings > Email Alerts) for the new files.
                try { await xdCall(XD_API + '/notify', 'POST', { file_ids: r.savedIds }); }
                catch (e) { errors.push('Email alert: ' + e.message); }
            }
            summary = r.saved ? r.saved + ' new or replaced files saved. ' : 'Nothing new. ';
        });
        xdStatus(summary + document.getElementById('xd-status').textContent);
    } catch (e) {
        errors.push(e.message);
    } finally {
        xdShowError('xd-error', errors.length ? 'Some steps failed: ' + errors.join(' | ') : '');
        xdLock(false, full ? 'xd-full-btn' : 'xd-check-btn');
    }
}

async function xdUpload(files) {
    if (xdBusy || !files || !files.length) return;
    xdLock(true, null);
    const feed = document.getElementById('xd-upload-feed').value;
    const errors = [];
    try {
        await xdWithLease(async () => {
            for (let i = 0; i < files.length; i++) {
                xdStatus(`Uploading ${i + 1} of ${files.length}: ${files[i].name}`);
                const form = new FormData();
                form.append('file', files[i]);
                form.append('feed', feed);
                try { await xdCall(XD_API + '/upload', 'POST', form, true); }
                catch (e) { errors.push(files[i].name + ': ' + e.message); }
            }
            await xdRebuild();
        });
    } catch (e) {
        errors.push(e.message);
    } finally {
        document.getElementById('xd-upload').value = '';
        xdShowError('xd-error', errors.length ? 'Some files failed: ' + errors.join(' | ') : '');
        xdLock(false, null);
    }
}

async function xdLoadS3() {
    if (xdBusy) return;
    xdLock(true, 'xd-s3-btn', 'Loading...');
    const errors = [];
    try {
        await xdWithLease(async () => {
            xdStatus('Listing the S3 folders...');
            const d = await xdCall(XD_API + '/s3-missing');
            for (let i = 0; i < d.keys.length; i++) {
                xdStatus(`Reading ${i + 1} of ${d.keys.length} from S3...`);
                try { await xdCall(XD_API + '/s3-load', 'POST', { key: d.keys[i] }); }
                catch (e) { errors.push(d.keys[i].split('/').pop() + ': ' + e.message); }
            }
            await xdRebuild();
        });
    } catch (e) {
        errors.push(e.message);
    } finally {
        xdShowError('xd-error', errors.length ? 'Some files failed: ' + errors.join(' | ') : '');
        xdLock(false, 'xd-s3-btn');
    }
}

// On opening: show what is stored, then check if the last check is old enough.
(async () => {
    const d = await xdLoad();
    if (!d) return;
    if (!d.files.length) {
        xdStatus('No files recorded yet. Press Load From S3 to read the files already saved there.');
        return;
    }
    const hours = Number(d.config.auto_check_hours) || 0;
    const lastAt = (d.state || {}).last_checked;
    const last = lastAt ? new Date(lastAt) : null;
    if (hours > 0 && !d.running && (!last || (Date.now() - last.getTime()) / 3600000 >= hours)) xdCheck(false);
})();
