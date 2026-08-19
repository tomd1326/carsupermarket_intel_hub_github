/* ── Formatters ──────────────────────────────────────────────────────────── */
function fmtGBP(v) {
    if (v == null) return '—';
    // Sign before the symbol: -£1,234, never £-1,234.
    const n = Number(v);
    return (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function fmtInt(v) {
    if (v == null) return '—';
    return Math.round(Number(v)).toLocaleString('en-GB');
}
function fmtPct(v) {
    if (v == null) return '—';
    return Number(v).toFixed(1) + '%';
}
function normaliseFuel(raw) {
    const s = String(raw ?? '').toLowerCase();
    if (s.includes('petrol'))              return 'Petrol';
    if (s.includes('diesel'))              return 'Diesel';
    if (s.includes('electric') || s==='ev') return 'Electric';
    if (s.includes('hybrid'))              return 'Hybrid';
    return raw || '—';
}
function vehicleStatus(raw) {
    const s = String(raw ?? '').toUpperCase();
    if (s === 'ORDERED') return 'Ordered';
    if (s === 'SOLD')    return 'Delivered';
    return 'Stock';
}
function statusRank(status) {
    if (status === 'Stock')     return 0;
    if (status === 'Ordered')   return 1;
    if (status === 'Delivered') return 2;
    return 3;
}

/* ── State ───────────────────────────────────────────────────────────────── */
let allRows       = [];
let sortCol       = 'deriv';   // default: alphabetical by make/model/deriv
let sortAsc       = true;
let currentFontPx = 12;
const activeFilters = { status: 'All', band: 'All', fuel: 'All' };
const _today = new Date(); _today.setHours(0,0,0,0);

/* ── Snowflake status ────────────────────────────────────────────────────── */
function setStatus(state) {
    const el = document.getElementById('sf-status');
    if (!el) return;
    const label = '<i class="fas fa-snowflake me-1 text-info"></i>Snowflake';
    const map = {
        idle:    `${label} <span class="text-muted">—</span>`,
        running: `${label} <span class="spinner-border spinner-border-sm ms-1" style="width:.7rem;height:.7rem"></span>`,
        done:    `${label} <i class="fas fa-check-circle text-success ms-1"></i>`,
        error:   `${label} <i class="fas fa-times-circle text-danger ms-1"></i>`,
    };
    el.innerHTML = map[state] ?? map.idle;
}

/* ── Fetch ───────────────────────────────────────────────────────────────── */
async function fetchData(force = false) {
    const btn = document.getElementById('fetchBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Loading…'; }
    document.getElementById('fetch-loading').style.display = 'block';
    document.getElementById('data-section').style.display  = 'none';
    document.getElementById('table-wrap').classList.add('d-none');
    document.getElementById('table-error').classList.add('d-none');
    setStatus('running');
    try {
        const url  = BASE_URL + '/buying-requirements/sf-data' + (force ? '?force=true' : '');
        const res  = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.detail || 'Unknown error');
        allRows = data.rows;
        setStatus('done');
        if (data.cached_at) {
            const d = new Date(data.cached_at * 1000);
            document.getElementById('sf-status').title = 'Cached at ' + d.toLocaleTimeString('en-GB');
        }
        // Apex updated at — max vehicle_updated_at (r[28]) across all rows
        let maxApex = null;
        allRows.forEach(r => {
            const v = r[28];
            if (v && (!maxApex || v > maxApex)) maxApex = v;
        });
        if (maxApex) {
            const d = new Date(String(maxApex).replace(' ', 'T'));
            document.getElementById('apex-updated-val').textContent =
                d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
            document.getElementById('apex-updated').style.display = '';
        }
        document.getElementById('fetch-loading').style.display = 'none';
        document.getElementById('data-section').style.display  = '';
        document.getElementById('table-wrap').classList.remove('d-none');
        applyFilters();
    } catch (e) {
        setStatus('error');
        document.getElementById('fetch-loading').style.display = 'none';
        const errEl = document.getElementById('table-error');
        errEl.textContent = 'Failed to load data: ' + e.message;
        errEl.classList.remove('d-none');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Refresh Data'; }
    }
}

/* ── Summary bar ─────────────────────────────────────────────────────────── */
function updateSummary(filtered) {
    let stock = 0, ordered = 0, delivered = 0;
    filtered.forEach(r => {
        const s = vehicleStatus(r[21]);
        if (s === 'Stock')     stock++;
        else if (s === 'Ordered')   ordered++;
        else if (s === 'Delivered') delivered++;
    });
    document.getElementById('sum-stock').textContent     = stock.toLocaleString('en-GB');
    document.getElementById('sum-ordered').textContent   = ordered.toLocaleString('en-GB');
    document.getElementById('sum-delivered').textContent = delivered.toLocaleString('en-GB');
    const dosVal = parseInt(document.getElementById('dosInput')?.value) || 0;
    const anyFilter = activeFilters.status !== 'All' || activeFilters.band !== 'All' ||
                      activeFilters.fuel !== 'All' || dosVal > 0 ||
                      (document.getElementById('searchInput')?.value ?? '') !== '';
    document.getElementById('sum-filtered-note').style.display = anyFilter ? '' : 'none';
}

/* ── Filters ─────────────────────────────────────────────────────────────── */
function applyFilters() {
    const search  = (document.getElementById('searchInput')?.value ?? '').toLowerCase();
    const maxDays = parseInt(document.getElementById('dosInput')?.value) || 0;
    const clearBtn = document.getElementById('dosClearBtn');
    if (clearBtn) clearBtn.style.display = maxDays > 0 ? '' : 'none';

    const filtered = allRows.filter(r => {
        const status = vehicleStatus(r[21]);
        const band   = r[3] || '';
        const fuel   = normaliseFuel(r[2]);
        if (activeFilters.status !== 'All' && status !== activeFilters.status) return false;
        if (activeFilters.band   !== 'All' && band   !== activeFilters.band)   return false;
        if (activeFilters.fuel   !== 'All' && fuel   !== activeFilters.fuel)   return false;
        // Days of Sales: only filters Ordered/Delivered rows — Stock always passes
        if (maxDays > 0 && status !== 'Stock') {
            const orderRaw = r[17];
            if (!orderRaw) return false;
            const orderDate = new Date(String(orderRaw).slice(0, 10));
            orderDate.setHours(0,0,0,0);
            const days = Math.round((_today - orderDate) / 86400000);
            if (days > maxDays) return false;
        }
        if (search) {
            const deriv = (r[1] || '').toLowerCase();
            const vrm   = (r[18] || '').toLowerCase();
            if (!deriv.includes(search) && !vrm.includes(search)) return false;
        }
        return true;
    });
    updateSummary(filtered);
    renderTable(filtered);
}

function clearDosFilter() {
    document.getElementById('dosInput').value = '';
    document.getElementById('dosClearBtn').style.display = 'none';
    applyFilters();
}

function clearAllFilters() {
    activeFilters.status = 'All';
    activeFilters.band   = 'All';
    activeFilters.fuel   = 'All';
    document.getElementById('searchInput').value = '';
    document.getElementById('dosInput').value    = '';
    document.getElementById('dosClearBtn').style.display = 'none';
    document.querySelectorAll('.filter-btn').forEach(btn => {
        const isAll = btn.dataset.value === 'All';
        btn.classList.toggle('btn-dark', isAll);
        btn.classList.toggle('btn-outline-secondary', !isAll);
    });
    applyFilters();
}

document.addEventListener('click', e => {
    const btn = e.target.closest('.filter-btn');
    if (!btn) return;
    const group = btn.dataset.group;
    const value = btn.dataset.value;
    if (!group || !value) return;
    activeFilters[group] = value;
    document.querySelectorAll(`.filter-btn[data-group="${group}"]`).forEach(b => {
        b.classList.toggle('btn-dark', b.dataset.value === value);
        b.classList.toggle('btn-outline-secondary', b.dataset.value !== value);
    });
    applyFilters();
});

/* ── Sort ────────────────────────────────────────────────────────────────── */
function getSortValue(r, col) {
    const status = vehicleStatus(r[21]);
    if (col === 'deriv')          return (r[1] || '').toLowerCase();
    if (col === 'price_band')     return (r[3] || '').toLowerCase();
    if (col === 'vrm')            return (r[18] || '').toLowerCase();
    if (col === 'status')         return statusRank(status);
    if (col === 'fuel')           return normaliseFuel(r[2]).toLowerCase();
    if (col === 'location')       return (r[23] || '').toLowerCase();
    if (col === 'delivered')      return r[27] || '';
    if (col === 'supplier')       return (r[19] || '').toLowerCase();
    if (col === 'dfr')            return r[24] || '';
    if (col === 'mileage')        return Number(r[25]) || 0;
    if (col === 'dis')            return Number(r[7])  || 0;
    if (col === 'dts')            return Number(r[8])  || 0;
    if (col === 'purchase_price') return Number(r[12]) || 0;
    if (col === 'cap_clean')      return Number(r[30]) || 0;
    if (col === 'cap_pct')        return (r[12] && r[30]) ? r[12] / r[30] : 0;
    if (col === 'sell_price')     return Number(r[13]) || 0;
    if (col === 'at_ptm')         return Number(r[14]) || 0;
    if (col === 'at_ptm_pct')     return Number(r[15]) || 0;
    if (col === 'at_rating')      return Number(r[16]) || 0;
    if (col === 'gpu_m')          return Number(r[22]) || 0;
    return '';
}

function sortRows(rows) {
    return [...rows].sort((a, b) => {
        const av = getSortValue(a, sortCol);
        const bv = getSortValue(b, sortCol);
        let cmp = av < bv ? -1 : av > bv ? 1 : 0;
        // secondary: always alphabetical on deriv
        if (cmp === 0 && sortCol !== 'deriv') {
            const ad = (a[1] || '').toLowerCase();
            const bd = (b[1] || '').toLowerCase();
            cmp = ad < bd ? -1 : ad > bd ? 1 : 0;
        }
        return sortAsc ? cmp : -cmp;
    });
}

function updateSortIndicators() {
    document.querySelectorAll('#stock-table thead th[data-sort]').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        const arrow = th.querySelector('.sort-arrow');
        if (!arrow) return;
        if (th.dataset.sort === sortCol) {
            th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
            arrow.textContent = sortAsc ? ' ▲' : ' ▼';
            arrow.style.opacity = '1';
        } else {
            arrow.textContent = '';
            arrow.style.opacity = '0.35';
        }
    });
}

