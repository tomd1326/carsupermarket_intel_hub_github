/* Settings > GA4 Requests (templates/_settings_ga4_requests.html). Uses the
   BASE_URL set just before this file is loaded. */
const G4_API = BASE_URL + '/api/ga4-requests';
const G4_FIELDS = ['query_name', 'description', 'dimensions', 'metrics', 'filter', 'unique_key', 'used_by', 'notes'];
let g4Requests = [];
let g4Tables = {};          // query name -> Snowflake check result
let g4Open = new Set();     // query names with their details shown
let g4Editing = null;       // query name being edited; null = adding

function g4Esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function g4ShowError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg || '';
    el.classList.toggle('d-none', !msg);
}

async function g4Call(url, method = 'GET', body) {
    const res = await fetch(url, {
        method, headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.status !== 'ok') throw new Error(data.message || 'Failed');
    return data;
}

async function g4Load() {
    try {
        g4Requests = (await g4Call(G4_API)).requests;
        g4ShowError('g4-error', '');
        g4Render();
    } catch (e) {
        g4ShowError('g4-error', 'Could not load the GA4 requests: ' + e.message);
    }
}

function g4Fmt(v) {
    if (!v) return '';
    const s = String(v).replace('T', ' ');
    const [d, t] = s.split(' ');
    return d.split('-').reverse().join('/') + (t ? ' ' + t.slice(0, 5) : '');
}

function g4Render() {
    const body = document.getElementById('g4-body');
    if (!g4Requests.length) {
        body.innerHTML = '<tr><td colspan="9" class="text-muted">No requests yet.</td></tr>';
        return;
    }
    body.innerHTML = g4Requests.map((r, i) => {
        const t = g4Tables[r.query_name];
        let latest = '', changed = '';
        if (t && !t.exists) {
            latest = '<span class="g4-missing">Table not found</span>';
        } else if (t) {
            latest = g4Esc(g4Fmt(t.latest_date));
            changed = g4Esc(g4Fmt(t.last_altered))
                + (t.row_count != null ? `<div class="g4-sub">${Number(t.row_count).toLocaleString('en-GB')} rows</div>` : '');
        }
        const q = g4Esc(r.query_name);
        const open = g4Open.has(r.query_name);
        let html = `<tr>
            <td><strong>${q}</strong>${r.description ? `<div class="g4-sub">${g4Esc(r.description)}</div>` : ''}</td>
            <td class="g4-fields">${g4Esc(r.dimensions.split(',').join(', '))}</td>
            <td class="g4-fields">${g4Esc(r.metrics.split(',').join(', '))}</td>
            <td>${r.start_date ? 'Yes' : 'No'}</td>
            <td class="g4-fields">${g4Esc(r.table)}</td>
            <td>${latest}</td>
            <td>${changed}</td>
            <td class="g4-fields">${g4Esc(r.used_by || '')}</td>
            <td><div class="g4-actions">
                <button class="btn btn-outline-secondary" onclick="g4Toggle(${i})">${open ? 'Hide' : 'Details'}</button>
                <button class="btn btn-outline-primary" onclick="g4Edit(${i})">Edit</button>
                <button class="btn btn-outline-secondary" onclick="g4Move(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Move Up"><i class="fas fa-arrow-up"></i></button>
                <button class="btn btn-outline-secondary" onclick="g4Move(${i}, 1)" ${i === g4Requests.length - 1 ? 'disabled' : ''} title="Move Down"><i class="fas fa-arrow-down"></i></button>
                <button class="btn btn-outline-danger" onclick="g4Delete(${i})">Delete</button>
            </div></td>
        </tr>`;
        if (open) html += g4DetailRow(r, i);
        return html;
    }).join('');
}

function g4DetailRow(r, i) {
    const cols = r.columns.map(c => `${g4Esc(c.field)} &rarr; <strong>${g4Esc(c.column.toUpperCase())}</strong>`).join('<br>');
    return `<tr class="g4-detail"><td colspan="9"><div class="g4-detail-grid">
        <span class="g4-k">Rundeck Command</span>
        <span><div class="g4-code" id="g4-cmd-${i}">${g4Esc(r.command)}</div>
            <button class="btn btn-sm btn-outline-secondary mt-1" onclick="g4Copy(${i})"><i class="fas fa-copy me-1"></i>Copy</button></span>
        <span class="g4-k">S3 Folder</span>
        <span class="g4-code">s3://aa-raw-sources-uk/${g4Esc(r.s3_folder)}</span>
        <span class="g4-k">dbt Models</span>
        <span class="g4-cols">uk_dbt_raw_data: ${r.dbt_models.map(g4Esc).join(' &rarr; ')}</span>
        <span class="g4-k">Snowflake Table</span>
        <span class="g4-cols">PROD_CARSUPERMARKET_DB.${g4Esc(r.table)}</span>
        <span class="g4-k">Columns</span>
        <span class="g4-cols">${cols}</span>
        <span class="g4-k">Unique Key</span>
        <span class="g4-cols">${g4Esc(r.unique_key || '')}</span>
        ${r.filter ? `<span class="g4-k">Filter</span><span class="g4-code">${g4Esc(r.filter)}</span>` : ''}
        ${r.notes ? `<span class="g4-k">Notes</span><span style="white-space:pre-wrap">${g4Esc(r.notes)}</span>` : ''}
    </div></td></tr>`;
}

function g4Toggle(i) {
    const q = g4Requests[i].query_name;
    if (g4Open.has(q)) g4Open.delete(q); else g4Open.add(q);
    g4Render();
}

async function g4Copy(i) {
    const text = g4Requests[i].command;
    try {
        await navigator.clipboard.writeText(text);
    } catch (e) {
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('g4-cmd-' + i));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }
}

