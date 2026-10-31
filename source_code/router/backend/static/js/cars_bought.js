/* Cars Bought: build a purchase batch from an invoice or from the Supplier
 * Stock Evaluator, let the buyer check and complete it, then submit. */

const BASE = window.BASE_URL || '';
let customFields = [];
let suppliers = [];
let rows = [];          // vehicle rows being edited
let batchMeta = {};     // source, filename, s3_key, supplier_id
let buyerCfg = {buyers: [], lock_buyer: false, me: ''};
let capChecked = new Set();   // VRMs CAP has been asked about in this batch

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const num = v => { if (v === null || v === undefined || v === '') return null; const n = parseFloat(String(v).replace(/[,£\s]/g, '')); return isNaN(n) ? null : n; };
const gbp = v => v === null || v === undefined ? '—' : '£' + Number(v).toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const r2 = v => Math.round(v * 100) / 100;
const ukDate = iso => { if (!iso) return ''; const [y, m, d] = iso.slice(0, 10).split('-'); return `${d}/${m}/${y}`; };

function postJSON(url, body) {
    return fetch(BASE + url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
        .then(r => r.json());
}

function busy(text) {
    $('busy').style.display = text ? 'block' : 'none';
    $('busyText').textContent = text || '';
}

function msg(html, cls) {
    $('srcMsg').innerHTML = html ? `<div class="alert alert-${cls || 'info'} py-2 mb-0 mt-2">${html}</div>` : '';
}

/* ── Columns ───────────────────────────────────────────── */

const MONEY_COLS = [
    ['vehicle_net', 'Vehicle Net', true], ['vehicle_vat', 'Vehicle VAT'], ['vehicle_gross', 'Vehicle Gross'],
    ['fees_net', 'Fees Net', true], ['fees_vat', 'Fees VAT'], ['fees_gross', 'Fees Gross'],
];

function renderHead() {
    $('vehHead').innerHTML = '<tr><th></th><th>VRM</th><th>Mileage</th><th>Vehicle</th><th>CAP ID</th>' +
        MONEY_COLS.map(([, l, g]) => `<th class="text-end${g ? ' group-start' : ''}">${l}</th>`).join('') +
        '<th class="text-end group-start">AT Valuation</th><th class="text-end">CAP Clean</th>' +
        customFields.map((f, i) => `<th class="${i === 0 ? 'group-start' : ''}">${esc(f.label)}${f.required ? ' *' : ''}</th>`).join('') +
        '<th></th></tr>';
}

function vehicleText(r) {
    const cap = [r.make, r.model, r.derivative].filter(Boolean).join(' ');
    let html = cap ? esc(cap) : '';
    if (r.supplier_description && r.supplier_description !== cap)
        html += `${html ? '<br>' : ''}<span class="text-muted">${esc(r.supplier_description)}</span>`;
    const tags = [];
    if (r.from_evaluator) tags.push(`<span class="tag tag-ev" title="Seen ${esc(r.evaluator_seen_at || '')}">Evaluator: ${esc(r.evaluator_supplier)}${r.evaluator_list_price != null ? ' ' + gbp(r.evaluator_list_price) : ''}</span>`);
    if (r.already_bought) tags.push(`<span class="tag tag-warn">Already Bought ${esc(r.already_bought)}</span>`);
    if (r.fees && r.fees.length) tags.push(`<span class="tag tag-info" title="${esc(r.fees.map(f => `${f.description}: ${f.gross ?? f.net}`).join('\n'))}">${r.fees.length} Fee${r.fees.length > 1 ? 's' : ''}</span>`);
    if (!r.cap_id) tags.push(capChecked.has(r.vrm) ? '<span class="tag tag-warn">No CAP Match</span>'
                                                   : '<span class="tag tag-info">Press Look Up CAP &amp; AT</span>');
    if (r.cap_source === 'live') tags.push('<span class="tag tag-src">CAP & AT: Live Lookup</span>');
    else if (r.cap_source === 'typed') tags.push('<span class="tag tag-src">CAP ID: Typed In</span>');
    return html + (tags.length ? `<div>${tags.join('')}</div>` : '');
}

function renderRows() {
    $('vehBody').innerHTML = rows.map((r, i) => {
        const money = MONEY_COLS.map(([k, , g]) =>
            `<td class="${g ? 'group-start' : ''}"><input class="form-control num" data-i="${i}" data-k="${k}" value="${r[k] ?? ''}"></td>`).join('');
        const custom = customFields.map((f, j) => {
            const v = (r.custom || {})[f.key] ?? '';
            const cls = f.type === 'text' ? 'txt' : 'num';
            return `<td class="${j === 0 ? 'group-start' : ''}"><input class="form-control ${cls}" data-i="${i}" data-c="${esc(f.key)}" value="${esc(v)}"></td>`;
        }).join('');
        return `<tr>
            <td class="text-muted">${i + 1}</td>
            <td><input class="form-control vrm" data-i="${i}" data-k="vrm" value="${esc(r.vrm)}"></td>
            <td><input class="form-control mil" data-i="${i}" data-k="mileage" value="${r.mileage ?? ''}"></td>
            <td class="desc">${vehicleText(r)}</td>
            <td><input class="form-control capid" data-i="${i}" data-k="cap_id" value="${r.cap_id ?? ''}"></td>
            ${money}
            <td class="text-end group-start">${gbp(r.at_retail_valuation)}</td>
            <td class="text-end">${gbp(r.cap_clean)}</td>
            ${custom}
            <td class="text-nowrap"><button class="btn btn-sm btn-outline-primary" data-check="${i}" title="Look up CAP and Autotrader again for this car"><i class="fas fa-sync-alt"></i></button>
                <button class="btn btn-sm btn-outline-danger" data-del="${i}" title="Remove"><i class="fas fa-times"></i></button></td>
        </tr>`;
    }).join('');
    renderTotals();
}

function rowTotals() {
    const t = {};
    MONEY_COLS.forEach(([k]) => { t[k] = r2(rows.reduce((s, r) => s + (num(r[k]) || 0), 0)); });
    ['net', 'vat', 'gross'].forEach(p => { t[p] = r2(t['vehicle_' + p] + t['fees_' + p]); });
    t.at = r2(rows.reduce((s, r) => s + (num(r.at_retail_valuation) || 0), 0));
    t.atCount = rows.filter(r => num(r.at_retail_valuation)).length;
    t.cap = r2(rows.reduce((s, r) => s + (num(r.cap_clean) || 0), 0));
    t.capCount = rows.filter(r => num(r.cap_clean)).length;
    customFields.filter(f => f.type !== 'text').forEach(f => {
        t['c_' + f.key] = r2(rows.reduce((s, r) => s + (num((r.custom || {})[f.key]) || 0), 0));
    });
    return t;
}

function renderTotals() {
    const t = rowTotals();
    $('vehFoot').innerHTML = `<tr><td colspan="5">Total (${rows.length})</td>` +
        MONEY_COLS.map(([k]) => `<td>${gbp(t[k])}</td>`).join('') +
        `<td>${gbp(t.at)}</td><td>${gbp(t.cap)}</td>` +
        customFields.map(f => `<td>${f.type === 'text' ? '' : gbp(t['c_' + f.key])}</td>`).join('') + '<td></td></tr>';

    // Rows against the invoice's printed totals.
    const checks = [['net', 'bNet', 'Net'], ['vat', 'bVat', 'VAT'], ['gross', 'bGross', 'Gross']].map(([p, id, label]) => {
        const inv = num($(id).value);
        if (inv === null) return '';
        const diff = r2(t[p] - inv);
        return Math.abs(diff) <= 0.01
            ? `<span class="check-box check-ok"><i class="fas fa-check me-1"></i>${label} Matches Invoice ${gbp(inv)}</span>`
            : `<span class="check-box check-bad"><i class="fas fa-exclamation-triangle me-1"></i>${label}: Rows ${gbp(t[p])} vs Invoice ${gbp(inv)} (Difference ${gbp(diff)})</span>`;
    }).join('');
    $('totalsCheck').innerHTML = checks;

    const n = rows.length || 1;
    const stats = [
        ['Vehicles', rows.length],
        ['Total Net', gbp(t.net)], ['Total VAT', gbp(t.vat)], ['Total Gross', gbp(t.gross)],
        ['Fees Gross', gbp(t.fees_gross)],
        ['Average Gross Per Vehicle', gbp(r2(t.gross / n))],
        ['From Evaluator', `${rows.filter(r => r.from_evaluator).length} of ${rows.length}`],
    ];
    if (t.atCount) stats.push(['AT Valuation Total', `${gbp(t.at)} (${t.atCount})`]);
    if (t.capCount) stats.push(['CAP Clean Total', `${gbp(t.cap)} (${t.capCount})`]);
    customFields.filter(f => f.type !== 'text').forEach(f => stats.push([f.label + ' Total', gbp(t['c_' + f.key])]));
    const dup = rows.filter(r => r.already_bought).length;
    if (dup) stats.push(['Already Bought', dup]);
    $('summaryGrid').innerHTML = stats.map(([l, v]) => `<div class="stat"><div class="lbl">${esc(l)}</div><div class="val">${v}</div></div>`).join('');
}

function fillBuyers() {
    const sel = $('bBuyer'), keep = sel.value;
    if (buyerCfg.lock_buyer) {
        sel.innerHTML = `<option value="${esc(buyerCfg.me)}">${esc(buyerCfg.me)}</option>`;
        sel.disabled = true;
        return;
    }
    sel.disabled = false;
    sel.innerHTML = '<option value="">Choose Buyer</option>' +
        buyerCfg.buyers.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    if (buyerCfg.buyers.includes(keep)) sel.value = keep;
    else if (buyerCfg.buyers.includes(buyerCfg.me)) sel.value = buyerCfg.me;
}

function showBatch(header) {
    fillBuyers();
    $('batchCard').style.display = '';
    $('summaryCard').style.display = '';
    $('bSupplier').value = header.supplier_name || '';
    $('bInvoice').value = header.invoice_number || '';
    $('bDate').value = header.invoice_date || new Date().toISOString().slice(0, 10);
    $('bNet').value = header.invoice_net ?? '';
    $('bVat').value = header.invoice_vat ?? '';
    $('bGross').value = header.invoice_gross ?? '';
    $('bNotes').value = '';
    $('aiNotes').textContent = header.notes ? 'Read From The File: ' + header.notes : '';
    renderHead();
    renderRows();
}

/* ── Editing ───────────────────────────────────────────── */

$('vehBody').addEventListener('input', e => {
    const el = e.target, i = +el.dataset.i;
    if (isNaN(i) || !rows[i]) return;
    if (el.dataset.c) {
        rows[i].custom = rows[i].custom || {};
        rows[i].custom[el.dataset.c] = el.value;
    } else if (el.dataset.k) {
        rows[i][el.dataset.k] = el.dataset.k === 'vrm' ? el.value.toUpperCase().replace(/\s+/g, '') : el.value;
        if (el.dataset.k === 'cap_id') rows[i].cap_source = el.value ? 'typed' : null;
    }
    renderTotals();
});
$('vehBody').addEventListener('click', e => {
    const del = e.target.closest('[data-del]');
    if (del) { rows.splice(+del.dataset.del, 1); renderRows(); return; }
    const chk = e.target.closest('[data-check]');
    if (chk && rows[+chk.dataset.check].vrm) runLookup([rows[+chk.dataset.check]]);
});
['bNet', 'bVat', 'bGross'].forEach(id => $(id).addEventListener('input', renderTotals));
$('addRowBtn').addEventListener('click', () => { rows.push({vrm: '', fees: [], custom: {}}); renderRows(); });
$('clearBtn').addEventListener('click', () => {
    if (rows.length && !confirm('Clear this batch without saving?')) return;
    rows = []; batchMeta = {};
    $('batchCard').style.display = 'none';
    $('summaryCard').style.display = 'none';
    msg('');
});

/* ── Source: upload ────────────────────────────────────── */

const modal = id => bootstrap.Modal.getOrCreateInstance($(id));
$('openUploadBtn').addEventListener('click', () => modal('uploadModal').show());
$('openEvaluatorBtn').addEventListener('click', () => modal('evaluatorModal').show());

const dz = $('dropZone');
dz.addEventListener('click', () => $('fileInput').click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('over'); if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); });
$('fileInput').addEventListener('change', e => { if (e.target.files[0]) readFile(e.target.files[0]); e.target.value = ''; });

