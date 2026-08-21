/* Vehicle detail rendering, shared by the Assess Vehicle list page and the
   per-vehicle page.

   Every function here writes into elements the host page provides by id
   (capBody, atBody, brBody, presenceRow, presenceDetail, checksBody,
   staleBanner, hdrImage). A page that omits one simply does not call the
   matching render function. */

const $ = id => document.getElementById(id);

/* ── Formatting helpers ──────────────────────────── */
function money(v) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    // Sign before the symbol: -£1,234, never £-1,234.
    return isNaN(n) ? '—' : (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB', {maximumFractionDigits: 0});
}
function num(v, dp) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    return isNaN(n) ? '—' : n.toLocaleString('en-GB',
        {minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0});
}
function txt(v) { return (v == null || v === '') ? '—' : String(v); }
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function kv(label, value, cls) {
    return `<div class="kv"><span class="k">${esc(label)}</span>` +
           `<span class="v ${cls || ''}">${value}</span></div>`;
}
function valBox(label, value, accent) {
    return `<div class="val-box ${accent ? 'accent' : ''}">` +
           `<div class="vb-label">${esc(label)}</div><div class="vb-value">${value}</div></div>`;
}
function stat(label, value, cls) {
    return `<div class="stat ${cls || ''}"><div class="s-label">${esc(label)}</div>` +
           `<div class="s-value">${value}</div></div>`;
}
function bandLabel(b) { return b === 'interest_free' ? '0%' : (b || '—'); }

function describe(d) {
    const at = d.autotrader || {}, cap = d.cap || {};
    return [at.make, at.model, at.derivative].filter(Boolean).join(' ')
        || at.description
        || [cap.make, cap.model, cap.derivative].filter(Boolean).join(' ')
        || 'Vehicle not identified';
}

function ageText(hours) {
    if (hours == null) return '';
    if (hours < 1) return Math.max(1, Math.round(hours * 60)) + ' min';
    if (hours < 48) return Math.round(hours) + ' hr';
    return Math.round(hours / 24) + ' days';
}

/* ── Fetch helpers ───────────────────────────────── */
// The lookup endpoints always answer with JSON. An HTML reply means the
// request never got there — usually a redirect to the login page after the
// session expired. Parsing that as JSON gives "Unexpected token '<'", which
// tells you nothing, so say what actually happened.
function parseJsonOrExplain(r, body) {
    const trimmed = (body || '').trim();
    try {
        return JSON.parse(trimmed);
    } catch (e) {
        if (trimmed.startsWith('<')) {
            throw new Error(`Got a web page instead of data (HTTP ${r.status}) — your session has probably expired. Reload the page and try again.`);
        }
        throw new Error(`Unexpected reply from the server (HTTP ${r.status}). Check the terminal for the error.`);
    }
}

function jsonFetch(url, options) {
    return fetch(url, options)
        .then(r => r.text().then(body => ({r, body})))
        .then(({r, body}) => {
            const data = parseJsonOrExplain(r, body);
            if (!r.ok) throw new Error(data.detail || `Request failed (HTTP ${r.status}).`);
            return data;
        });
}

/* ── Vehicle image ───────────────────────────────── */
// Hidden unless one loads — plenty of vehicles have no image, and CAP returns
// an error for those, so the header has to look right without it rather than
// showing a broken picture.
function renderImage(cap) {
    const img = $('hdrImage');
    if (!img) return;
    img.style.display = 'none';
    if (!cap.cap_id) { img.removeAttribute('src'); return; }

    let url = BASE_URL + '/assess-vehicle/image/' + encodeURIComponent(cap.cap_id);
    // The registration date picks the model year current at the time, so a
    // facelifted model shows the version this car actually is.
    if (cap.registered) url += '?date=' + encodeURIComponent(cap.registered);

    img.onload = () => { img.style.display = ''; };
    img.onerror = () => { img.style.display = 'none'; };
    img.onclick = () => openImage(url);
    img.title = 'Click to enlarge';
    img.src = url;
}