document.querySelectorAll('#stock-table thead th[data-sort]').forEach(th => {
    th.addEventListener('click', e => {
        if (e.target.classList.contains('resize-handle')) return;
        const col = th.dataset.sort;
        if (sortCol === col) {
            sortAsc = !sortAsc;
        } else {
            sortCol = col;
            sortAsc = true;
        }
        updateSortIndicators();
        applyFilters();
    });
});

/* ── Column resize ───────────────────────────────────────────────────────── */
function syncTableWidth() {
    const table = document.getElementById('stock-table');
    const cols  = table.querySelectorAll('colgroup col');
    let total = 0;
    cols.forEach(c => { total += parseInt(c.style.width) || 0; });
    table.style.width = total + 'px';
}

(function initResize() {
    const ghost   = document.getElementById('resize-ghost');
    const table   = document.getElementById('stock-table');
    const cols    = Array.from(table.querySelectorAll('colgroup col'));

    document.querySelectorAll('#stock-table thead th').forEach((th, colIdx) => {
        const handle = document.createElement('div');
        handle.className = 'resize-handle';
        th.appendChild(handle);

        let startX, startW, rafId;

        handle.addEventListener('mousedown', e => {
            e.stopPropagation();
            e.preventDefault();
            startX = e.clientX;
            startW = th.offsetWidth;
            ghost.style.left    = e.clientX + 'px';
            ghost.style.display = 'block';
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';

            let lastX = startX;
            let dragged = false;

            function onMove(e) {
                dragged = true;
                lastX = e.clientX;
                cancelAnimationFrame(rafId);
                rafId = requestAnimationFrame(() => {
                    ghost.style.left = lastX + 'px';
                });
            }
            function cleanup(commitX) {
                cancelAnimationFrame(rafId);
                ghost.style.display = 'none';
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup',   onUp);
                document.removeEventListener('click',     suppressClick, true);
                window.removeEventListener('blur',        onBlur);
                if (commitX !== null && dragged) {
                    const newW = Math.max(30, startW + (commitX - startX));
                    if (cols[colIdx]) cols[colIdx].style.width = newW + 'px';
                    syncTableWidth();
                }
            }
            // Capture-phase click suppressor — fires before the th sort handler
            function suppressClick(e) { e.stopPropagation(); }
            function onUp(e)  { cleanup(e.clientX); }
            function onBlur() { cleanup(null); }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',   onUp);
            document.addEventListener('click',     suppressClick, true);
            window.addEventListener('blur',        onBlur);
        });
    });
})();