function startNew() {
    if (rows.length && !confirm('This replaces the batch on screen, which has not been submitted. Carry on?')) return false;
    return true;
}

/* Progress panel: the steps of reading a file, ticked off as the server
   reports them (stage: read, scan, match). 'scan' only shows for scans. */
const RP_STEPS = [
    ['upload', 'Uploading The File', 10],
    ['read', 'Reading The Invoice', 40],
    ['scan', 'Reading The Scan (Textract)', 70],
    ['match', 'Matching To Supplier Stock Data', 90],
];
let rpTimer = null, rpStart = 0, rpScan = false;

function rpShow(file) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    $('rpIcon').className = 'fas ' + (ext === 'pdf' ? 'fa-file-pdf' : ['csv', 'xlsx', 'xls', 'xlsm'].includes(ext) ? 'fa-file-excel'
        : ['jpg', 'jpeg', 'png'].includes(ext) ? 'fa-file-image' : 'fa-file-alt');
    $('rpName').textContent = file.name;
    $('rpSize').textContent = file.size >= 1048576 ? (file.size / 1048576).toFixed(1) + ' MB' : Math.ceil(file.size / 1024) + ' KB';
    rpScan = false;
    rpStart = Date.now();
    clearInterval(rpTimer);
    rpTimer = setInterval(() => { $('rpTime').textContent = Math.round((Date.now() - rpStart) / 1000) + ' seconds'; }, 500);
    $('rpTime').textContent = '0 seconds';
    $('readProgress').style.display = '';
    dz.style.display = 'none';
    rpStep('upload');
}