// The stored image is already 600x448, so the enlarged view reuses the same URL
// the thumbnail loaded — served from the browser cache, no second fetch.
function openImage(url) {
    $('imgLightboxPic').src = url;
    $('imgLightbox').style.display = '';
}

function closeImage() {
    $('imgLightbox').style.display = 'none';
    $('imgLightboxPic').removeAttribute('src');
}

/* ── Detail blocks ───────────────────────────────── */

// Presence and Buying Requirements arrive after the valuation — they need the
// SharePoint sheets, which are much slower than CAP/Autotrader. These show the
// wait in place rather than leaving the blocks blank or, worse, looking like a
// confirmed "not found".
function showPresenceLoading() {
    $('presenceRow').innerHTML =
        '<div class="presence loading"><i class="fas fa-spinner fa-spin"></i>' +
        '<span>Checking Apex, Daily Buyers Sheet and Cars Bought...</span></div>';
    $('presenceDetail').innerHTML = '';
}

function showPresenceError(msg) {
    $('presenceRow').innerHTML =
        `<div class="presence"><i class="fas fa-triangle-exclamation"></i>` +
        `<span>Could not check the sheets: ${esc(msg || 'request failed')}</span></div>`;
    $('presenceDetail').innerHTML = '';
}

function showBrLoading() {
    $('brBody').innerHTML =
        '<div class="empty-hint"><i class="fas fa-spinner fa-spin me-1"></i>Loading...</div>';
}

function showBrError(msg) {
    $('brBody').innerHTML = `<div class="err-box">${esc(msg || 'request failed')}</div>`;
}

function renderPresence(p) {
    const badge = (on, label) =>
        `<div class="presence ${on ? 'on' : ''}">` +
        `<i class="fas fa-${on ? 'check-circle' : 'circle-xmark'}"></i>` +
        `<span>${esc(label)}</span></div>`;

    $('presenceRow').innerHTML =
        badge(!!p.apex, 'Apex' + (p.apex ? '' : ' — not found')) +
        badge(!!p.dbs, 'Daily Buyers Sheet' + (p.dbs ? '' : ' — not found')) +
        badge(!!p.cb, 'Cars Bought' + (p.cb ? '' : ' — not found'));

    const blocks = [];
    if (p.apex) {
        blocks.push(['Apex', [
            kv('Status', txt(p.apex.vehicle_status)),
            kv('Supplier', txt(p.apex.supplier)),
            kv('Location', txt(p.apex.location)),
            kv('Price band', bandLabel(p.apex.price_band)),
            kv('Purchase price', money(p.apex.purchase_price), 'mono'),
            kv('Selling price', money(p.apex.selling_price), 'mono'),
        ].join('')]);
    }
    if (p.dbs) {
        blocks.push(['Daily Buyers Sheet', [
            kv('Supplier', txt(p.dbs.supplier)),
            kv('Price band', bandLabel(p.dbs.band)),
            kv('SIV', money(p.dbs.siv), 'mono'),
            kv('RRP', money(p.dbs.rrp), 'mono'),
            kv('Date of registration', txt(p.dbs.dor)),
        ].join('')]);
    }
    if (p.cb) {
        // Read straight off the TO GO ON sheet, unlike Apex/DBS above — no CAP
        // ID match or price band is required, so this only ever has the plain
        // sheet columns (buyer, supplier, mileage, date bought), not SIV/RRP/band.
        blocks.push(['Cars Bought', [
            kv('Supplier', txt(p.cb.supplier)),
            kv('Buyer', txt(p.cb.buyer)),
            kv('Mileage', num(p.cb.mileage)),
            kv('Date bought', txt(p.cb.date)),
        ].join('')]);
    }

    $('presenceDetail').innerHTML = blocks.length
        ? '<div class="row g-3">' + blocks.map(([title, html]) =>
            `<div class="col-12 col-md-4"><div class="section-title">${esc(title)}</div>${html}</div>`
          ).join('') + '</div>'
        : '';
}

