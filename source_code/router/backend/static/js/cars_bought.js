/* Cars Bought, Add Cars: build a purchase batch from an invoice or from the
 * Supplier Stock Evaluator, let the buyer check and complete it, then submit.
 * Loaded after cars_bought_common.js. */

let suppliers = [];
let rows = [];          // vehicle rows being edited
let batchMeta = {};     // source, filename, s3_key, supplier_id
let capChecked = new Set();   // VRMs CAP has been asked about in this batch

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

const batchFields = () => customFields.filter(f => fieldApplies(f, $('bSupplier').value));
let batchFieldKeys = '';

function renderHead() {
    const fields = batchFields();
    batchFieldKeys = fields.map(f => f.key).join();
    $('vehHead').innerHTML = '<tr><th></th><th>VRM</th><th>Mileage</th><th>Vehicle</th><th>CAP ID</th><th>VAT Status</th>' +
        MONEY_COLS.map(([, l, g]) => `<th class="text-end${g ? ' group-start' : ''}">${l}</th>`).join('') +
        '<th class="text-end group-start">Total Purchase</th>' +
        '<th class="text-end group-start">AT Valuation</th><th class="text-end">CAP Clean</th>' +
        fields.map((f, i) => `<th class="${i === 0 ? 'group-start' : ''}">${esc(f.label)}${f.required ? ' *' : ''}</th>`).join('') +
        '<th></th></tr>';
}

/* The supplier changed: redrawn only if that changes which fields show. */
function refreshBatchFields() {
    if (!rows.length || batchFields().map(f => f.key).join() === batchFieldKeys) return;
    renderHead();
    renderRows();
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
    else if (r.cap_source === 'stored') tags.push('<span class="tag tag-src">CAP & AT: Stored Figures</span>');
    else if (r.cap_source === 'typed') tags.push('<span class="tag tag-src">CAP ID: Typed In</span>');
    return html + (tags.length ? `<div>${tags.join('')}</div>` : '');
}