function rpStep(stage) {
    if (stage === 'scan') rpScan = true;
    const steps = RP_STEPS.filter(s => s[0] !== 'scan' || rpScan);
    const at = steps.findIndex(s => s[0] === stage);
    $('rpSteps').innerHTML = steps.map(([key, label], i) => {
        const state = i < at ? 'done' : i === at ? 'active' : '';
        const icon = state === 'done' ? '<i class="fas fa-check-circle"></i>'
            : state === 'active' ? '<i class="fas fa-spinner fa-spin"></i>' : '<i class="far fa-circle"></i>';
        return `<li class="${state}"><span class="rp-icon">${icon}</span>${label}</li>`;
    }).join('');
    $('rpBar').style.width = (steps[at] ? steps[at][2] : 100) + '%';
}

function rpHide() {
    clearInterval(rpTimer);
    $('readProgress').style.display = 'none';
    dz.style.display = '';
}

// The read runs on the server in the background; ask every 2 seconds, for up to 3 minutes.
async function pollExtract(jobId) {
    for (let i = 0; i < 90; i++) {
        await new Promise(r => setTimeout(r, 2000));
        const res = await fetch(BASE + '/cars-bought/extract/' + jobId).then(r => r.json());
        if (res.status !== 'running') return res;
        if (res.stage) rpStep(res.stage);
    }
    return {status: 'error', message: 'Reading the file took more than 3 minutes'};
}