function renderCap(cap) {
    if (cap.error) { $('capBody').innerHTML = `<div class="err-box">${esc(cap.error)}</div>`; return; }
    if (!cap.cap_id && cap.note) { $('capBody').innerHTML = `<div class="err-box">${esc(cap.note)}</div>`; return; }
    $('capBody').innerHTML =
        kv('VIN', txt(cap.vin), 'mono') +
        kv('CAP ID', txt(cap.cap_id), 'mono') +
        kv('Make', txt(cap.make)) +
        kv('Model', txt(cap.model)) +
        kv('Derivative', txt(cap.derivative)) +
        '<div class="val-grid">' +
            valBox('Clean Live',   money(cap.live_clean), true) +
            valBox('Clean Month',  money(cap.monthly_clean)) +
            valBox('Retail Live',  money(cap.live_retail)) +
            valBox('Retail Month', money(cap.monthly_retail)) +
        '</div>';
}

/* ── Collapsible option lists ────────────────────────
   Shared by the HPI and Autotrader cards so both read the same way: a heading
   with a count, click to expand, one row per option with its price on the right.

   State is one object per section keyed by a unique id, holding true or false
   explicitly. An earlier version used "empty set means use the defaults", which
   could not represent "the user closed the section that starts open" — closing
   it emptied the set, the set being empty meant fall back to the default, and
   the default re-opened it. */
const optOpen = {};

function optSection(id, label, items, openByDefault, opts) {
    if (!items || !items.length) return '';
    const o = opts || {};
    const open = id in optOpen ? optOpen[id] : openByDefault;
    const rows = items.map((it, i) => {
        const price = it.price ? `<span class="opt-price">${money(it.price)}</span>` : '';
        const badges = (it.badges || [])
            .map(b => `<span class="spec-badge ${b.cls || ''}">${esc(b.text)}</span>`).join('');
        // The long description is hidden behind an info icon rather than always
        // shown — the short name is what the buying team scans, the full
        // manufacturer wording is only needed occasionally.
        const detailId = `${id}_detail_${i}`;
        const infoBtn = it.detail
            ? `<button type="button" class="opt-info" title="More detail"
                   onclick="toggleOptDetail('${detailId}')">
                   <i class="fas fa-circle-info"></i></button>`
            : '';
        const long = it.detail
            ? `<div class="opt-detail" id="${detailId}" style="display:none;">${esc(it.detail)}</div>`
            : '';
        // A deselected VIN-spec item stays in the list (never silently
        // disappears) but reads as "turned off" rather than "fitted".
        const offNote = it.deselected
            ? `<span class="opt-off">Deselected</span>` : '';
        return `<div class="opt-item ${it.deselected ? 'opt-item-off' : ''}">
            <div class="opt-item-head">
                <span class="opt-name">${esc(it.name)}</span>
                ${badges ? `<span class="spec-badges">${badges}</span>` : ''}${offNote}${infoBtn}${price}
            </div>${long}
        </div>`;
    }).join('');
    const note = o.note ? `<div class="opt-note">${esc(o.note)}</div>` : '';
    return `<div class="spec-group opt-group">
        <button type="button" class="spec-cat ${open ? 'open' : ''}"
                onclick="toggleOptSection('${id}')">
            <i class="fas fa-${open ? 'minus' : 'plus'}"></i>
            <span>${esc(label)}</span>
            <span class="spec-chosen">(${items.length})</span>
        </button>
        <div class="spec-items" ${open ? '' : 'style="display:none;"'}>${note}${rows}</div>
    </div>`;
}

// Flips one section and re-renders whichever card owns it. Each card keeps its
// own last-rendered data so the re-render does not need another request.
function toggleOptSection(id) {
    const current = id in optOpen ? optOpen[id] : OPT_DEFAULT_OPEN.has(id);
    optOpen[id] = !current;
    if (id.startsWith('hpi')) renderHpi(hpiData);
    else if (id.startsWith('at')) renderAtOptions();
}