/* ── Render ──────────────────────────────────────────────────────────────── */
function renderTable(rows) {
    const sorted = sortRows(rows);
    const tbody  = document.getElementById('stock-tbody');
    tbody.innerHTML = '';

    sorted.forEach(r => {
        const status = vehicleStatus(r[21]);
        const fuel   = normaliseFuel(r[2]);
        const deriv  = (r[1] || '').split(' / ').slice(0, -1).join(' / ');
        const tr     = document.createElement('tr');

        if (status === 'Delivered')       tr.classList.add('vehicle-row-sold');
        else if (status === 'Ordered')    tr.classList.add('vehicle-row-ordered');
        else if (r[3] === 'interest_free') tr.classList.add('vehicle-row-if');

        const td = (text, cls) => {
            const el = document.createElement('td');
            el.textContent = text;
            if (cls) el.className = cls;
            tr.appendChild(el);
            return el;
        };
        const tdLink = (text, href) => {
            const el = document.createElement('td');
            el.className = 'col-center';
            if (href) {
                const a = document.createElement('a');
                a.href = href; a.target = '_blank';
                a.textContent = text; a.className = 'apex-link';
                el.appendChild(a);
            }
            tr.appendChild(el);
        };

        td(deriv, 'col-left');
        td(r[3] === 'interest_free' ? '0%' : (r[3] || '—'), 'col-left');
        td(r[18] || '—', 'col-left');
        td(status, 'col-left');
        tdLink('Apex',   r[20] || null);
        tdLink('Portal', r[26] || null);
        td(fuel, 'col-left');
        td(r[23] || '—', 'col-left');
        td(r[27] ? r[27].slice(0,10).split('-').reverse().join('/') : '—', 'col-left');
        td(r[19] || '—', 'col-left');
        td(r[24] ? r[24].slice(0,10).split('-').reverse().join('/') : '—', 'col-left');
        td(r[25] != null ? Number(r[25]).toLocaleString('en-GB') : '—', 'col-right');
        td(r[7]  != null ? fmtInt(r[7])  : '—', 'col-right');
        td(r[8]  != null ? fmtInt(r[8])  : '—', 'col-right');
        td(r[12] != null && r[12] !== 0 ? fmtGBP(r[12]) : '—', 'col-right');
        td(r[30] != null && r[30] !== 0 ? fmtGBP(r[30]) : '—', 'col-right');
        td((r[12] && r[30]) ? ((r[12]/r[30])*100).toFixed(1)+'%' : '—', 'col-right');
        td(r[13] != null && r[13] !== 0 ? fmtGBP(r[13]) : '—', 'col-right');
        td(r[14] != null ? fmtGBP(r[14]) : '—', 'col-right');
        td((r[13] && r[14]) ? fmtPct((r[13]/r[14])*100) : '—', 'col-right');
        td(r[16] != null ? fmtInt(r[16]) : '—', 'col-right');
        const gpuTd = td(r[22] != null ? fmtGBP(r[22]) : '—', 'col-right');
        if (r[22] != null && r[22] < 0) gpuTd.style.color = '#dc2626';

        tbody.appendChild(tr);
    });

    document.getElementById('row-count').textContent =
        sorted.length.toLocaleString('en-GB') + ' vehicles';
}

/* ── Font size ───────────────────────────────────────────────────────────── */
function changeFontSize(delta) {
    currentFontPx = Math.min(16, Math.max(8, Math.round((currentFontPx + delta * 0.5) * 10) / 10));
    document.getElementById('stock-table').style.fontSize = currentFontPx + 'px';
    document.getElementById('fontSizeLabel').textContent  = currentFontPx + 'px';
}

/* ── Boot ────────────────────────────────────────────────────────────────── */
document.getElementById('searchInput').addEventListener('keydown', e => { if (e.key === 'Enter') applyFilters(); });
document.getElementById('dosInput').addEventListener('keydown',    e => { if (e.key === 'Enter') applyFilters(); });

updateSortIndicators();
syncTableWidth();
fetchData();