function readFile(file) {
    if (!startNew()) return;
    if (file.size > 4 * 1024 * 1024) { msg(`${esc(file.name)} is larger than 4 MB`, 'danger'); return; }
    const reader = new FileReader();
    reader.onload = () => {
        const data = String(reader.result).split(',')[1] || '';
        rpShow(file);
        msg('');
        postJSON('/cars-bought/extract', {filename: file.name, data})
            .then(start => { if (start.status === 'running') { rpStep('read'); return pollExtract(start.job_id); } return start; })
            .then(res => {
                rpHide();
                modal('uploadModal').hide();
                if (res.status !== 'ok') { msg(esc(res.message || 'Could not read the file'), 'danger'); return; }
                capChecked = new Set();
                rows = res.vehicles;
                batchMeta = {source: 'upload', filename: res.filename, s3_key: res.s3_key, supplier_id: res.supplier_id};
                msg(`${rows.length} vehicle${rows.length === 1 ? '' : 's'} read from ${esc(res.filename)}.`
                    + ' Press Look Up CAP & AT, and check every row against the invoice before submitting.', 'success');
                showBatch(res);
            })
            .catch(err => { rpHide(); modal('uploadModal').hide(); msg('Error: ' + esc(err.message), 'danger'); });
    };
    reader.readAsDataURL(file);
}