// Sections that start expanded. Needed by toggleOptSection so the first click
// on one of these closes it rather than reopening it.
const OPT_DEFAULT_OPEN = new Set(['hpiOptional', 'atFitted']);

// Shows/hides one option's long description, toggled by its info icon.
function toggleOptDetail(id) {
    const el = $(id);
    if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

/* ── HPI Spec Check ──────────────────────────────────
   Factory-fitted options from cap hpi. Every request is a billable check, so
   the card only ever shows what is already stored — running one takes a button
   press and a confirmation. Once stored it is never re-requested: the options
   were fitted when the car was built and do not change. */

let hpiData = null;

function renderHpi(spec) {
    hpiData = spec;
    const el = $('hpiBody');
    const src = $('hpiSrc');

    if (!spec || !spec.found) {
        if (src) src.textContent = 'Not run';
        el.innerHTML =
            '<div class="empty-hint mb-2">No spec check has been run for this vehicle.</div>' +
            '<button class="btn btn-primary w-100" onclick="openHpiConfirm()">' +
            '<i class="fas fa-list-check me-1"></i>Run Spec Check</button>' +
            '<div class="hpi-cost-note">Billable — one HPI check</div>';
        return;
    }

    if (src) src.textContent = spec.fetched_at ? 'Checked ' + spec.fetched_at : 'Spec Check';

    if (spec.blocked) {
        el.innerHTML = '<div class="err-box">Spec Check is blocked for this make.</div>';
        return;
    }
    if (!spec.available) {
        el.innerHTML =
            '<div class="empty-hint">' +
            esc(spec.status_message || 'No spec data available for this vehicle.') +
            '</div>' +
            '<div class="hpi-cost-note">Stored, so it will not be charged again.</div>';
        return;
    }

    const optional = spec.optional || [];

    // The rating is how much the extras are worth against the list price —
    // 1 is under 2%, 5 is over 16%.
    const rating = spec.options_rating;
    const ratingHtml = rating == null ? '—'
        : `<span class="hpi-rating hpi-rating-${Math.floor(rating)}">${num(rating, rating % 1 ? 1 : 0)}</span>`;

    el.innerHTML =
        '<div class="val-grid">' +
            valBox('Options Value', money(spec.options_price), true) +
            valBox('List Price', money(spec.base_price)) +
        '</div>' +
        kv('Total when new', money(spec.total_price), 'mono') +
        kv('Options rating', ratingHtml) +
        optSection('hpiOptional', 'Optional extras',
                   optional.map(o => ({
                       name: o.short_description,
                       price: o.price,
                       detail: o.long_description,
                   })), true);
}

function openHpiConfirm() {
    $('hpiConfirmVrm').textContent = VRM;
    const btn = $('hpiConfirmBtn');
    btn.disabled = false;
    btn.innerHTML = 'Run Spec Check';
    $('hpiConfirm').style.display = '';
}

function closeHpiConfirm() { $('hpiConfirm').style.display = 'none'; }

function runSpecCheck() {
    const btn = $('hpiConfirmBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Checking...';
    $('hpiBody').innerHTML =
        '<div class="empty-hint"><i class="fas fa-spinner fa-spin me-1"></i>Running spec check...</div>';
    closeHpiConfirm();

    jsonFetch(BASE_URL + '/assess-vehicle/spec-check/' + encodeURIComponent(VRM), {method: 'POST'})
        .then(renderHpi)
        .catch(e => {
            $('hpiBody').innerHTML =
                `<div class="err-box">${esc(e.message || 'Spec check failed')}</div>` +
                '<button class="btn btn-outline-secondary w-100 mt-2" onclick="openHpiConfirm()">' +
                'Try again</button>';
        });
}

function loadSpecCheck() {
    // Read-only — shows a stored result if there is one, never calls HPI.
    jsonFetch(BASE_URL + '/assess-vehicle/spec-check/' + encodeURIComponent(VRM))
        .then(renderHpi)
        .catch(() => renderHpi(null));
}

// Autotrader Portal pricing page for this vehicle. Needs the mileage as well as
// the VRM, so it is passed in rather than read off the Autotrader block.
function portalLink(vrm, mileage) {
    if (!vrm || !mileage) return '';
    const url = 'https://portal.autotrader.co.uk/managing/pricing/vrm/'
        + encodeURIComponent(vrm) + '/' + encodeURIComponent(mileage);
    return `<a href="${url}" target="_blank" rel="noopener" class="portal-link">` +
           `<i class="fas fa-arrow-up-right-from-square me-1"></i>Portal</a>`;
}

// Last Autotrader result rendered, so an options section can be expanded or
// collapsed without another lookup.
let atData = null;
let atVrm = null, atMileage = null;

function renderAt(at, vrm, mileage) {
    atData = at; atVrm = vrm; atMileage = mileage;
    $('atPortal').innerHTML = portalLink(vrm, mileage);
    if (at.error) { $('atBody').innerHTML = `<div class="err-box">${esc(at.error)}</div>`; return; }
    if (!at.found) { $('atBody').innerHTML = '<div class="empty-hint">Vehicle not found on Autotrader.</div>'; return; }
    const r = at.retail_rating;
    const ratingHtml = r ? `<span class="rating-pill rating-${String(r).toLowerCase()}">${esc(r)}</span>` : '—';
    // Stored rows keep one description rather than separate make/model/
    // derivative, so show whichever shape this result has.
    const idRows = at.make
        ? kv('Make', txt(at.make)) + kv('Model', txt(at.model)) + kv('Derivative', txt(at.derivative))
        : kv('Vehicle', txt(at.description || at.derivative));
    // Three retail figures, because they answer different questions:
    //   Average  - a typical car of this derivative (Autotrader's own baseline)
    //   No spec  - the same car stripped of every optional extra
    //   Spec     - this car's actual extras, once picked
    // The Portal states the difference against the AVERAGE, so this does too.
    const hasSpec = at.spec_valuation != null;
    const current = hasSpec ? at.spec_valuation : at.base_valuation;
    const avg = at.average_valuation;
    const uplift = (current != null && avg != null) ? current - avg : null;

    const upliftHtml = uplift == null ? '' :
        `<div class="spec-uplift ${uplift >= 0 ? 'pos' : 'neg'}">` +
        `<i class="fas fa-${uplift >= 0 ? 'circle-check' : 'circle-minus'} me-1"></i>` +
        `Spec: ${money(Math.abs(uplift))} ${uplift >= 0 ? 'above' : 'below'} average</div>`;

    // The current selection, not the figure last valued with — they can differ
    // once a VIN-spec seed has run but nobody has pressed Apply since.
    const selected = at.selected_features || [];
    const vinCount = (at.vin_spec_features || []).filter(n => selected.includes(n)).length;
    const specCount = selected.length;
    const vinNote = vinCount
        ? `<span class="spec-vin-note"><i class="fas fa-check-double me-1"></i>${vinCount} from Autotrader VIN spec</span>`
        : '';
    const specRow =
        `<div class="spec-row">` +
        `<span class="spec-count">${specCount ? specCount + ' extra' + (specCount === 1 ? '' : 's') + ' selected' : 'No extras selected'}${vinNote}</span>` +
        `<button class="btn-spec" onclick="openSpecPicker()">` +
        `<i class="fas fa-pen me-1"></i>${specCount ? 'Edit spec' : 'Add spec'}</button>` +
        `</div>`;

    // Only show the three-way breakdown once there is something to compare;
    // before any spec work the single retail figure is all Autotrader gave us.
    const haveBreakdown = avg != null || at.base_valuation != null;

    $('atBody').innerHTML =
        kv('VIN', txt(at.vin), 'mono') +
        idRows +
        kv('Retail rating', ratingHtml) +
        (haveBreakdown
            ? '<div class="val-grid val-grid-3 mt-2">' +
                valBox('Average Spec', money(avg)) +
                valBox('No Spec', money(at.base_valuation)) +
                valBox('This Car', money(hasSpec ? at.spec_valuation : null), true) +
              '</div>' + upliftHtml
            : '<div class="val-grid">' +
                valBox('Retail Valuation', money(at.retail_valuation), true) +
              '</div>') +
        '<div class="val-grid mt-2">' +
            valBox('Days to Sell', num(at.days_to_sell)) +
        '</div>' +
        specRow +
        '<div id="atOptions"></div>';

    renderAtOptions();
}

/* Autotrader's own option lists, laid out like the HPI card so the two can be
   read side by side.

   "Fitted to this car" is the current selection — ticked in the picker, and for
   some vehicles seeded automatically from Autotrader's own VIN data on the
   first check. The full list of everything Autotrader offers for the
   derivative is not shown here — that is what the Edit spec picker is for, so
   it is not duplicated on the card.

   A VIN-spec item that has been un-ticked still shows, badged "VIN spec",
   rather than disappearing — the point is the buyer can always see what
   Autotrader itself says was fitted, even after deciding it looks wrong. */
function renderAtOptions() {
    const el = $('atOptions');
    if (!el || !atData) return;

    const selected = new Set(atData.selected_features || []);
    const vinSpec = new Set(atData.vin_spec_features || []);
    const names = new Set([...selected, ...vinSpec]);

    const fitted = Array.from(names).map(name => ({
        name,
        deselected: vinSpec.has(name) && !selected.has(name),
        badges: vinSpec.has(name) ? [{text: 'VIN spec', cls: 'vin'}] : [],
    }));
    // VIN-confirmed items first, deselected ones last, so the list reads as
    // "what's on the car" then "what Autotrader flagged but got turned off".
    fitted.sort((a, b) => (a.deselected === b.deselected) ? 0 : (a.deselected ? 1 : -1));

    const note = atData.vin_spec_features && atData.vin_spec_features.length
        ? 'VIN spec items came from Autotrader automatically. Anything else was picked by hand. Use Edit spec to change either.'
        : 'Selected by hand — Autotrader does not provide VIN spec for this vehicle. Use Edit spec to change it.';

    el.innerHTML = optSection('atFitted', 'Fitted to this car', fitted, true, {note});
}

function renderBr(br, cap) {
    if (!cap.cap_id) {
        $('brBody').innerHTML = '<div class="empty-hint">No CAP ID, so buying requirements cannot be matched.</div>';
        $('brBand').textContent = ''; return;
    }
    if (br.error) { $('brBody').innerHTML = `<div class="err-box">${esc(br.error)}</div>`; return; }
    if (!br.found) {
        $('brBody').innerHTML = '<div class="empty-hint">This CAP ID has no rows in buying requirements.</div>';
        $('brBand').textContent = ''; return;
    }

    $('brBand').textContent = 'Band ' + bandLabel(br.price_band);
    const reqCls = br.required > 0 ? 'pos' : (br.required < 0 ? 'neg' : '');
    const remCls = br.remaining > 0 ? 'pos' : (br.remaining < 0 ? 'neg' : '');

    $('brBody').innerHTML =
        '<div class="stat-grid mb-2">' +
            stat('Total Stock',   num(br.stock)) +
            stat('Stock on Sale', num(br.stock_on_sale)) +
            stat('90d Sales',     num(br.sales90)) +
            stat('30d Sales',     num(br.sales30)) +
        '</div>' +
        '<div class="stat-grid">' +
            stat('Ideal Stock',  num(br.ideal, 1), 'hl') +
            stat('Daily Buyers', br.dbs == null ? '—' : num(br.dbs)) +
            stat('Cars Bought',  br.cb == null ? '—' : num(br.cb)) +
            stat('Remaining to Buy', num(br.remaining, 1), remCls) +
        '</div>' +
        '<div class="mt-3">' +
            kv('Required (ideal − stock)', num(br.required, 1), 'mono ' + reqCls) +
            kv('Number to buy setting', num(br.number_to_buy), 'mono') +
        '</div>';
}

/* ── Spec picker ─────────────────────────────────── */
// Autotrader returns every optional extra available on the derivative, with a
// rarity and value rating for each. Which ones this car actually has is not
// something the API knows on this account, so they get ticked by hand.
let specFeatures = [];
let specSelected = new Set();
// Names Autotrader itself confirmed as factory-fitted for this VIN — badged in
// the picker the same way as on the vehicle card, so a buyer can tell "Autotrader
// confirmed this" apart from a manual tick even while choosing.
let specVinFeatures = new Set();
// Which category sections are expanded. Empty means "not touched yet", so the
// first render opens whichever sections already have something ticked.
let specOpen = new Set();

function openSpecPicker() {
    const el = $('specModal');
    el.style.display = '';
    $('specBody').innerHTML = '<div class="empty-hint"><i class="fas fa-spinner fa-spin"></i></div>';

    jsonFetch(BASE_URL + '/assess-vehicle/features/' + encodeURIComponent(VRM)
              + '?mileage=' + encodeURIComponent(MILEAGE))
        .then(d => {
            specFeatures = d.features || [];
            specSelected = new Set(d.selected || []);
            specVinFeatures = new Set(d.vin_features || []);
            specOpen = new Set();
            $('specSearch').value = '';
            renderSpecPicker();
            renderAtOptions();
        })
        .catch(err => {
            $('specBody').innerHTML = `<div class="err-box">${esc(err.message)}</div>`;
        });
}

function closeSpecPicker() { $('specModal').style.display = 'none'; }

function renderSpecPicker(filter) {
    const q = (filter || '').trim().toLowerCase();
    const shown = q
        ? specFeatures.filter(f => f.name.toLowerCase().includes(q))
        : specFeatures;

    if (!shown.length) {
        $('specBody').innerHTML = '<div class="empty-hint">No matching extras.</div>';
        updateSpecCount();
        return;
    }

    // Insertion order, not alphabetical — the API layer already returns them in
    // the order the Autotrader Portal lists its categories.
    const groups = new Map();
    shown.forEach(f => {
        if (!groups.has(f.category)) groups.set(f.category, []);
        groups.get(f.category).push(f);
    });

    $('specBody').innerHTML = Array.from(groups.entries()).map(([cat, items]) => {
        const chosen = items.filter(f => specSelected.has(f.name)).length;
        // A search collapses nothing — the point of filtering is to see matches.
        // Otherwise only sections with something ticked start open.
        const open = q ? true : (specOpen.has(cat) || (!specOpen.size && chosen > 0));
        const rows = items.map(f => {
            const badges =
                (specVinFeatures.has(f.name) ? `<span class="spec-badge vin">VIN spec</span>` : '') +
                (f.rarity_rating ? `<span class="spec-badge">${esc(f.rarity_rating)}</span>` : '') +
                (f.value_rating ? `<span class="spec-badge val">${esc(f.value_rating)}</span>` : '');
            const id = 'spec_' + btoa(unescape(encodeURIComponent(f.name))).replace(/[^a-zA-Z0-9]/g, '');
            return `<label class="spec-item" for="${id}">
                <input type="checkbox" id="${id}" ${specSelected.has(f.name) ? 'checked' : ''}
                       onchange="toggleSpec(this, ${JSON.stringify(f.name).replace(/"/g, '&quot;')})">
                <span class="spec-name">${esc(f.name)}</span>
                <span class="spec-badges">${badges}</span>
            </label>`;
        }).join('');
        return `<div class="spec-group">
            <button type="button" class="spec-cat ${open ? 'open' : ''}"
                    onclick="toggleSpecGroup(${JSON.stringify(cat).replace(/"/g, '&quot;')})">
                <i class="fas fa-${open ? 'minus' : 'plus'}"></i>
                <span>${esc(cat)}</span>
                ${chosen ? `<span class="spec-chosen">(${chosen} selected)</span>` : ''}
            </button>
            <div class="spec-items" ${open ? '' : 'style="display:none;"'}>${rows}</div>
        </div>`;
    }).join('');

    updateSpecCount();
}

function toggleSpec(input, name) {
    if (input.checked) specSelected.add(name); else specSelected.delete(name);
    // Deliberately no re-render — that would rebuild the checkbox mid-click.
    // The per-section "(n selected)" count refreshes on the next open/close.
    updateSpecCount();
}

function toggleSpecGroup(cat) {
    // First interaction seeds the set from what is currently open, so toggling
    // one section does not collapse the others the initial render expanded.
    if (!specOpen.size) {
        specFeatures.forEach(f => {
            if (specSelected.has(f.name)) specOpen.add(f.category);
        });
    }
    if (specOpen.has(cat)) specOpen.delete(cat); else specOpen.add(cat);
    renderSpecPicker($('specSearch').value);
}

function updateSpecCount() {
    const n = specSelected.size;
    $('specSaveBtn').textContent = n
        ? `Apply (${n}) extra${n === 1 ? '' : 's'}`
        : 'Apply with no extras';
}

function saveSpec() {
    const btn = $('specSaveBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Revaluing...';

    jsonFetch(BASE_URL + '/assess-vehicle/features/' + encodeURIComponent(VRM), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mileage: MILEAGE, features: Array.from(specSelected)})
    })
    .then(() => {
        closeSpecPicker();
        // Reload the vehicle so every block reflects the new valuation.
        jsonFetch(BASE_URL + '/assess-vehicle/lookup/' + encodeURIComponent(VRM)
                  + '?mileage=' + encodeURIComponent(MILEAGE))
            .then(d => render(d.result));
    })
    .catch(err => {
        btn.disabled = false;
        updateSpecCount();
        alert('Could not save the spec: ' + err.message);
    });
}

