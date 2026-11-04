/* Cars Bought: what the Add Cars page (cars_bought.js) and the All Cars Bought
 * table (cars_bought_table.js) share. Loaded before either. */

const BASE = window.BASE_URL || '';
let customFields = [];
let buyerCfg = {buyers: [], lock_buyer: false, me: ''};

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

/* A car with no VAT shown on it is margin. Its fees have their own VAT, which
   can be different, and do not count. Imported from the old sheet with no VAT
   given, it is not known. Same as vat_status() in utils/cars_bought.py. */
const vatStatus = r => r.source === 'import' && num(r.vehicle_vat) === null ? 'Unknown'
    : num(r.vehicle_vat) ? 'VAT Qualifying' : 'Margin';

/* What the car cost in all: the car, every fee and all VAT. Same as
   purchase_total() in utils/cars_bought.py. */
function purchaseTotal(r) {
    const gross = num(r.vehicle_gross);
    const car = gross !== null ? gross : (num(r.vehicle_net) || 0) + (num(r.vehicle_vat) || 0);
    return r2(car + (num(r.fees_gross) || 0));
}

const isSumField = f => f.type === 'money' || f.type === 'number';
const yesNoOptions = v => ['', 'Yes', 'No'].map(o => `<option value="${o}"${o === v ? ' selected' : ''}>${o}</option>`).join('');

/* Whether a custom field is for this supplier. No suppliers set means all of
   them. Same as field_applies() in utils/cars_bought.py. */
const sameName = (a, b) => String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase();
const fieldApplies = (f, supplier) => !(f.suppliers || []).length || f.suppliers.some(n => sameName(n, supplier));

/* An empty Buyer or Supplier is highlighted everywhere it shows. */
const missingCell = value => (value || '').trim() ? `<td>${esc(value)}</td>` : '<td class="cb-missing">Missing</td>';

function openBatch(id) {
    fetch(BASE + '/cars-bought/batches/' + id).then(r => r.json()).then(res => {
        const v = res.vehicles || [];
        $('batchModalTitle').textContent = v.length ? `Batch ${id}: ${v[0].supplier_name}, ${ukDate(v[0].purchase_date)}` : `Batch ${id}`;
        v.forEach(r => { r.vat_status = vatStatus(r); r.purchase_total = purchaseTotal(r); });
        const cols = [['vrm', 'VRM'], ['mileage', 'Mileage'], ['cap_id', 'CAP ID'], ['make', 'Make'], ['model', 'Model'],
            ['derivative', 'Derivative'], ['vat_status', 'VAT Status'], ['vehicle_net', 'Vehicle Net', 1], ['vehicle_vat', 'Vehicle VAT', 1],
            ['vehicle_gross', 'Vehicle Gross', 1], ['fees_net', 'Fees Net', 1], ['fees_vat', 'Fees VAT', 1], ['fees_gross', 'Fees Gross', 1],
            ['purchase_total', 'Total Purchase', 1]];
        // The supplier's fields, plus any other that already holds a value.
        const fields = customFields.filter(f => fieldApplies(f, v.length ? v[0].supplier_name : '') || v.some(r => ((r.custom || {})[f.key] ?? '') !== ''));
        $('batchModalBody').innerHTML = `<table class="table table-sm veh-table"><thead><tr>${cols.map(c => `<th>${c[1]}</th>`).join('')}${fields.map(f => `<th>${esc(f.label)}</th>`).join('')}</tr></thead><tbody>` +
            v.map(r => `<tr>${cols.map(([k, , m]) => `<td class="${m ? 'text-end' : ''}">${m ? gbp(r[k]) : esc(r[k] ?? '')}</td>`).join('')}${fields.map(f => `<td>${esc((r.custom || {})[f.key] ?? '')}</td>`).join('')}</tr>`).join('') +
            '</tbody></table>';
        bootstrap.Modal.getOrCreateInstance($('batchModal')).show();
    });
}