/* ── Source: evaluator ─────────────────────────────────── */

$('evLoadBtn').addEventListener('click', () => {
    const sid = $('evSupplier').value;
    const vrms = $('vrmPaste').value;
    if (!vrms.trim()) { alert('Paste at least one VRM'); return; }
    if (!startNew()) return;
    modal('evaluatorModal').hide();
    busy('Loading from the Supplier Stock Evaluator...');
    msg('');
    postJSON('/cars-bought/from-evaluator', {supplier_id: sid, vrms})
        .then(res => {
            busy('');
            if (res.status !== 'ok') { msg(esc(res.message), 'danger'); return; }
            rows = res.vehicles;
            capChecked = new Set();
            const sup = suppliers.find(s => String(s.supplier_id) === String(sid));
            batchMeta = {source: 'evaluator', supplier_id: sid || null};
            let m = `${rows.length - res.not_found.length} of ${rows.length} found in the evaluator.`;
            if (res.not_found.length) m += ` Not found (fill in by hand): ${res.not_found.map(esc).join(', ')}`;
            msg(m, res.not_found.length ? 'warning' : 'success');
            showBatch({supplier_name: sup ? sup.supplier_name : ''});
        })
        .catch(err => { busy(''); msg('Error: ' + esc(err.message), 'danger'); });
});

/* ── Submit ────────────────────────────────────────────── */

/* ── CAP lookup (must be done before Submit) ───────────── */

function capPending() {
    return rows.filter(r => r.vrm && !r.cap_id && !capChecked.has(r.vrm));
}

const LOOKUP_FIELDS = ['cap_id', 'make', 'model', 'derivative', 'cap_clean', 'at_retail_valuation', 'at_rating'];
const blank = v => v === null || v === undefined || v === '';

/* Live CAP and Autotrader for each car, always fresh API calls (no
   evaluator data: for a batch this size it is not worth it). The results
   replace any CAP / Autotrader figures already on the row.
   One car per request, 8 at once: the count goes up car by car, and a slow
   car (CAP timing out) never holds up the rest. */