function renderChecks(checks) {
    if (!checks.length) {
        $('checksBody').innerHTML =
            '<div class="empty-hint">No previous checks recorded for this vehicle.</div>';
        return;
    }
    const rows = checks.map(c => {
        const badge = c.source === 'manual'
            ? '<span class="src-badge src-manual">Manual</span>'
            : '<span class="src-badge src-evaluator">Evaluator</span>';
        const who = c.source === 'manual'
            ? (c.by || '—')
            : (c.supplier || '—');
        const editMileage = typeof window.openMileageEditor === 'function'
            ? ` <button type="button" class="mileage-edit-btn" title="Reassess at a different mileage" ` +
              `onclick="window.openMileageEditor(${Number(c.mileage) || 0}, ${Number(c.id) || 'null'})"><i class="fas fa-pencil"></i></button>`
            : '';
        return `<tr>
            <td>${esc(c.when || '—')}</td>
            <td>${badge}</td>
            <td>${esc(who)}</td>
            <td class="num">${num(c.mileage)}${editMileage}</td>
            <td class="num">${money(c.cap_clean)}</td>
            <td class="num">${money(c.cap_retail)}</td>
            <td class="num">${money(c.at_retail)}</td>
            <td class="num">${c.at_rating == null ? '—' : num(c.at_rating, 1)}</td>
        </tr>`;
    }).join('');

    $('checksBody').innerHTML = `
        <div style="overflow-x:auto;">
        <table class="checks-table">
            <thead><tr>
                <th>When</th><th>Type</th><th>By / Supplier</th>
                <th class="num">Mileage</th><th class="num">CAP Clean</th>
                <th class="num">CAP Retail</th><th class="num">AT Retail</th>
                <th class="num">AT Rating</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        </div>`;
}