async function g4Check() {
    const btn = document.getElementById('g4-check-btn');
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" style="width:.7rem;height:.7rem"></span>Checking...';
    try {
        g4Tables = (await g4Call(G4_API + '/snowflake')).tables;
        g4ShowError('g4-error', '');
        g4Render();
    } catch (e) {
        g4ShowError('g4-error', 'Snowflake check failed: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = label;
    }
}

function g4Edit(i) {
    const r = i === null ? { start_date: false } : g4Requests[i];
    g4Editing = i === null ? null : r.query_name;
    document.getElementById('g4-edit-title').textContent = i === null ? 'Add Request' : 'Edit ' + r.query_name;
    G4_FIELDS.forEach(k => { document.getElementById('g4-' + k).value = r[k] || ''; });
    document.getElementById('g4-start_date').checked = !!r.start_date;
    g4ShowError('g4-edit-error', '');
    const card = document.getElementById('g4-edit');
    card.classList.remove('d-none');
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function g4CloseEdit() {
    document.getElementById('g4-edit').classList.add('d-none');
    g4Editing = null;
}

async function g4Save() {
    const request = {};
    G4_FIELDS.forEach(k => { request[k] = document.getElementById('g4-' + k).value.trim(); });
    request.start_date = document.getElementById('g4-start_date').checked;
    const btn = document.getElementById('g4-save-btn');
    btn.disabled = true;
    try {
        g4Requests = (await g4Call(G4_API, 'POST', { request, original: g4Editing })).requests;
        g4CloseEdit();
        g4Render();
    } catch (e) {
        g4ShowError('g4-edit-error', e.message);
    } finally {
        btn.disabled = false;
    }
}

async function g4Move(i, step) {
    try {
        g4Requests = (await g4Call(G4_API + '/move', 'POST', { query_name: g4Requests[i].query_name, step })).requests;
        g4Render();
    } catch (e) {
        g4ShowError('g4-error', e.message);
    }
}

async function g4Delete(i) {
    const q = g4Requests[i].query_name;
    if (!confirm(`Delete ${q}? This only removes it from this list, not from Rundeck or Snowflake.`)) return;
    try {
        g4Requests = (await g4Call(G4_API + '/' + encodeURIComponent(q), 'DELETE')).requests;
        g4Open.delete(q);
        g4Render();
    } catch (e) {
        g4ShowError('g4-error', e.message);
    }
}

g4Load();