async function runLookup(todo) {
    const parallel = 8;
    let done = 0, failed = 0, lastError = '';
    $('capBtn').disabled = true;
    const label = 'Looking up CAP and Autotrader';
    busy(`${label}... 0 of ${todo.length}`);
    const one = async r => {
        try {
            const res = await postJSON('/cars-bought/cap-lookup', {vehicles: [{vrm: r.vrm, mileage: num(r.mileage)}]});
            if (res.status !== 'ok') throw new Error(res.message);
            const c = res.results[r.vrm] || {};
            capChecked.add(r.vrm);
            LOOKUP_FIELDS.forEach(k => { if (!blank(c[k])) r[k] = c[k]; });
            if (!blank(c.cap_id)) r.cap_source = 'live';
            r.lookup_note = c.note || null;
        } catch (e) {
            failed += 1;
            lastError = e.message;
        }
        done += 1;
        busy(`${label}... ${done} of ${todo.length}`);
        renderRows();
    };
    // A pool of 8: the next car starts as soon as any one finishes.
    const queue = todo.slice();
    await Promise.all(Array.from({length: Math.min(parallel, queue.length)}, async () => {
        while (queue.length) await one(queue.shift());
    }));
    busy('');
    $('capBtn').disabled = false;
    const noCap = todo.filter(r => !r.cap_id).map(r => r.vrm + (r.lookup_note ? ` (${r.lookup_note})` : ''));
    const notes = [];
    if (failed) notes.push(`${failed} could not be checked, try again (${esc(lastError)})`);
    if (noCap.length) notes.push(`no CAP match for ${noCap.map(esc).join(', ')} (type the CAP ID in if you know it)`);
    msg(notes.length ? notes.join('; ') : `CAP and Autotrader found for all ${todo.length}.`, notes.length ? 'warning' : 'success');
}

$('capBtn').addEventListener('click', () => {
    const todo = rows.filter(r => r.vrm);
    if (todo.length) runLookup(todo);
});

$('submitBtn').addEventListener('click', () => {
    if (!$('bBuyer').value) { alert('Choose the buyer'); return; }
    if (capPending().length) { alert(`Press Look Up CAP & AT first: ${capPending().length} row(s) have no CAP ID yet.`); return; }
    const t = rowTotals();
    const mismatch = [['net', 'bNet'], ['vat', 'bVat'], ['gross', 'bGross']]
        .some(([p, id]) => num($(id).value) !== null && Math.abs(t[p] - num($(id).value)) > 0.01);
    if (mismatch && !confirm('The rows do not add up to the invoice totals. Submit anyway?')) return;
    if (!confirm(`Submit ${rows.length} vehicle${rows.length === 1 ? '' : 's'} from ${$('bSupplier').value || 'this supplier'}?`)) return;
    const batch = {
        ...batchMeta,
        buyer: $('bBuyer').value,
        supplier_name: $('bSupplier').value, invoice_number: $('bInvoice').value, purchase_date: $('bDate').value,
        invoice_net: num($('bNet').value), invoice_vat: num($('bVat').value), invoice_gross: num($('bGross').value),
        notes: $('bNotes').value,
    };
    $('submitBtn').disabled = true;
    busy('Submitting...');
    postJSON('/cars-bought/submit', {batch, vehicles: rows})
        .then(res => {
            busy('');
            $('submitBtn').disabled = false;
            if (res.status !== 'ok') { alert(res.message); return; }
            const missing = Object.entries(res.cap_not_found || {});
            msg(`Batch ${res.batch_id} saved.` + (missing.length ? ` No CAP match for: ${missing.map(([v, n]) => `${esc(v)} (${esc(n)})`).join(', ')}` : ''), missing.length ? 'warning' : 'success');
            rows = []; batchMeta = {};
            $('batchCard').style.display = 'none';
            $('summaryCard').style.display = 'none';
            loadHistory();
        })
        .catch(err => { busy(''); $('submitBtn').disabled = false; alert('Error: ' + err.message); });
});

/* ── History and export ────────────────────────────────── */