function renderRows() {
    $('vehBody').innerHTML = rows.map((r, i) => {
        const money = MONEY_COLS.map(([k, , g]) =>
            `<td class="${g ? 'group-start' : ''}"><input class="form-control num" data-i="${i}" data-k="${k}" value="${r[k] ?? ''}"></td>`).join('');
        const custom = batchFields().map((f, j) => {
            const v = (r.custom || {})[f.key] ?? '';
            const cls = f.type === 'text' ? 'txt' : 'num';
            const box = f.type === 'yesno' ? `<select class="form-select yesno" data-i="${i}" data-c="${esc(f.key)}">${yesNoOptions(v)}</select>`
                : `<input class="form-control ${cls}" data-i="${i}" data-c="${esc(f.key)}" value="${esc(v)}">`;
            return `<td class="${j === 0 ? 'group-start' : ''}">${box}</td>`;
        }).join('');
        return `<tr>
            <td class="text-muted">${i + 1}</td>
            <td><input class="form-control vrm" data-i="${i}" data-k="vrm" value="${esc(r.vrm)}"></td>
            <td><input class="form-control mil" data-i="${i}" data-k="mileage" value="${r.mileage ?? ''}"></td>
            <td class="desc">${vehicleText(r)}</td>
            <td><input class="form-control capid" data-i="${i}" data-k="cap_id" value="${r.cap_id ?? ''}"></td>
            <td class="text-nowrap" data-status="${i}">${vatStatus(r)}</td>
            ${money}
            <td class="text-end group-start fw-bold" data-total="${i}">${gbp(purchaseTotal(r))}</td>
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
    t.total = r2(rows.reduce((s, r) => s + purchaseTotal(r), 0));
    t.at = r2(rows.reduce((s, r) => s + (num(r.at_retail_valuation) || 0), 0));
    t.atCount = rows.filter(r => num(r.at_retail_valuation)).length;
    t.cap = r2(rows.reduce((s, r) => s + (num(r.cap_clean) || 0), 0));
    t.capCount = rows.filter(r => num(r.cap_clean)).length;
    batchFields().filter(isSumField).forEach(f => {
        t['c_' + f.key] = r2(rows.reduce((s, r) => s + (num((r.custom || {})[f.key]) || 0), 0));
    });
    return t;
}

function renderTotals() {
    const t = rowTotals();
    $('vehFoot').innerHTML = `<tr><td colspan="6">Total (${rows.length})</td>` +
        MONEY_COLS.map(([k]) => `<td>${gbp(t[k])}</td>`).join('') +
        `<td>${gbp(t.total)}</td><td>${gbp(t.at)}</td><td>${gbp(t.cap)}</td>` +
        batchFields().map(f => `<td>${isSumField(f) ? gbp(t['c_' + f.key]) : ''}</td>`).join('') + '<td></td></tr>';

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
        ['Total Net', gbp(t.net)], ['Total VAT', gbp(t.vat)], ['Total Purchase', gbp(t.total)],
        ['Fees Gross', gbp(t.fees_gross)],
        ['Average Purchase Per Vehicle', gbp(r2(t.total / n))],
        ['Margin Vehicles', `${rows.filter(r => vatStatus(r) === 'Margin').length} of ${rows.length}`],
        ['From Evaluator', `${rows.filter(r => r.from_evaluator).length} of ${rows.length}`],
    ];
    if (t.atCount) stats.push(['AT Valuation Total', `${gbp(t.at)} (${t.atCount})`]);
    if (t.capCount) stats.push(['CAP Clean Total', `${gbp(t.cap)} (${t.capCount})`]);
    batchFields().filter(isSumField).forEach(f => stats.push([f.label + ' Total', gbp(t['c_' + f.key])]));
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

/* An empty Buyer or Supplier is highlighted everywhere it shows. */
function markMissingBatch() {
    ['bBuyer', 'bSupplier'].forEach(id => $(id).classList.toggle('cb-missing', !$(id).value.trim()));
}
['bBuyer', 'bSupplier'].forEach(id => {
    $(id).addEventListener('input', markMissingBatch);
    $(id).addEventListener('change', markMissingBatch);
});
['input', 'change'].forEach(type => $('bSupplier').addEventListener(type, refreshBatchFields));

/* Under the Supplier box: the text the supplier was matched from, or a way to add it. */
let batchInvoice = {};
function renderSupplierMatch() {
    const h = batchInvoice;
    let html = '';
    if (h.supplier_text) html = `Matched From Invoice Text: ${esc(h.supplier_text)}`;
    else if (h.found_text !== undefined && !h.supplier_matched) {
        html = 'Not Matched To A Supplier.';
        if (window.IS_ADMIN) html += ' <button type="button" class="btn btn-sm btn-link p-0 align-baseline" id="supFromBatchBtn">Add Supplier</button>';
    }
    $('supplierMatch').innerHTML = html;
    const btn = $('supplierMatch').querySelector('#supFromBatchBtn');
    if (btn) btn.addEventListener('click', () => openAddSupplier(batchInvoice));
}

/* Add Supplier popup (admin). invoice: {found_text, supplier_suggestions}. */
function openAddSupplier(invoice) {
    const text = invoice.found_text || '';
    $('supFoundText').textContent = text || 'This invoice was read before its text was kept. Press Read Again in Pending Invoices, then Add Supplier.';
    const ideas = invoice.supplier_suggestions || [];
    $('supSuggestions').innerHTML = ideas.length
        ? ideas.map((s, i) => `<button type="button" class="btn btn-sm btn-outline-secondary text-start" data-idea="${i}">${esc(s.text)}</button>`).join('')
        : '<span class="small text-muted">None Found. Select Text On The Left.</span>';
    const use = s => { $('supText').value = s.text; $('supName').value = s.supplier; };
    $('supSuggestions').onclick = e => { const b = e.target.closest('[data-idea]'); if (b) use(ideas[+b.dataset.idea]); };
    use(ideas[0] || {text: '', supplier: ''});
    $('supError').textContent = '';
    const known = new Set([...(buyerCfg.supplier_names || []).map(s => s.supplier), ...suppliers.map(s => s.supplier_name)]);
    $('supNameList').innerHTML = [...known].sort().map(n => `<option value="${esc(n)}">`).join('');
    $('supContact').innerHTML = '<option value="">Reading The Apex Suppliers...</option>';
    loadApexContacts().then(() => { $('supContact').innerHTML = contactOptions(); })
        .catch(err => { $('supContact').innerHTML = '<option value="">Not Mapped</option>'; $('supError').textContent = err.message; });
    modal('supplierModal').show();
}

if (window.IS_ADMIN) {
    $('supFoundText').addEventListener('mouseup', () => {
        const picked = String(window.getSelection ? window.getSelection() : '').trim();
        if (!picked) return;
        $('supText').value = picked.split('\n')[0].trim().slice(0, 100);
        if (!$('supName').value) $('supName').value = $('supText').value;
    });
    $('supSaveBtn').addEventListener('click', () => {
        const text = $('supText').value.trim(), supplier = $('supName').value.trim();
        const contact = $('supContact').selectedOptions[0];
        postJSON('/cars-bought/supplier-names', {text, supplier, contact_id: $('supContact').value, contact_name: contact?.dataset.name || ''}).then(res => {
            if (res.status !== 'ok') { $('supError').textContent = res.message; return; }
            buyerCfg.supplier_names = res.supplier_names;
            renderInbox(res.files || []);
            modal('supplierModal').hide();
            // The batch on screen, if this text is on its invoice.
            if ((batchInvoice.found_text || '').toLowerCase().includes(text.toLowerCase())) {
                Object.assign(batchInvoice, {supplier_text: text, supplier_matched: true});
                $('bSupplier').value = supplier;
                markMissingBatch();
                refreshBatchFields();
                renderSupplierMatch();
            }
            msg(`${esc(text)} now means ${esc(supplier)}. ${res.updated} pending invoice${res.updated === 1 ? '' : 's'} updated.`, 'success');
        }).catch(err => { $('supError').textContent = 'Error: ' + err.message; });
    });
}

function showBatch(header) {
    fillBuyers();
    $('batchCard').style.display = '';
    $('summaryCard').style.display = '';
    $('bSupplier').value = header.supplier_name || '';
    markMissingBatch();
    batchInvoice = header;
    renderSupplierMatch();
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
    // Typing must not rebuild the rows (the box would lose focus), so only these two cells change.
    $('vehBody').querySelector(`[data-status="${i}"]`).textContent = vatStatus(rows[i]);
    $('vehBody').querySelector(`[data-total="${i}"]`).textContent = gbp(purchaseTotal(rows[i]));
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

/* ── Source: SharePoint folder ─────────────────────────── */

let inboxTimer = null;
let inboxChecking = false;   // a look in the folder is running
let inboxSummary = '';       // what the last look found

const inboxBatch = new Set();   // files being read now, and those finished since the reading began

// How far a file being read has got, and what to call that step.
const INBOX_STEPS = {
    download: [20, 'Downloading From SharePoint'], read: [45, 'Reading The Invoice'], scan: [70, 'Reading The Scan (Textract)'],
    match: [85, 'Matching The Vehicles'], save: [95, 'Saving'],
};
const inboxStep = f => (f.status === 'queued' ? null : INBOX_STEPS[f.stage]) || [5, 'Waiting To Start'];
const inboxBar = pct => `<div class="inbox-bar"><div style="width:${pct}%"></div></div>`;

/* The status line under the buttons. bar: a percent for a progress bar, or
   'unknown' for a moving one when there is no way to tell how far it has got. */
function inboxLine(text, bar) {
    $('inboxLine').style.display = text ? '' : 'none';
    const html = bar === 'unknown'
        ? '<div class="progress mb-1" style="height:6px;"><div class="progress-bar progress-bar-striped progress-bar-animated" style="width:100%;"></div></div>'
        : bar === undefined ? '' : inboxBar(bar);
    $('inboxLine').innerHTML = html + esc(text);
}

function inboxFoundText(res) {
    const when = new Date().toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
    if (!res.in_folder) return `Checked at ${when}: the folder is empty.`;
    const parts = [`${res.found} new`, `${res.already_read} already read`];
    if (res.too_old) parts.push(`${res.too_old} saved before the Ignore Files date`);
    if (res.next_check) parts.push(`${res.next_check} left for the next check`);
    return `Checked at ${when}: ${res.in_folder} file${res.in_folder === 1 ? '' : 's'} in the folder, ${parts.join(', ')}.`;
}

function inboxStatus(f) {
    if (f.status === 'failed') return `<span class="tag tag-warn" title="${esc(f.error || '')}">Could Not Read</span> <span class="text-muted">${esc(f.error || '')}</span>`;
    if (f.status !== 'pending') { const [pct, label] = inboxStep(f); return inboxBar(pct) + esc(label); }
    const moved = f.moved_to ? '' : ` <span class="tag tag-warn" title="${esc(f.move_error || '')}">Not Moved</span>`;
    return `<span class="tag tag-info">Pending Review</span>${moved}`;
}

function renderInbox(files) {
    $('inboxCard').style.display = files.length ? '' : 'none';
    $('inboxBody').innerHTML = files.map(f => {
        const id = esc(f.file_id);
        const addSupplier = f.status === 'pending' && !f.supplier_matched && window.IS_ADMIN
            ? ` <button class="btn btn-sm btn-outline-danger" data-addsup="${id}">Add Supplier</button>` : '';
        const actions = f.status === 'pending' ? `<button class="btn btn-sm btn-primary" data-review="${id}">Review</button>${addSupplier}
                <button class="btn btn-sm btn-outline-secondary" data-reread="${id}" data-name="${esc(f.filename)}">Read Again</button>`
            : f.status === 'failed' ? `<button class="btn btn-sm btn-outline-primary" data-retry="${id}">Retry</button>` : '';
        const ignore = f.status === 'pending' || f.status === 'failed'
            ? ` <button class="btn btn-sm btn-outline-secondary" data-ignore="${id}" data-name="${esc(f.filename)}">Ignore</button>` : '';
        const link = `<a href="${BASE}/cars-bought/inbox/${encodeURIComponent(f.file_id)}/view" target="_blank" rel="noopener" title="Open the file">${esc(f.filename)}</a>`;
        return `<tr><td>${ukDate(f.found_at)}</td>${f.status === 'pending' ? missingCell(f.supplier_name) : `<td>${esc(f.supplier_name || '')}</td>`}<td>${link}</td><td>${esc(f.invoice_number || '')}</td>
            <td>${ukDate(f.invoice_date)}</td><td class="text-end">${f.vehicles ?? ''}</td>
            <td class="text-end">${f.invoice_gross == null ? '' : gbp(f.invoice_gross)}</td>
            <td style="white-space:normal;">${inboxStatus(f)}</td><td class="text-nowrap">${actions}${ignore}</td></tr>`;
    }).join('');
    // Ask again while a file is being read. The server fails a read that never
    // finishes, so this stops by itself.
    clearTimeout(inboxTimer);
    const reading = files.filter(f => f.status === 'queued' || f.status === 'reading');
    reading.forEach(f => inboxBatch.add(f.file_id));
    if (reading.length) inboxTimer = setTimeout(pollInbox, 2000);
    else inboxBatch.clear();
    if (inboxChecking) return;
    if (!reading.length) { inboxLine(inboxSummary); return; }
    // Files no longer being read count as finished, whether they were read or not.
    // Text only: each file's own row has the progress bar.
    const size = inboxBatch.size;
    inboxLine(`Read ${size - reading.length} of ${size} invoice${size === 1 ? '' : 's'}. ${inboxSummary}`);
}

function loadInbox() {
    return fetch(BASE + '/cars-bought/inbox').then(r => r.json()).then(res => renderInbox(res.files || []));
}

// One failed request (the server restarting, say) must not stop the page asking.
function pollInbox() {
    loadInbox().catch(() => { inboxTimer = setTimeout(pollInbox, 5000); });
}

function checkInbox() {
    const btn = $('inboxCheckBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Checking Folder...';
    // The server answers once, at the end, so this counts the seconds until then.
    const started = Date.now();
    const tick = () => inboxLine(`Looking in the SharePoint folder ${window.INBOX_FOLDER}... ${Math.round((Date.now() - started) / 1000)} seconds`, 'unknown');
    inboxChecking = true;
    tick();
    const ticker = setInterval(tick, 1000);
    const stop = () => { clearInterval(ticker); inboxChecking = false; };
    return postJSON('/cars-bought/inbox/check', {}).then(res => {
        stop();
        inboxSummary = res.status === 'ok' ? inboxFoundText(res) : 'The last check of the folder failed.';
        renderInbox(res.files || []);
        if (res.status !== 'ok') msg(esc(res.message), 'danger');
    }).catch(err => {
        stop();
        inboxSummary = 'The last check of the folder failed.';
        inboxLine(inboxSummary);
        throw err;
    }).finally(() => {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-folder-open me-1"></i>Check Folder';
    });
}

$('inboxCheckBtn').addEventListener('click', () => { checkInbox().catch(err => msg('Error: ' + esc(err.message), 'danger')); });

$('inboxBody').addEventListener('click', e => {
    const review = e.target.closest('[data-review]');
    if (review) { reviewInvoice(review.dataset.review); return; }
    const addSup = e.target.closest('[data-addsup]');
    if (addSup) {
        fetch(BASE + '/cars-bought/inbox/' + encodeURIComponent(addSup.dataset.addsup)).then(r => r.json()).then(res => {
            if (res.status !== 'ok') { msg(esc(res.message), 'danger'); return; }
            openAddSupplier(res);
        }).catch(err => msg('Error: ' + esc(err.message), 'danger'));
        return;
    }
    const reread = e.target.closest('[data-reread]');
    if (reread) {
        if (confirm(`Read ${reread.dataset.name} again? Any scan is charged again by Textract.`))
            postJSON(`/cars-bought/inbox/${encodeURIComponent(reread.dataset.reread)}/retry`, {}).then(res => renderInbox(res.files || []));
        return;
    }
    const retry = e.target.closest('[data-retry]');
    if (retry) { postJSON(`/cars-bought/inbox/${encodeURIComponent(retry.dataset.retry)}/retry`, {}).then(res => renderInbox(res.files || [])); return; }
    const ignore = e.target.closest('[data-ignore]');
    if (ignore && confirm(`Ignore ${ignore.dataset.name}? It will not be read again.`))
        postJSON(`/cars-bought/inbox/${encodeURIComponent(ignore.dataset.ignore)}/ignore`, {}).then(res => renderInbox(res.files || []));
});

function reviewInvoice(id) {
    if (!startNew()) return;
    busy('Opening the invoice...');
    msg('');
    fetch(BASE + '/cars-bought/inbox/' + encodeURIComponent(id)).then(r => r.json()).then(res => {
        busy('');
        if (res.status !== 'ok') { msg(esc(res.message), 'danger'); loadInbox(); return; }
        capChecked = new Set();
        rows = res.vehicles;
        batchMeta = {source: 'sharepoint', filename: res.filename, s3_key: res.s3_key, supplier_id: res.supplier_id, inbox_file_id: id};
        const view = `<a href="${BASE}/cars-bought/inbox/${encodeURIComponent(id)}/view" target="_blank" rel="noopener">${esc(res.filename)}</a>`;
        msg(`${rows.length} vehicle${rows.length === 1 ? '' : 's'} read from ${view}.`
            + ' Press Look Up CAP & AT, and check every row against the invoice before submitting.', 'success');
        showBatch(res);
    }).catch(err => { busy(''); msg('Error: ' + esc(err.message), 'danger'); });
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
            // Autotrader's /vehicles only fills these gaps: what the invoice said is kept.
            ['colour', 'first_registered'].forEach(k => { if (blank(r[k]) && !blank(c[k])) r[k] = c[k]; });
            if (!blank(c.cap_id)) r.cap_source = c.from_store ? 'stored' : 'live';
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
            loadInbox();
        })
        .catch(err => { busy(''); $('submitBtn').disabled = false; alert('Error: ' + err.message); });
});

/* ── History and export ────────────────────────────────── */

function loadHistory() {
    fetch(BASE + '/cars-bought/batches').then(r => r.json()).then(res => {
        const b = res.batches || [];
        $('histBody').innerHTML = b.length ? b.map(x => `<tr data-batch="${x.batch_id}">
            <td>${ukDate(x.purchase_date)}</td>${missingCell(x.buyer)}${missingCell(x.supplier_name)}<td>${esc(x.invoice_number || '')}</td>
            <td class="text-end">${x.vehicles}</td><td class="text-end">${gbp(x.rows_gross)}</td>
            <td class="text-end">${x.invoice_gross == null ? '—' : gbp(x.invoice_gross)}</td>
            <td>${x.source === 'evaluator' ? 'Evaluator' : x.has_file
                ? `<a href="${BASE}/cars-bought/batches/${x.batch_id}/view" target="_blank" rel="noopener" data-stop="1" title="Open the file">${esc(x.filename || 'Upload')}</a>`
                : esc(x.filename || 'Upload')}</td>
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

$('exportBtn').addEventListener('click', () => {
    const q = new URLSearchParams();
    if ($('expFrom').value) q.set('date_from', $('expFrom').value);
    if ($('expTo').value) q.set('date_to', $('expTo').value);
    window.location.href = BASE + '/cars-bought/export.csv?' + q.toString();
});

/* ── Admin settings ────────────────────────────────────── */

function cfRow(f) {
    const types = [['money', 'Money'], ['number', 'Number'], ['text', 'Text'], ['yesno', 'Yes / No']];
    return `<tr data-key="${esc(f.key || '')}">
        <td><input class="form-control form-control-sm cf-label" value="${esc(f.label || '')}"></td>
        <td><select class="form-select form-select-sm cf-type">${types.map(([v, l]) => `<option value="${v}"${f.type === v ? ' selected' : ''}>${l}</option>`).join('')}</select></td>
        <td class="text-center"><input type="checkbox" class="form-check-input cf-req"${f.required ? ' checked' : ''}></td>
        <td><input class="form-control form-control-sm cf-suppliers" placeholder="All Suppliers" value="${esc((f.suppliers || []).join(', '))}"></td>
        <td><button class="btn btn-sm btn-outline-danger cf-del"><i class="fas fa-times"></i></button></td></tr>`;
}

function snRow(s) {
    return `<tr>
        <td><input class="form-control form-control-sm sn-text" value="${esc(s.text || '')}"></td>
        <td><input class="form-control form-control-sm sn-supplier" value="${esc(s.supplier || '')}"></td>
        <td><button class="btn btn-sm btn-outline-danger sn-del" title="Delete"><i class="fas fa-times"></i></button></td></tr>`;
}

// The Apex suppliers (contacts_suppliers__s), read when the settings or Add
// Supplier popup opens. Null until read, and then the mappings are not sent on Save.
let apexContacts = null;

function loadApexContacts() {
    return fetch(BASE + '/cars-bought/supplier-contacts').then(r => r.json()).then(res => {
        if (res.status !== 'ok') throw new Error(res.message);
        apexContacts = res.contacts;
        return res;
    });
}

function contactOptions(currentId, currentName) {
    const known = apexContacts.some(c => String(c.id) === String(currentId));
    const gone = currentId && !known
        ? `<option value="${esc(currentId)}" data-name="${esc(currentName || '')}" selected>${esc(currentName || currentId)} (Not In Apex List)</option>` : '';
    return `<option value="">Not Mapped</option>${gone}` + apexContacts.map(c =>
        `<option value="${esc(c.id)}" data-name="${esc(c.name)}"${String(c.id) === String(currentId) ? ' selected' : ''}>${esc(c.label)}</option>`).join('');
}

function openApexSuppliers() {
    apexContacts = null;
    $('scSearch').value = '';
    $('scBody').innerHTML = '';
    $('scStatus').textContent = 'Reading The Apex Suppliers...';
    loadApexContacts().then(res => {
        $('scStatus').textContent = '';
        $('scBody').innerHTML = res.suppliers.map(s => `<tr data-sid="${s.supplier_id}">
            <td class="sc-name">${esc(s.supplier_name)}</td>
            <td><select class="form-select form-select-sm sc-contact">${contactOptions(s.contact_id, s.contact_name)}</select></td></tr>`).join('');
    }).catch(err => { $('scStatus').textContent = err.message; });
}

function apexMappings() {
    if (!apexContacts) return undefined;
    return [...$('scBody').querySelectorAll('tr')].map(tr => {
        const sel = tr.querySelector('.sc-contact');
        return {supplier_id: tr.dataset.sid, contact_id: sel.value, contact_name: sel.selectedOptions[0]?.dataset.name || ''};
    });
}

function filterSupplierNames() {
    const q = $('snSearch').value.trim().toLowerCase();
    $('snBody').querySelectorAll('tr').forEach(tr => {
        const line = (tr.querySelector('.sn-text').value + ' ' + tr.querySelector('.sn-supplier').value).toLowerCase();
        tr.style.display = !q || line.includes(q) ? '' : 'none';
    });
}

/* Read Figures Again: read Snowflake now for every metric that is on the All
   Cars Bought table. That page shows the new figures when it next opens. */
function readMetricsAgain() {
    const wanted = (buyerCfg.table_columns || []).filter(c => c.column.startsWith('METRIC:'))
        .map(c => (buyerCfg.metrics || []).find(m => m.id === c.column.slice(7).toLowerCase())).filter(m => m && m.enabled);
    return Promise.all(wanted.map(m => fetch(`${BASE}/cars-bought/metric-values/${encodeURIComponent(m.id)}?force=true`).then(r => r.json())
        .then(res => { if (res.status !== 'ok') throw new Error(res.message); })));
}

let tableColumnEditor = null;

function openTableColumnEditor() {
    const live = buyerCfg.table_live || [];
    tableColumnEditor = new ColumnEditor($('tableColEditor'), {
        live, shown: buyerCfg.table_columns || [],
        formats: [['text', 'Text'], ['number', 'Number'], ['gbp', 'Pounds'], ['pct', 'Percent'], ['date', 'Date']],
        numberFormats: ['number', 'gbp', 'pct'],
        newColumn: column => {
            const c = live.find(x => x.column === column) || {};
            return {column, label: c.label || '', format: c.format || 'text', decimals: c.decimals || 0};
        },
    });
}

function srRow(r) {
    return `<tr>
        <td><input class="form-control form-control-sm sr-supplier" value="${esc(r.supplier || '')}"></td>
        <td><input class="form-control form-control-sm sr-words" value="${esc((r.fee_words || []).join(', '))}"></td>
        <td class="text-center"><input type="checkbox" class="form-check-input sr-priced"${r.priced_lines_are_fees ? ' checked' : ''}></td>
        <td><button class="btn btn-sm btn-outline-danger sr-del"><i class="fas fa-times"></i></button></td></tr>`;
}

if (window.IS_ADMIN) {
    $('settingsBtn').addEventListener('click', () => {
        $('cfBody').innerHTML = customFields.map(cfRow).join('');
        openTableColumnEditor();
        window.openVehicleMetrics();
        $('tableRowsInput').value = buyerCfg.table_rows ?? 500;
        $('srBody').innerHTML = (buyerCfg.supplier_rules || []).map(srRow).join('');
        openApexSuppliers();
        $('lockBuyerInput').checked = buyerCfg.lock_buyer;
        const labels = buyerCfg.column_labels || {};
        $('colNamesBody').innerHTML = Object.keys(labels).map(k =>
            `<tr><td class="text-nowrap fw-semibold" style="width:170px">${esc(labels[k])}</td>
             <td><input class="form-control form-control-sm" data-colfield="${esc(k)}" value="${esc(((buyerCfg.column_names || {})[k] || []).join(', '))}"></td></tr>`).join('');
        $('feeWordsInput').value = (buyerCfg.fee_words || []).join(', ');
        $('feeVatRateInput').value = buyerCfg.fee_vat_rate ?? 20;
        $('evalHoursInput').value = buyerCfg.evaluator_max_hours ?? 24;
        $('lookupReuseInput').checked = buyerCfg.lookup_reuse !== false;
        // Sorted for finding a row: the longest match wins, so order does not change what matches.
        $('snBody').innerHTML = [...(buyerCfg.supplier_names || [])]
            .sort((a, b) => a.supplier.localeCompare(b.supplier) || a.text.localeCompare(b.text)).map(snRow).join('');
        $('snSearch').value = '';
        $('ownNamesInput').value = (buyerCfg.own_names || []).join(', ');
        $('gridColourInput').value = buyerCfg.grid_colour || '#d0d0d0';
        $('missingColourInput').value = buyerCfg.missing_colour || '#f8d7da';
        $('notUsedColourInput').value = buyerCfg.not_used_colour || '#e9ecef';
        $('inboxEnabledInput').checked = !!buyerCfg.inbox_enabled;
        $('inboxFolderInput').value = buyerCfg.inbox_folder || '';
        $('inboxDoneInput').value = buyerCfg.inbox_done_folder || '';
        $('inboxMaxInput').value = buyerCfg.inbox_max_files ?? 20;
        $('inboxSinceInput').value = buyerCfg.inbox_since || '';
        bootstrap.Modal.getOrCreateInstance($('settingsModal')).show();
    });
    $('cfAddBtn').addEventListener('click', () => $('cfBody').insertAdjacentHTML('beforeend', cfRow({type: 'money'})));
    $('cfBody').addEventListener('click', e => { const b = e.target.closest('.cf-del'); if (b) b.closest('tr').remove(); });
    $('snSearch').addEventListener('input', filterSupplierNames);
    $('scSearch').addEventListener('input', () => {
        const q = $('scSearch').value.trim().toLowerCase();
        $('scBody').querySelectorAll('tr').forEach(tr => {
            const line = (tr.querySelector('.sc-name').textContent + ' ' + (tr.querySelector('.sc-contact').selectedOptions[0]?.textContent || '')).toLowerCase();
            tr.style.display = !q || line.includes(q) ? '' : 'none';
        });
    });
    $('snAddBtn').addEventListener('click', () => {
        $('snSearch').value = '';
        filterSupplierNames();
        $('snBody').insertAdjacentHTML('afterbegin', snRow({}));
    });
    $('snBody').addEventListener('click', e => { const b = e.target.closest('.sn-del'); if (b) b.closest('tr').remove(); });
    $('srAddBtn').addEventListener('click', () => $('srBody').insertAdjacentHTML('beforeend', srRow({})));
    $('srBody').addEventListener('click', e => { const b = e.target.closest('.sr-del'); if (b) b.closest('tr').remove(); });
    $('settingsSaveBtn').addEventListener('click', () => {
        const split = s => s.split(',').map(x => x.trim()).filter(Boolean);
        const fields = [...$('cfBody').querySelectorAll('tr')].map(tr => ({
            key: tr.dataset.key || null, label: tr.querySelector('.cf-label').value,
            type: tr.querySelector('.cf-type').value, required: tr.querySelector('.cf-req').checked,
            suppliers: split(tr.querySelector('.cf-suppliers').value),
        }));
        // A misspelled supplier would hide the field from the supplier it was meant for.
        const known = [...suppliers.map(s => s.supplier_name), ...[...$('snBody').querySelectorAll('.sn-supplier')].map(el => el.value)];
        const unknown = [...new Set(fields.flatMap(f => f.suppliers))].filter(n => !known.some(k => sameName(k, n)));
        if (unknown.length && !confirm(`Custom Fields: not a known supplier: ${unknown.join(', ')}. Save anyway?`)) return;
        const column_names = {};
        $('colNamesBody').querySelectorAll('[data-colfield]').forEach(el => { column_names[el.dataset.colfield] = split(el.value); });
        const supplier_rules = [...$('srBody').querySelectorAll('tr')].map(tr => ({
            supplier: tr.querySelector('.sr-supplier').value, fee_words: split(tr.querySelector('.sr-words').value),
            priced_lines_are_fees: tr.querySelector('.sr-priced').checked,
        }));
        postJSON('/cars-bought/config', {custom_fields: fields, lock_buyer: $('lockBuyerInput').checked, supplier_rules,
                                         supplier_contacts: apexMappings(),
                                         column_names, fee_words: split($('feeWordsInput').value),
                                         fee_vat_rate: num($('feeVatRateInput').value),
                                         evaluator_max_hours: num($('evalHoursInput').value),
                                         lookup_reuse: $('lookupReuseInput').checked,
                                         table_columns: tableColumnEditor.columns(),
                                         table_rows: num($('tableRowsInput').value),
                                         grid_colour: $('gridColourInput').value,
                                         missing_colour: $('missingColourInput').value,
                                         not_used_colour: $('notUsedColourInput').value,
                                         inbox_enabled: $('inboxEnabledInput').checked,
                                         inbox_folder: $('inboxFolderInput').value,
                                         inbox_done_folder: $('inboxDoneInput').value,
                                         inbox_max_files: num($('inboxMaxInput').value),
                                         inbox_since: $('inboxSinceInput').value,
                                         own_names: split($('ownNamesInput').value),
                                         supplier_names: [...$('snBody').querySelectorAll('tr')].map(tr => ({
                                             text: tr.querySelector('.sn-text').value, supplier: tr.querySelector('.sn-supplier').value,
                                         }))}).then(res => {
            if (res.status !== 'ok') { alert(res.message); return; }
            customFields = res.custom_fields;
            buyerCfg = res;
            fillBuyers();
            $('inboxCheckBtn').style.display = res.inbox_enabled ? '' : 'none';
            window.INBOX_ENABLED = res.inbox_enabled;
            window.INBOX_FOLDER = res.inbox_folder;
            $('inboxFolderLink').href = res.inbox_folder_url;
            $('inboxFolderLink').style.display = res.inbox_enabled ? '' : 'none';
            document.documentElement.style.setProperty('--cb-grid', res.grid_colour);
            document.documentElement.style.setProperty('--cb-missing', res.missing_colour);
            document.documentElement.style.setProperty('--cb-not-used', res.not_used_colour);
            markMissingBatch();
            loadInbox().catch(() => {});
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
        suppliers.filter(s => s.has_stock_list).map(s => `<option value="${s.supplier_id}">${esc(s.supplier_name)}</option>`).join('');
    $('supplierList').innerHTML = suppliers.map(s => `<option value="${esc(s.supplier_name)}">`).join('');
    // From the Settings link on the All Cars Bought page.
    if (window.IS_ADMIN && location.hash === '#settings') $('settingsBtn').click();
});
loadHistory();
// What is already pending shows first; the slower look in the folder follows.
loadInbox().catch(() => {}).then(() => {
    if (window.INBOX_ENABLED) return checkInbox().catch(err => msg('Error: ' + esc(err.message), 'danger'));
});