function loadHistory() {
    fetch(BASE + '/cars-bought/batches').then(r => r.json()).then(res => {
        const b = res.batches || [];
        $('histBody').innerHTML = b.length ? b.map(x => `<tr data-batch="${x.batch_id}">
            <td>${ukDate(x.purchase_date)}</td><td>${esc(x.buyer || '')}</td><td>${esc(x.supplier_name)}</td><td>${esc(x.invoice_number || '')}</td>
            <td class="text-end">${x.vehicles}</td><td class="text-end">${gbp(x.rows_gross)}</td>
            <td class="text-end">${x.invoice_gross == null ? '—' : gbp(x.invoice_gross)}</td>
            <td>${x.source === 'evaluator' ? 'Evaluator' : esc(x.filename || 'Upload')}</td>
            <td>${esc(x.submitted_by)}</td><td>${ukDate(x.submitted_at)} ${x.submitted_at.slice(11, 16)}</td>
            <td><a class="btn btn-sm btn-outline-secondary" href="${BASE}/cars-bought/export.csv?batch_id=${x.batch_id}" data-stop="1"><i class="fas fa-download"></i></a>
            ${window.IS_ADMIN ? `<button class="btn btn-sm btn-outline-danger" data-delbatch="${x.batch_id}" title="Delete"><i class="fas fa-trash"></i></button>` : ''}</td>
        </tr>`).join('') : '<tr><td colspan="11" class="text-muted">No purchases yet</td></tr>';
    }).catch(() => { $('histBody').innerHTML = '<tr><td colspan="11" class="text-danger">Could not load purchases</td></tr>'; });
}

$('histBody').addEventListener('click', e => {
    if (e.target.closest('[data-stop]')) return;
    const del = e.target.closest('[data-delbatch]');
    if (del) {
        if (!confirm(`Delete batch ${del.dataset.delbatch} and all its vehicles?`)) return;
        fetch(BASE + '/cars-bought/batches/' + del.dataset.delbatch, {method: 'DELETE'}).then(r => r.json())
            .then(res => { if (res.status !== 'ok') alert(res.message); loadHistory(); });
        return;
    }
    const tr = e.target.closest('[data-batch]');
    if (tr) openBatch(tr.dataset.batch);
});

function openBatch(id) {
    fetch(BASE + '/cars-bought/batches/' + id).then(r => r.json()).then(res => {
        const v = res.vehicles || [];
        $('batchModalTitle').textContent = v.length ? `Batch ${id}: ${v[0].supplier_name}, ${ukDate(v[0].purchase_date)}` : `Batch ${id}`;
        const cols = [['vrm', 'VRM'], ['mileage', 'Mileage'], ['cap_id', 'CAP ID'], ['make', 'Make'], ['model', 'Model'],
            ['derivative', 'Derivative'], ['vehicle_net', 'Vehicle Net', 1], ['vehicle_vat', 'Vehicle VAT', 1],
            ['vehicle_gross', 'Vehicle Gross', 1], ['fees_net', 'Fees Net', 1], ['fees_vat', 'Fees VAT', 1], ['fees_gross', 'Fees Gross', 1]];
        $('batchModalBody').innerHTML = `<table class="table table-sm veh-table"><thead><tr>${cols.map(c => `<th>${c[1]}</th>`).join('')}${customFields.map(f => `<th>${esc(f.label)}</th>`).join('')}</tr></thead><tbody>` +
            v.map(r => `<tr>${cols.map(([k, , m]) => `<td class="${m ? 'text-end' : ''}">${m ? gbp(r[k]) : esc(r[k] ?? '')}</td>`).join('')}${customFields.map(f => `<td>${esc((r.custom || {})[f.key] ?? '')}</td>`).join('')}</tr>`).join('') +
            '</tbody></table>';
        bootstrap.Modal.getOrCreateInstance($('batchModal')).show();
    });
}

$('exportBtn').addEventListener('click', () => {
    const q = new URLSearchParams();
    if ($('expFrom').value) q.set('date_from', $('expFrom').value);
    if ($('expTo').value) q.set('date_to', $('expTo').value);
    window.location.href = BASE + '/cars-bought/export.csv?' + q.toString();
});

/* ── Admin settings ────────────────────────────────────── */

function cfRow(f) {
    const types = [['money', 'Money'], ['number', 'Number'], ['text', 'Text']];
    return `<tr data-key="${esc(f.key || '')}">
        <td><input class="form-control form-control-sm cf-label" value="${esc(f.label || '')}"></td>
        <td><select class="form-select form-select-sm cf-type">${types.map(([v, l]) => `<option value="${v}"${f.type === v ? ' selected' : ''}>${l}</option>`).join('')}</select></td>
        <td class="text-center"><input type="checkbox" class="form-check-input cf-req"${f.required ? ' checked' : ''}></td>
        <td><button class="btn btn-sm btn-outline-danger cf-del"><i class="fas fa-times"></i></button></td></tr>`;
}

if (window.IS_ADMIN) {
    $('settingsBtn').addEventListener('click', () => {
        $('cfBody').innerHTML = customFields.map(cfRow).join('');
        $('lockBuyerInput').checked = buyerCfg.lock_buyer;
        const labels = buyerCfg.column_labels || {};
        $('colNamesBody').innerHTML = Object.keys(labels).map(k =>
            `<tr><td class="text-nowrap fw-semibold" style="width:170px">${esc(labels[k])}</td>
             <td><input class="form-control form-control-sm" data-colfield="${esc(k)}" value="${esc(((buyerCfg.column_names || {})[k] || []).join(', '))}"></td></tr>`).join('');
        $('feeWordsInput').value = (buyerCfg.fee_words || []).join(', ');
        $('feeVatRateInput').value = buyerCfg.fee_vat_rate ?? 20;
        $('evalHoursInput').value = buyerCfg.evaluator_max_hours ?? 24;
        $('supplierNamesInput').value = (buyerCfg.supplier_names || []).map(s => `${s.text} = ${s.supplier}`).join('\n');
        bootstrap.Modal.getOrCreateInstance($('settingsModal')).show();
    });
    $('cfAddBtn').addEventListener('click', () => $('cfBody').insertAdjacentHTML('beforeend', cfRow({type: 'money'})));
    $('cfBody').addEventListener('click', e => { const b = e.target.closest('.cf-del'); if (b) b.closest('tr').remove(); });
    $('settingsSaveBtn').addEventListener('click', () => {
        const fields = [...$('cfBody').querySelectorAll('tr')].map(tr => ({
            key: tr.dataset.key || null, label: tr.querySelector('.cf-label').value,
            type: tr.querySelector('.cf-type').value, required: tr.querySelector('.cf-req').checked,
        }));
        const split = s => s.split(',').map(x => x.trim()).filter(Boolean);
        const column_names = {};
        $('colNamesBody').querySelectorAll('[data-colfield]').forEach(el => { column_names[el.dataset.colfield] = split(el.value); });
        postJSON('/cars-bought/config', {custom_fields: fields, lock_buyer: $('lockBuyerInput').checked,
                                         column_names, fee_words: split($('feeWordsInput').value),
                                         fee_vat_rate: num($('feeVatRateInput').value),
                                         evaluator_max_hours: num($('evalHoursInput').value),
                                         supplier_names: $('supplierNamesInput').value.split('\n')
                                             .map(l => l.split('=')).filter(p => p.length === 2)
                                             .map(([text, supplier]) => ({text: text.trim(), supplier: supplier.trim()}))}).then(res => {
            if (res.status !== 'ok') { alert(res.message); return; }
            customFields = res.custom_fields;
            buyerCfg = res;
            fillBuyers();
            bootstrap.Modal.getOrCreateInstance($('settingsModal')).hide();
            if (rows.length) { renderHead(); renderRows(); }
        });
    });
}

/* ── Start ─────────────────────────────────────────────── */

fetch(BASE + '/cars-bought/config').then(r => r.json()).then(res => {
    customFields = res.custom_fields || [];
    suppliers = res.suppliers || [];
    buyerCfg = res;
    fillBuyers();
    $('evSupplier').innerHTML = '<option value="">Any Supplier</option>' +
        suppliers.map(s => `<option value="${s.supplier_id}">${esc(s.supplier_name)}</option>`).join('');
    $('supplierList').innerHTML = suppliers.map(s => `<option value="${esc(s.supplier_name)}">`).join('');
});
loadHistory();
