/* Orders & Deliveries report - page behaviour.
 *
 * Loaded after shading.js (whose shadeScale()/shadeColour() this uses) and
 * after the template's inline config block, which sets:
 *   window.BASE_URL    - URL prefix for every fetch on this page
 *   window.FONT_SIZES  - the user's saved page/vehicle font sizes
 * Both are read at load time, so this file must stay after that block.
 */
const BASE_URL = window.BASE_URL;


        // Column definitions: key -> {label, type}
        // type: 'text' | 'num' | 'money' | 'pct' | 'date' | 'link' (link uses linkKey)
        const COLS = [
            { key: 'vrm', label: 'VRM' },
            { key: 'order_date', label: 'Order Date', type: 'date' },
            { key: 'delivered', label: 'Delivered', type: 'date' },
            // Make, model and derivative in one column — built by vehicleDesc()
            // (declared below; only called at render time, so order is fine).
            { key: 'description', label: 'Description', derived: r => vehicleDesc(r) },
            // Deliberately untyped, though the value is a number: 'num' would
            // format it as 12,345 and right-align it. It is an identifier, not
            // a quantity, so it renders as plain text.
            { key: 'cap_id', label: 'CAP ID' },
            { key: 'supplier', label: 'Supplier' },
            { key: 'ack_dis', label: 'DIS', type: 'num',
              info: 'Days In Stock — purchase date to acknowledged date' },
            { key: 'dis_band', label: 'DIS Band' },
            { key: 'dts', label: 'DTS', type: 'num' },
            { key: 'purch_price', label: 'Purch', type: 'money' },
            { key: 'sell_price_incl_fee', label: 'Sell', type: 'money',
              info: 'Selling price before discount, including the admin fee actually charged on the deal' },
            // Apex deep links. Two-word labels so the header wraps onto two
            // lines, keeping these at the fixed icon-column width. The AT
            // Portal link is the same shape but sits next to AT Valuation.
            { key: 'vehicle_url', label: 'Apex Vehicle', type: 'link' },
            { key: 'order_url', label: 'Apex Order', type: 'link' },
            { key: 'invoice_url', label: 'Apex Invoice', type: 'link' },
            { key: 'price_band', label: 'Price Band' },
            { key: 'discount', label: 'Discount', type: 'money' },
            { key: 'finance_subsidy', label: 'Finance Subsidy', type: 'money' },
            { key: 'chassis_mgn_aft_subs', label: 'Chassis', type: 'money',
              info: 'Chassis margin — sale price minus purchase price, discount and VAT cost, plus admin fee margin, after finance subsidy' },
            { key: 'vehicle_costs', label: 'Costs', type: 'money' },
            { key: 'write_down', label: 'Write Down', type: 'money' },
            { key: 'metal_mgn', label: 'Metal', type: 'money' },
            { key: 'gpu_m_costs', label: 'GPU-M Costs', type: 'money' },
            { key: 'gpu_m', label: 'GPU-M', type: 'money' },
            { key: 'at_val_order', label: 'AT Valuation', type: 'money' },
            // Autotrader deep link. Two-word label so the header wraps onto
            // two lines, keeping this at the fixed icon-column width.
            { key: 'at_portal_url', label: 'AT Portal', type: 'link' },
            { key: 'sell_pct_at', label: 'PTM %', type: 'pct',
              info: 'Price To Market — selling price after discount, including admin fee, as a percentage of the Autotrader market valuation. Averaged per vehicle, so it will not equal the Sell and AT Valuation columns divided together.' },
            { key: 'at_rating', label: 'AT Rating', type: 'num' },
            { key: 'finance_comm', label: 'Finance Comm', type: 'money' },
            { key: 'product_mgn', label: 'Product Mgn', type: 'money' },
            { key: 'gpu_s_costs', label: 'GPU-S Costs', type: 'money' },
            { key: 'gpu_s', label: 'GPU-S', type: 'money' },
            { key: 'gpu_ms', label: 'GPU-MS', type: 'money' },
            { key: 'purchase_cap_pct', label: 'Purchase CAP %', type: 'pct' },
            { key: 'fuel', label: 'Fuel' },
            { key: 'colour', label: 'Colour' },
            { key: 'mileage', label: 'Mileage', type: 'num' },
            { key: 'mileage_band', label: 'Mileage Band' },
            { key: 'dfr', label: 'DFR', type: 'date' },
            { key: 'age_band', label: 'Age Band' },
            { key: 'supplier_channel', label: 'Supplier Channel' },
            { key: 'site', label: 'Site' },
            { key: 'sales_type', label: 'Sales Type' },
            { key: 'cap_live_purchase', label: 'CAP Live at Purchase', type: 'money' },
            { key: 'cap_live_order', label: 'CAP Live at Order', type: 'money' },
        ];

        // Lens config: which date field drives filtering/labels, and which
        // deal statuses are kept. Mirrors the two Power Query variants.
        const LENS = {
            // The day key is the lens's own date column, which is already a
            // yyyy-mm-dd string in the payload — unlike week and month, which
            // are precomputed period columns from sales_performance__p. So a
            // day period needs nothing new from the server.
            orders:     { dateKey: 'order_date', label: 'Order date',    statuses: ['Acknowledged', 'Delivered'],
                          periodKeys: { day: 'order_date', week: 'order_week',     month: 'order_month' } },
            deliveries: { dateKey: 'delivered',  label: 'Delivery date', statuses: ['Delivered'],
                          periodKeys: { day: 'delivered',  week: 'delivered_week', month: 'delivered_month' } },
        };

        let allRows = [];
        // Every control on the page — Lens, View, Group By, and all slicer
        // selections — is staged: clicking updates only the *Pending copy and
        // the button's own visual active state. Nothing about what's actually
        // displayed (rerender(), which reads the *Applied values) changes
        // until Apply is clicked, which copies every Pending value across.
        let lensApplied = 'orders', lensPending = 'orders';
        let viewApplied = 'summary', viewPending = 'summary';
        let granularityApplied = 'week', granularityPending = 'week';
        let sortKey = null, sortDir = 1;

        // ── Slicers ─────────────────────────────────────────────────────────
        // One panel per dimension, in display order. `key` is the row field;
        // 'period' is a synthetic key (Delivery/Order month) that follows the
        // lens. Selections are multi-select and cross-filter each other.
        const SLICERS = [
            { key: 'period',           label: 'Period' },
            { key: 'supplier_channel', label: 'Supplier Channel' },
            { key: 'supplier',         label: 'Supplier' },
            { key: 'sales_type',       label: 'Sales Type' },
            { key: 'fuel',             label: 'Fuel' },
            { key: 'price_band',       label: 'Price Band (Rate/Cash)' },
            { key: 'mileage_band',     label: 'Mileage Band' },
            { key: 'age_band',         label: 'Age Band' },
        ];
        // Slicer selections are LIVE — clicking a value filters the tables and
        // grid immediately. Only the three top toggles (Lens, View, Group By)
        // are staged behind the Apply button.
        const slicerSel = {};
        SLICERS.forEach(s => { slicerSel[s.key] = new Set(); });
        // Last pill clicked in each panel, per panel — the anchor a
        // shift-click extends a range from. Kept outside slicerSel because it
        // is interaction state, not part of the filter itself: it must not
        // affect what is filtered, only what a subsequent shift-click selects.
        const slicerAnchor = {};

        // Local yyyy-mm-dd for a Date. NEVER use toISOString().slice(0,10)
        // for this — that converts to UTC, so a local midnight in a positive
        // timezone (e.g. UK summer, UTC+1) rolls back to the previous day,
        // shifting an ISO week's Monday onto the prior Sunday and computing
        // the WRONG (previous) week. This was why the current week wasn't
        // being selected by default.
        function ymdLocal(d) {
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        }

        // ISO week key (yyyy-Www) for a yyyy-mm-dd date string. The rows carry
        // their own week keys from Snowflake — this is only used to build the
        // contiguous list of periods for the slicer and the default selection,
        // both of which work from today's date rather than from a row.
        //
        // It must therefore produce keys IDENTICAL to the SQL ones, including
        // sales__c's rule of folding week 53 into week 1 (see
        // order_acknowledged_week). Without that fold, a late-December pill
        // would carry a key no row could ever match.
        function isoWeekKey(dateStr) {
            const d = new Date(dateStr + 'T00:00:00');
            const target = new Date(d.getTime());
            const dayNum = (d.getDay() + 6) % 7;               // Mon=0..Sun=6
            target.setDate(target.getDate() - dayNum + 3);      // nearest Thursday
            const firstThursday = new Date(target.getFullYear(), 0, 4);
            const firstDayNum = (firstThursday.getDay() + 6) % 7;
            firstThursday.setDate(firstThursday.getDate() - firstDayNum + 3);
            let week = 1 + Math.round((target - firstThursday) / (7 * 86400000));
            if (week === 53) week = 1;                          // matches sales__c
            return `${target.getFullYear()}-W${String(week).padStart(2, '0')}`;
        }
        // Monday date (yyyy-mm-dd) for an ISO week key — used to sort weeks
        // chronologically and to compute the default selection.
        function isoWeekMonday(weekKey) {
            const [y, w] = weekKey.split('-W').map(Number);
            // Jan 4 is always in ISO week 1 — move it to that week's Thursday
            // first (same nearest-Thursday adjustment isoWeekKey uses), THEN
            // step back 3 days to Monday. Using Jan 4's own weekday directly
            // (without this adjustment) is off by however many days Jan 4
            // sits from the nearest Thursday — e.g. in 2026 (Jan 4 = Sunday)
            // it produced Monday = 30 Dec 2025 instead of the correct 29 Dec.
            const jan4 = new Date(y, 0, 4);
            const jan4DayNum = (jan4.getDay() + 6) % 7;       // Mon=0..Sun=6
            const week1Thursday = new Date(jan4);
            week1Thursday.setDate(jan4.getDate() - jan4DayNum + 3);
            const week1Monday = new Date(week1Thursday);
            week1Monday.setDate(week1Thursday.getDate() - 3);
            const monday = new Date(week1Monday);
            monday.setDate(week1Monday.getDate() + (w - 1) * 7);
            return monday;
        }

        const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const DAY_ABBR = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];   // Date.getDay() order

        // Human display label for a period key, e.g. "W30 20-Jul" for a week
        // key or "Jul 2026" for a month key. The raw key (yyyy-Www / yyyy-MM)
        // stays the actual filtering value everywhere — this is display only.
        function periodDisplayLabel(key) {
            // Day keys are plain dates (yyyy-mm-dd) — shown as "Mon 14-Sep".
            // Tested before the month pattern below, which would otherwise
            // never see them anyway, but the order keeps the intent obvious.
            if (/^\d{4}-\d{2}-\d{2}$/.test(key)) {
                const d = new Date(key + 'T00:00:00');
                return `${DAY_ABBR[d.getDay()]} ${d.getDate()}-${MONTH_ABBR[d.getMonth()]}`;
            }
            if (/^\d{4}-W\d{2}$/.test(key)) {
                const monday = isoWeekMonday(key);
                const weekNum = key.split('-W')[1];
                return `W${weekNum} ${monday.getDate()}-${MONTH_ABBR[monday.getMonth()]}`;
            }
            if (/^\d{4}-\d{2}$/.test(key)) {
                const [y, m] = key.split('-').map(Number);
                return `${MONTH_ABBR[m - 1]} ${y}`;
            }
            return key;   // '(blank)' or anything unrecognised
        }

        // The period follows both the lens (which date drives it) and the
        // granularity toggle (week vs month). Takes lens/granularity explicitly
        // — callers pass the Applied pair for real data, the Pending pair for
        // the slicer panels' live preview of what's being staged.
        // Reads the period key straight off the row — the four keys (order/
        // delivery x week/month) are built in sales_performance__p, so nothing
        // is recomputed here on every render. Format is unchanged from what the
        // client used to produce: 'yyyy-Www' and 'yyyy-MM'.
        function periodOf(r, lensVal, granularityVal) {
            return r[LENS[lensVal].periodKeys[granularityVal]] || null;
        }
        // Sales type is bucketed for slicing — any rate/cash finance is one
        // "Rate/Cash" option, interest-free is "0%". The split is built in
        // sales_performance__p as SALES_TYPE_BUCKET rather than derived here,
        // so it is not recomputed for every row on every slicer click.
        function slicerValue(r, key, lensVal, granularityVal) {
            if (key === 'period') return periodOf(r, lensVal, granularityVal);
            if (key === 'sales_type') return r.sales_type_bucket;
            return r[key];
        }
        // Normalise a slicer value to a trimmed string, or '' if empty/blank —
        // so blank supplier/band values don't become empty pills.
        function normVal(v) {
            if (v == null) return '';
            return String(v).trim();
        }

        // Explicit display order for slicers where alphabetical is wrong.
        const SLICER_ORDER = {
            sales_type: ['Rate/Cash', '0%', 'Unknown'],
            fuel: ['Petrol', 'Diesel', 'Electric', 'Hybrid', 'Plug-In Hybrid'],
            dis_band: ['0-14', '15-29', '30-44', '45-59', '60-89', '90+'],
        };
        // Heading reflects APPLIED lens/granularity — frozen until Apply,
        // same as everything else in the slicer bar.
        function slicerHeadingApplied(key) {
            if (key === 'period') {
                const noun = granularityApplied === 'day' ? 'Day'
                           : granularityApplied === 'week' ? 'Week' : 'Month';
                return (lensApplied === 'deliveries' ? 'Delivery ' : 'Order ') + noun;
            }
            return SLICERS.find(s => s.key === key).label;
        }

        // ── Font resizers (persisted per user in Aurora via /api/user-prefs) ──
        // Two independent controls:
        //   'page'    — everything except the vehicle grid. Sets --page-font on
        //               <body>, so every em-sized rule on the page follows it.
        //   'vehicle' — the vehicle grid only, resized from its own buttons
        //               next to that table's CSV button.
        // The pref keys are unchanged from when these were the summary/vehicle
        // sizes, so an existing saved preference still applies.
        // Defaults mirror _FONT_DEFAULT / _FONT_DEFAULT_VEHICLE in
        // orders_deliveries.py — keep the two in step.
        const FONT_MIN = 8, FONT_MAX = 20, FONT_DEFAULT = 13;
        const FONT_CONFIG = {
            page:    { prefKey: 'orders_deliveries_font_summary', cssVar: '--page-font',    labelId: 'font-size-label',         fallback: 13 },
            vehicle: { prefKey: 'orders_deliveries_font_vehicle', cssVar: '--vehicle-font', labelId: 'vehicle-font-size-label', fallback: 12 },
        };
        // Seeded from the server-rendered values (see :root and the labels'
        // initial text above) so this matches first paint exactly — loadFont()
        // still confirms against Aurora, but has nothing left to visibly change.
        const fontSizes = { page: window.FONT_SIZES.page, vehicle: window.FONT_SIZES.vehicle };
        const fontSaveTimers = {};

        function applyFont(which) {
            document.documentElement.style.setProperty(FONT_CONFIG[which].cssVar, fontSizes[which] + 'px');
            const label = document.getElementById(FONT_CONFIG[which].labelId);
            if (label) label.textContent = fontSizes[which];

            // Both tables size their columns by MEASURING text at the current
            // font (measureSummaryLabelWidth / measureVehicleColumnWidths), so a
            // font change needs a re-render, not just a CSS variable swap —
            // otherwise the columns keep their old widths and the filter panels
            // and control bar no longer line up with the summary card's edge.
            // Guarded on allRows so this does nothing during the initial load,
            // when loadFont() runs before any data has arrived.
            if (!allRows.length) return;
            if (which === 'page' && viewApplied === 'summary') renderSummary();
            if (which === 'vehicle' && viewApplied === 'vehicle') renderGrid();
        }

        function changeFont(which, delta) {
            fontSizes[which] = Math.min(FONT_MAX, Math.max(FONT_MIN, fontSizes[which] + delta));
            applyFont(which);
            // Debounced save so rapid clicks don't spam Aurora
            clearTimeout(fontSaveTimers[which]);
            fontSaveTimers[which] = setTimeout(() => saveFont(which), 500);
        }

        function resetFont(which) {
            fontSizes[which] = FONT_CONFIG[which].fallback;
            applyFont(which);
            clearTimeout(fontSaveTimers[which]);
            fontSaveTimers[which] = setTimeout(() => saveFont(which), 500);
        }

        async function saveFont(which) {
            try {
                await fetch(BASE_URL + '/api/user-prefs/' + FONT_CONFIG[which].prefKey, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(fontSizes[which]),
                });
            } catch (e) { /* non-fatal — font just won't persist this time */ }
        }

        async function loadFont() {
            await Promise.all(Object.keys(FONT_CONFIG).map(async which => {
                try {
                    const resp = await fetch(BASE_URL + '/api/user-prefs/' + FONT_CONFIG[which].prefKey);
                    if (resp.ok) {
                        const data = await resp.json();
                        const v = Number(data.value);
                        if (Number.isFinite(v) && v >= FONT_MIN && v <= FONT_MAX) fontSizes[which] = v;
                    }
                } catch (e) { /* fall back to default */ }
                applyFont(which);
            }));
        }

        function fmtMoney(v) {
            if (v === null || v === undefined || v === '') return '';
            const n = Math.round(Number(v));
            // Sign goes BEFORE the symbol: -£1,234, never £-1,234. Prepending
            // '£' to the formatted number puts the minus in the wrong place,
            // since toLocaleString renders it as "-1,234".
            return (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB');
        }
        function fmtNum(v) {
            if (v === null || v === undefined || v === '') return '';
            return Number(v).toLocaleString('en-GB');
        }
        function fmtPct(v) {
            if (v === null || v === undefined || v === '') return '';
            return (Number(v) * 100).toFixed(1) + '%';
        }
        // ISO 'yyyy-MM-dd' (what the API sends) -> 'dd/MM/yyyy'. Split on the
        // string rather than going through new Date(): a bare ISO date is
        // parsed as UTC, so formatting it back in a UK summer timezone can
        // land on the previous day.
        function fmtDate(v) {
            if (v === null || v === undefined || v === '') return '';
            const m = String(v).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/);
            return m ? `${m[3]}/${m[2]}/${m[1]}` : String(v);
        }
        function fmtCell(col, v) {
            // AT Rating comes through with decimals (e.g. 73.64) but is shown
            // as a whole number everywhere — rounded before fmtNum's own
            // formatting rather than given its own 'type', since every other
            // num column on this page genuinely wants fmtNum as-is.
            if (col.key === 'at_rating') return fmtNum(v === null || v === undefined || v === '' ? v : Math.round(Number(v)));
            switch (col.type) {
                case 'money': return fmtMoney(v);
                case 'num':   return fmtNum(v);
                case 'pct':   return fmtPct(v);
                case 'date':  return fmtDate(v);
                default:      return (v === null || v === undefined) ? '' : String(v);
            }
        }
        function isNeg(col, v) {
            return (col.type === 'money' || col.type === 'pct') && v !== null && v !== undefined && Number(v) < 0;
        }

        function escapeHtml(s) {
            const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
        }

        // Layer 1 — lens only (no slicers, no period). Everything downstream
        // (slicers, summary, grid) starts from this set. The date window
        // itself is just the Period slicer's selection, applied later via
        // passesSlicers like any other slicer.
        function baseRows(lensVal) {
            const cfg = LENS[lensVal];
            return allRows.filter(r => cfg.statuses.includes(r.deal_status) && !!r[cfg.dateKey]);
        }

        // Does a row pass all slicer selections in the given selection map,
        // optionally ignoring one panel (used so a panel's own values aren't
        // excluded by its own selection — that's what makes the cross-
        // filtering feel right).
        //
        // Always called with the APPLIED selection map + lensApplied/
        // granularityApplied — nothing on screen reflects a Pending value
        // until Apply is clicked.
        function passesSlicers(r, sel_map, lensVal, granularityVal, exceptKey) {
            for (const s of SLICERS) {
                if (s.key === exceptKey) continue;
                const sel = sel_map[s.key];
                // An empty selection means "no filter — show every row" for
                // every slicer EXCEPT Period. Weeks/months span the whole
                // history the page can hold, so an empty Period would load
                // every row ever seen instead of nothing — the opposite of
                // what Clear is for. So an empty Period matches NO rows: the
                // one case where the reverse of every other slicer is what
                // "too much data" actually needs.
                if (sel.size === 0) {
                    if (s.key === 'period') return false;
                    continue;
                }
                const v = normVal(slicerValue(r, s.key, lensVal, granularityVal));
                if (!v || !sel.has(v)) return false;
            }
            return true;
        }

        // Layer 2 — base rows that pass every (live) slicer. This is the
        // working set used by the summary tables, the grid, and the row count.
        function filteredRows() {
            return baseRows(lensApplied).filter(r => passesSlicers(r, slicerSel, lensApplied, granularityApplied));
        }

        // A column's value for one row. Derived columns (Description) build
        // theirs from several fields; everything else reads the row directly.
        // Used by the grid's sort, render, column measuring and CSV export, so
        // they all agree on what a column contains.
        function cellValue(col, r) {
            return col && col.derived ? col.derived(r) : r[col.key];
        }

        // The vehicle grid: filtered rows, sorted.
        function visibleRows() {
            const rows = filteredRows();
            if (sortKey) {
                const col = COLS.find(c => c.key === sortKey);
                const numeric = col && (col.type === 'num' || col.type === 'money' || col.type === 'pct');
                rows.sort((a, b) => {
                    let av = cellValue(col, a), bv = cellValue(col, b);
                    if (av == null) return 1; if (bv == null) return -1;
                    if (numeric) { av = Number(av); bv = Number(bv); return (av - bv) * sortDir; }
                    return String(av).localeCompare(String(bv)) * sortDir;
                });
            }
            return rows;
        }

        // ── Summary tables ───────────────────────────────────────────────────
        // One table per dimension, stacked. Each row = mean/sum over the rows
        // in that group; Grand Total = the same aggregation over every row in
        // the table (i.e. the filtered set the table is built from).
        const SUMMARY_COLS = [
            { key: 'units',        label: 'Units',              agg: 'count' },
            { key: 'units_pct',    label: 'Units %',            agg: 'pct_of_total' },
            { key: 'ack_dis',      label: 'DIS',                 agg: 'mean',
              info: 'Days In Stock — purchase date to acknowledged date' },
            { key: 'dts',          label: 'DTS',                 agg: 'mean',
              info: 'Days To Sell — first advertised date to acknowledged date' },
            { key: 'purch_price',  label: 'Purch',               agg: 'mean', money: true },
            { key: 'sell_price_incl_fee', label: 'Sell',       agg: 'mean', money: true,
              info: 'Selling price before discount, including the admin fee actually charged on the deal' },
            { key: 'discount',     label: 'Discount',            agg: 'mean', money: true },
            { key: 'chassis_mgn_aft_subs',  label: 'Chassis',    agg: 'mean', money: true,
              info: 'Chassis margin — sale price minus purchase price, discount and VAT cost, plus admin fee margin, after finance subsidy' },
            { key: 'vehicle_costs', label: 'Costs',              agg: 'mean', money: true },
            { key: 'metal_mgn',    label: 'Metal',               agg: 'mean', money: true },
            { key: 'gpu_m',        label: 'GPU-M',               agg: 'mean', money: true },
            { key: 'at_val_order', label: 'AT Valuation',        agg: 'mean', money: true },
            { key: 'sell_pct_at',  label: 'PTM %',               agg: 'mean',
              info: 'Price To Market — selling price after discount, including admin fee, as a percentage of the Autotrader market valuation. Averaged per vehicle, so it will not equal the Sell and AT Valuation columns divided together.' },
            { key: 'at_rating',    label: 'AT Rating',           agg: 'mean' },
            { key: 'fin_pen_pct',  label: 'Fin Pen %',           agg: 'fin_pen_pct' },
            { key: 'fin_comm_all', label: 'Fin Comm',            agg: 'fin_comm_all', money: true },
            { key: 'fin_comm_fin', label: 'Fin Comm / unit',     agg: 'fin_comm_financed', money: true },
            { key: 'product_mgn',  label: 'Product Mgn',         agg: 'mean', money: true },
            { key: 'gpu_s',        label: 'GPU-S',               agg: 'mean', money: true },
            { key: 'gpu_ms',       label: 'GPU-MS',              agg: 'mean', money: true },
        ];
        // Columns shaded per-column (relative min->max within the table, red to
        // yellow to green — same scale as the buying requirements page).
        // 'green' = higher is better; 'red' = higher is worse (DIS — longer in
        // stock is bad). Finance/product-related columns (Fin Pen %, Fin Comm,
        // Fin Comm/unit, Prod mgn) and Costs are deliberately NOT shaded.
        // Which columns, which way round, and the colours are all set in the
        // page's Settings popup (Highlighting tab) and rendered into the page as
        // window.OD_SHADING. applyShadingSettings() sets the values below from it.
        let SHADE_DIRECTION = {};
        let SHADE_COLS = new Set();
        let shadeOpts = {};

        function hexRgb(hex) {
            const n = parseInt(String(hex).slice(1), 16);
            return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
        }
        // Position on the scale (0 = worst, 1 = best) to a colour.
        function makeColourFn(s) {
            const lerp = (a, b, f) => Math.round(a + (b - a) * f);
            const mix = (from, to, f) => `rgb(${[0, 1, 2].map(i => lerp(from[i], to[i], f)).join(',')})`;
            if (s.scheme === 'single') {
                // Pale for the worst values, strong for the best. Never quite
                // full strength, so dark text stays readable.
                const white = [255, 255, 255], c = hexRgb(s.single_colour);
                return ratio => {
                    if (ratio == null || Number.isNaN(ratio)) return '';
                    const r = Math.min(1, Math.max(0, ratio));
                    return mix(white, c, 0.06 + 0.64 * r);
                };
            }
            const low = hexRgb(s.rag_low), mid = hexRgb(s.rag_mid), high = hexRgb(s.rag_high);
            return ratio => {
                if (ratio == null || Number.isNaN(ratio)) return '';
                const r = Math.min(1, Math.max(0, ratio));
                return r < 0.5 ? mix(low, mid, r * 2) : mix(mid, high, (r - 0.5) * 2);
            };
        }
        function applyShadingSettings(s) {
            shadeOpts = s;
            SHADE_DIRECTION = {};
            // A column on 'default' uses the page's default scheme; one with its
            // own scheme gets a colour function built from its own colours.
            const defaultFn = makeColourFn(s);
            shadeOpts.colourFns = {};
            Object.keys(s.columns || {}).forEach(k => {
                const c = s.columns[k];
                SHADE_DIRECTION[k] = c.direction === 'high-bad' ? 'red' : 'green';
                shadeOpts.colourFns[k] = c.scheme === 'default' ? defaultFn : makeColourFn(c);
            });
            SHADE_COLS = new Set(Object.keys(SHADE_DIRECTION));
            DAY_COUNT_MAX = s.day_cap || Infinity;
        }

        // Which values are shaded at all, and which set the column's min/max.
        // Day counts over a year are data problems (a vehicle never marked
        // sold, a bad purchase date), not genuinely slow stock — left in, a
        // single 900-day row stretches the scale so far that every real value
        // lands near the good end and the column reads uniformly green. These
        // rows are left UNSHADED entirely rather than pinned to the red end:
        // an anomaly is not "the worst case", it is bad data.
        // Set from the Highlighting tab's Day Cap (0 there = no cap).
        let DAY_COUNT_MAX = 365;
        const withinYear = v => v <= DAY_COUNT_MAX;
        applyShadingSettings(window.OD_SHADING);
        const SHADE_IN_SCALE = {
            ack_dis: withinYear,
            dts: withinYear,
        };

        function mean(vals) {
            const known = vals.filter(v => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v))).map(Number);
            if (!known.length) return null;
            return known.reduce((a, b) => a + b, 0) / known.length;
        }

        // Build one summary row (a group's rows -> the 16 column values).
        function summariseGroup(rows, totalUnits) {
            const out = { units: rows.length };
            out.units_pct = totalUnits ? rows.length / totalUnits : null;
            out.ack_dis = mean(rows.map(r => r.ack_dis));
            out.dts = mean(rows.map(r => r.dts));
            out.purch_price = mean(rows.map(r => r.purch_price));
            out.sell_price_incl_fee = mean(rows.map(r => r.sell_price_incl_fee));
            out.discount = mean(rows.map(r => r.discount));
            out.chassis_mgn_aft_subs = mean(rows.map(r => r.chassis_mgn_aft_subs));
            out.vehicle_costs = mean(rows.map(r => r.vehicle_costs));
            out.metal_mgn = mean(rows.map(r => r.metal_mgn));
            out.gpu_m = mean(rows.map(r => r.gpu_m));
            out.at_val_order = mean(rows.map(r => r.at_val_order));
            // Mean of each vehicle's own PTM %, matching daily_summary__p,
            // which averages sales_performance's per-vehicle
            // autotrader_price_position. Every vehicle counts once regardless
            // of its value.
            //
            // This deliberately does NOT equal the Sell and AT Valuation
            // columns beside it divided together — two reasons. The averaging
            // differs (a cheap car with an odd % barely moves either £ average
            // but pulls this one), and Sell is shown before the vehicle
            // discount while the % is measured after it, with Discount as its
            // own column. On a discounted group the on-screen division will be
            // visibly off; the figure that matches daily summary is the one
            // that matters here.
            out.sell_pct_at = mean(rows.map(r => r.sell_pct_at));
            out.at_rating = mean(rows.map(r => r.at_rating));
            out.product_mgn = mean(rows.map(r => r.product_mgn));
            out.gpu_s = mean(rows.map(r => r.gpu_s));
            out.gpu_ms = mean(rows.map(r => r.gpu_ms));

            // Fin Pen % = units on Rate finance / all units in the row
            // (0% and Cash both excluded from the numerator)
            const rateUnits = rows.filter(r => String(r.sales_type || '').trim().toLowerCase() === 'rate').length;
            out.fin_pen_pct = rows.length ? rateUnits / rows.length : null;

            // Avg Fin Comm = total commission / ALL units; Avg Fin Comm/unit =
            // total commission / units that actually earned commission.
            const commVals = rows.map(r => Number(r.finance_comm)).filter(v => !Number.isNaN(v));
            const commTotal = commVals.reduce((a, b) => a + b, 0);
            const financedCount = rows.filter(r => Number(r.finance_comm) > 0).length;
            out.fin_comm_all = rows.length ? commTotal / rows.length : null;
            out.fin_comm_fin = financedCount ? commTotal / financedCount : null;

            return out;
        }

        // Build a full summary table for one dimension: { key, heading, rows: [{label, ...cols}], total }
        // Full vehicle description: make/model plus derivative. Either part can
        // be blank, so they're joined only when both are present — otherwise a
        // missing derivative would leave a trailing space.
        function vehicleDesc(r) {
            return [r.make_model, r.derivative].map(v => normVal(v)).filter(Boolean).join(' ');
        }

        // The popup shows exactly the Vehicle view's columns, in the same
        // order — it IS COLS, not a copy or a subset. Anything added, removed
        // or reordered in COLS lands in both views automatically, and there is
        // no second list to keep in step. (It used to stop at GPU-MS and layer
        // on its own info text; both of those were sources of drift.)
        const MODAL_COLS = COLS;

        // Sentinel used as the View button's data-label on a Grand Total row —
        // no real group label can collide with it because group labels come
        // from row values, and '(blank)' is used for empty ones.
        const GRAND_TOTAL_LABEL = '__grand_total__';

        // ── Row expansion ───────────────────────────────────────────────────
        // Which rows are currently open, as "dimKey||label". Kept outside the
        // table data so it survives a re-render (slicer click, Apply) — the
        // rows are rebuilt every time, but what the user opened should stay
        // open. A row whose label no longer exists is simply never drawn.
        const expandedSummaryRows = new Set();
        const expandKey = (dimKey, label) => `${dimKey}||${label}`;

        // Summary tables the user has collapsed down to their heading row.
        // Kept per user in Aurora (same /api/user-prefs as the font sizes) so
        // a collapsed table stays collapsed on the next visit.
        const COLLAPSED_PREF_KEY = 'orders_deliveries_collapsed_tables';
        const collapsedTables = new Set();
        async function loadCollapsedTables() {
            try {
                const resp = await fetch(BASE_URL + '/api/user-prefs/' + COLLAPSED_PREF_KEY);
                if (!resp.ok) return;
                const data = await resp.json();
                if (Array.isArray(data.value)) data.value.forEach(k => collapsedTables.add(String(k)));
                if (allRows.length) renderSummary();
            } catch (e) { /* none collapsed */ }
        }
        async function saveCollapsedTables() {
            try {
                await fetch(BASE_URL + '/api/user-prefs/' + COLLAPSED_PREF_KEY, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify([...collapsedTables]),
                });
            } catch (e) { /* non-fatal - it just will not persist this time */ }
        }

        // One sub-row per selected period for an expanded group: the same
        // columns as the parent, computed over that group's rows in that
        // period only. Periods come from the rows themselves, so a period with
        // no vehicles in this group simply does not appear.
        function periodSubRows(groupRows) {
            const byPeriod = new Map();
            groupRows.forEach(r => {
                const p = periodOf(r, lensApplied, granularityApplied) || '(blank)';
                if (!byPeriod.has(p)) byPeriod.set(p, []);
                byPeriod.get(p).push(r);
            });
            const periods = [...byPeriod.keys()].sort((a, b) =>
                granularityApplied === 'week' && /^\d{4}-W\d{2}$/.test(a) && /^\d{4}-W\d{2}$/.test(b)
                    ? isoWeekMonday(a) - isoWeekMonday(b)
                    : String(a).localeCompare(String(b)));
            // Percentages are share-of-this-group, so each sub-row is measured
            // against the group's own total rather than the table's — otherwise
            // a group's sub-rows would not add up to 100%.
            const groupTotal = groupRows.length;
            return periods.map(p => ({
                label: p,
                ...summariseGroup(byPeriod.get(p), groupTotal),
            }));
        }

        function buildSummaryTable(rows, groupKey, heading, order, dimKey) {
            const groups = new Map();
            rows.forEach(r => {
                const v = normVal(r[groupKey]) || '(blank)';
                if (!groups.has(v)) groups.set(v, []);
                groups.get(v).push(r);
            });
            let labels = [...groups.keys()];
            labels = order
                ? labels.sort((a, b) => {
                    const ia = order.indexOf(a), ib = order.indexOf(b);
                    if (ia === -1 && ib === -1) return String(a).localeCompare(String(b));
                    if (ia === -1) return 1; if (ib === -1) return -1;
                    return ia - ib;
                })
                : labels.sort((a, b) => String(a).localeCompare(String(b), 'en', { numeric: true }));

            const total = rows.length;
            const dataRows = labels.map(label => ({ label, ...summariseGroup(groups.get(label), total) }));
            const totalRow = { label: 'Grand Total', ...summariseGroup(rows, total) };
            // `groups` is carried through so an expanded row can split its own
            // vehicles by period without re-filtering the whole dataset.
            return { heading, rows: dataRows, totalRow, groupKey, dimKey: dimKey || groupKey, groups };
        }

        // Per-column shading, scaled to that column's own min..max across the
        // rows being drawn. The scale itself (colours, interpolation) lives in
        // shading.js so every page shades identically; these two wrap it for
        // this page's "build a shader per column key, emit an HTML attribute"
        // rendering style.
        function columnShader(dataRows, key) {
            if (dataRows.length < (shadeOpts.min_rows || 2)) return null;
            return shadeScale(dataRows, r => r[key], {
                inScale: SHADE_IN_SCALE[key], spreadSds: shadeOpts.strength, colour: shadeOpts.colourFns[key],
            });
        }
        // Returns a ready-to-inline style attribute (or '' when not shadeable).
        function shadeAttr(shader, value, direction) {
            if (!shader) return '';
            const colour = shader(value, direction === 'red' ? 'high-bad' : 'high-good');
            return colour ? ` style="background: ${colour}"` : '';
        }

        function fmtSummaryCell(col, v) {
            if (v === null || v === undefined) return '';
            if (col.key === 'units') return Math.round(v).toLocaleString('en-GB');
            if (col.key === 'ack_dis' || col.key === 'dts') return Number(v).toFixed(1);
            if (col.key === 'at_rating') return Number(v).toFixed(0);
            if (col.money) return fmtMoney(v);
            return fmtPct(v);   // units_pct, fin_pen_pct
        }

        // Fixed column widths shared by every summary table, so they all line
        // up vertically regardless of which dimension is shown. The label
        // column width is measured (not guessed) from the actual longest
        // heading/label text that will appear across all seven tables — see
        // measureSummaryLabelWidth(). The table renders at its own natural
        // best fit — nothing stretches it, and nothing is sized from it
        // either: the filter panels above are a fixed width independent of
        // this table and of which view is showing.
        const SUMMARY_DATA_COL_WIDTH_BASE = 66;
        // The View column sits immediately after Units. It holds a fixed-size
        // button, so it gets its own fixed width rather than the shared data
        // column width — and it's excluded from every aggregation/shading path.
        const SUMMARY_VIEW_COL_WIDTH = 52;
        const VIEW_COL_AFTER = 'units';   // View column is rendered after this column
        const SUMMARY_LABEL_PADDING = 20;   // matches the cell's left+right padding
        // Both must match the CSS in orders_deliveries.css — the caret's own
        // width plus its gap (.expand-caret), and the sub-row label's indent
        // (.sub-label padding-left minus .dim-label's own).
        const SUMMARY_CARET_WIDTH = 14;
        const SUMMARY_SUB_INDENT = 16;
        let summaryLabelColWidth = 95;        // recomputed before each render
        let summaryDataColWidth = SUMMARY_DATA_COL_WIDTH_BASE;

        // Filter panel width — matches the .slicer CSS fallback and
        // #control-bar's pre-JS fallback width. This is the width the panels
        // always render at, in both views; the control bar is measured to
        // match them rather than the other way round.
        const SLICER_WIDTH_BASE = 163;

        // Measure the pixel width of the longest label-column text (every
        // table heading + every row/Grand Total label) using the real font,
        // so the column is exactly as wide as it needs to be — no guessing.
        function measureSummaryLabelWidth(tables) {
            const canvas = measureSummaryLabelWidth._canvas || (measureSummaryLabelWidth._canvas = document.createElement('canvas'));
            const ctx = canvas.getContext('2d');
            const size = getComputedStyle(document.documentElement).getPropertyValue('--page-font').trim() || '11px';
            ctx.font = `600 ${size} 'DM Sans', system-ui, sans-serif`;   // bold, matches .dim-label/.total-row weight
            let max = 0;
            tables.forEach(t => {
                const displayLabel = l => t.groupKey === '__period' ? periodDisplayLabel(l) : l;
                max = Math.max(max, ctx.measureText(t.heading).width);
                t.rows.forEach(r => { max = Math.max(max, ctx.measureText(displayLabel(r.label)).width); });
                max = Math.max(max, ctx.measureText(t.totalRow.label).width);
                // An expandable table's rows carry a caret, and its sub-rows a
                // period label at an indent. Both have to fit or the column
                // clips them — measured here rather than guessed, since the
                // period labels ("W30 20-Jul") are wider than most group names.
                const dim = SUMMARY_DIMENSIONS.find(d => d.key === t.dimKey);
                if (dim && dim.expandable) {
                    t.rows.forEach(r => {
                        max = Math.max(max, ctx.measureText(displayLabel(r.label)).width + SUMMARY_CARET_WIDTH);
                    });
                    // Every period that could be shown, not just the open ones —
                    // otherwise the column width jumps on each expand/collapse.
                    const periods = new Set();
                    (t.groups ? [...t.groups.values()] : []).forEach(rows => rows.forEach(r => {
                        periods.add(periodOf(r, lensApplied, granularityApplied) || '(blank)');
                    }));
                    periods.forEach(p => {
                        max = Math.max(max, ctx.measureText(periodDisplayLabel(p)).width + SUMMARY_SUB_INDENT);
                    });
                }
            });
            return Math.ceil(max) + SUMMARY_LABEL_PADDING;
        }

        function summaryColgroupHtml() {
            const cols = [`<col style="width:${summaryLabelColWidth}px">`];
            SUMMARY_COLS.forEach(c => {
                cols.push(`<col style="width:${summaryDataColWidth}px">`);
                if (c.key === VIEW_COL_AFTER) cols.push(`<col style="width:${SUMMARY_VIEW_COL_WIDTH}px">`);
            });
            return `<colgroup>${cols.join('')}</colgroup>`;
        }

        // Small "i" marker after a column heading. The explanation is rendered
        // from data-info by a CSS tooltip (see theme.css) rather than the
        // browser's `title`, which cannot be styled and waits ~1s before
        // showing. Deliberately NOT title — setting both shows two tooltips.
        function infoMarkHtml(col) {
            if (!col.info) return '';
            return ` <span class="info-mark" data-info="${escapeHtml(col.info)}">i</span>`;
        }

        function renderSummaryTableHtml(table) {
            const shaders = {};
            // Sales Type only ever has 2-3 rows (Rate/Cash, 0%, Unknown) — a
            // min->max shade over that few rows just marks "biggest of three",
            // not a meaningful spread, so it is left unshaded.
            if (table.dimKey !== '__sales_type_bucket' || shadeOpts.shade_sales_type) {
                SHADE_COLS.forEach(k => { shaders[k] = columnShader(table.rows, k); });
            }

            // Clicking the heading cell collapses the table to this one row.
            const isCollapsed = collapsedTables.has(table.dimKey);
            const head = `<tr><th>${escapeHtml(table.heading)}</th>` +
                SUMMARY_COLS.map(c => {
                    // title only when there is no info marker — otherwise the
                    // browser tooltip and the marker's own would both appear.
                    const t = c.info ? '' : ` title="${escapeHtml(c.label)}"`;
                    const th = `<th class="num"${t}>${escapeHtml(c.label)}${infoMarkHtml(c)}</th>`;
                    return c.key === VIEW_COL_AFTER ? th + '<th>View</th>' : th;
                }).join('') + '</tr>';

            // The button carries the group's identity (which table, which row
            // label) so the popup can rebuild exactly the rows that produced
            // the Units figure next to it.
            const viewCellHtml = label =>
                `<td class="view-cell"><button type="button" class="btn-view" data-dim="${escapeHtml(table.dimKey)}" data-label="${escapeHtml(label)}">View</button></td>`;

            // Period rows show a friendly label (e.g. "W30 20-Jul"); the raw
            // key (r.label) is only used internally for grouping/sorting.
            const displayLabel = l => table.groupKey === '__period' ? periodDisplayLabel(l) : l;

            const dim = SUMMARY_DIMENSIONS.find(d => d.key === table.dimKey);
            const expandable = !!(dim && dim.expandable);

            // Sub-rows are shaded against THEIR OWN group's spread across the
            // periods shown, not against the table's scale. The question a
            // sub-row answers is "which of this group's weeks was good or bad",
            // and the group may sit entirely above or below the table's range —
            // shading it on the table's scale would paint every week the same
            // colour and show no movement at all. Built per expanded group.
            const subShaders = subRows => {
                const s = {};
                if (shadeOpts.shade_sub_rows) SHADE_COLS.forEach(k => { s[k] = columnShader(subRows, k); });
                return s;
            };

            const subRowHtml = (sub, parentLabel, shade) => {
                const cells = SUMMARY_COLS.map(c => {
                    const v = sub[c.key];
                    const negCls = (typeof v === 'number' && v < 0) ? ' neg' : '';
                    const td = `<td class="num${negCls}"${shadeAttr(shade[c.key], v, SHADE_DIRECTION[c.key])}>${escapeHtml(fmtSummaryCell(c, v))}</td>`;
                    // The View button on a sub-row opens that group AND that
                    // period — both identities are needed to rebuild the rows.
                    return c.key === VIEW_COL_AFTER
                        ? td + `<td class="view-cell"><button type="button" class="btn-view" data-dim="${escapeHtml(table.dimKey)}" data-label="${escapeHtml(parentLabel)}" data-period="${escapeHtml(sub.label)}">View</button></td>`
                        : td;
                }).join('');
                return `<tr class="sub-row"><td class="dim-label sub-label">${escapeHtml(periodDisplayLabel(sub.label))}</td>${cells}</tr>`;
            };

            const rowHtml = r => {
                const cells = SUMMARY_COLS.map(c => {
                    const v = r[c.key];
                    const negCls = (typeof v === 'number' && v < 0) ? ' neg' : '';
                    const td = `<td class="num${negCls}"${shadeAttr(shaders[c.key], v, SHADE_DIRECTION[c.key])}>${escapeHtml(fmtSummaryCell(c, v))}</td>`;
                    return c.key === VIEW_COL_AFTER ? td + viewCellHtml(r.label) : td;
                }).join('');

                if (!expandable) {
                    return `<tr><td class="dim-label">${escapeHtml(displayLabel(r.label))}</td>${cells}</tr>`;
                }

                const isOpen = expandedSummaryRows.has(expandKey(table.dimKey, r.label));
                const label = `<td class="dim-label expandable" data-dim="${escapeHtml(table.dimKey)}" data-label="${escapeHtml(r.label)}">`
                    + `<span class="expand-caret">${isOpen ? '−' : '+'}</span>${escapeHtml(displayLabel(r.label))}</td>`;
                const parent = `<tr class="${isOpen ? 'is-expanded' : ''}">${label}${cells}</tr>`;
                if (!isOpen) return parent;
                const groupRows = (table.groups && table.groups.get(r.label)) || [];
                const subs = periodSubRows(groupRows);
                const shade = subShaders(subs);
                return parent + subs.map(s => subRowHtml(s, r.label, shade)).join('');
            };

            const totalCells = SUMMARY_COLS.map(c => {
                const v = table.totalRow[c.key];
                const negCls = (typeof v === 'number' && v < 0) ? ' neg' : '';
                const td = `<td class="num${negCls}">${escapeHtml(fmtSummaryCell(c, v))}</td>`;
                return c.key === VIEW_COL_AFTER ? td + viewCellHtml(GRAND_TOTAL_LABEL) : td;
            }).join('');
            const totalHtml = `<tr class="total-row"><td class="dim-label">${escapeHtml(table.totalRow.label)}</td>${totalCells}</tr>`;

            // The button sits in the panel's left padding, outside the table,
            // level with the header row (its height is set after layout).
            return `<div class="summary-table-wrap"><button type="button" class="collapse-btn" data-dim="${escapeHtml(table.dimKey)}" title="${isCollapsed ? 'Show' : 'Hide'} this table">${isCollapsed ? '+' : '−'}</button><table class="summary${isCollapsed ? ' is-collapsed' : ''}">
                ${summaryColgroupHtml()}
                <thead>${head}</thead>
                ${isCollapsed ? '' : `<tbody>${table.rows.map(rowHtml).join('')}${totalHtml}</tbody>`}
            </table></div>`;
        }

        // Dimensions and their column key + explicit ordering (undefined = alphabetical)
        // Period first — the week/month view is the entry point for reading the
        // report ("how did we do, and what changed"), so it leads rather than
        // sitting below Sales Type.
        // Every table except the period one is expandable: a row opens to show
        // that group split by period, so week-on-week movement is readable
        // without touching a filter. The period table is excluded because
        // splitting a week by week says nothing.
        const SUMMARY_DIMENSIONS = [
            { key: '__period', heading: null /* set per-lens */ },
            { key: '__sales_type_bucket', heading: 'Sales Type', order: ['Rate/Cash', '0%', 'Unknown'], skipOwnFilter: true, expandable: true },
            { key: 'supplier_channel', heading: 'Channel', expandable: true },
            { key: 'dis_band', heading: 'Days in Stock Band', order: SLICER_ORDER.dis_band, expandable: true },
            { key: 'fuel', heading: 'Fuel Type', order: SLICER_ORDER.fuel, expandable: true },
            { key: 'price_band', heading: 'Price Band (Rate/Cash)', expandable: true },
            { key: 'mileage_band', heading: 'Mileage Band', expandable: true },
            { key: 'age_band', heading: 'Age Band', expandable: true },
        ];

        // Price, mileage and age bands are set in Settings > Orders & Deliveries
        // Bands, so their label order comes with the data, not from this file.
        function applyBandOrders(orders) {
            if (!orders) return;
            Object.keys(orders).forEach(key => {
                SLICER_ORDER[key] = orders[key];
                const dim = SUMMARY_DIMENSIONS.find(d => d.key === key);
                if (dim) dim.order = orders[key];
            });
        }

        function renderSummary() {
            const area = document.getElementById('summary-area');
            const base = baseRows(lensApplied);                                                            // lens only, no slicers
            const filtered = base.filter(r => passesSlicers(r, slicerSel, lensApplied, granularityApplied)); // + all live slicers
            const filteredExceptSalesType = base.filter(r => passesSlicers(r, slicerSel, lensApplied, granularityApplied, 'sales_type'));

            // Pass 1: build every table's data (no HTML yet).
            const tables = SUMMARY_DIMENSIONS.map(dim => {
                let rows, groupKey, heading, order;
                if (dim.skipOwnFilter) {
                    // Groups straight off the column — no per-row object copy.
                    rows = filteredExceptSalesType;
                    groupKey = 'sales_type_bucket'; heading = dim.heading; order = dim.order;
                } else if (dim.key === '__period') {
                    rows = filtered.map(r => ({ ...r, __period: periodOf(r, lensApplied, granularityApplied) || '(blank)' }));
                    groupKey = '__period';
                    // Follows lens + granularity: "Order Week" / "Delivery Week"
                    // / "Order Month" / "Delivery Month" (same as the slicer).
                    heading = slicerHeadingApplied('period');
                    order = null; // yyyy-MM / yyyy-Www both sort chronologically as strings
                } else {
                    rows = filtered; groupKey = dim.key; heading = dim.heading; order = dim.order;
                }
                return buildSummaryTable(rows, groupKey, heading, order, dim.key);
            });

            // Measure the true longest label across every table's heading and
            // row labels (including Grand Total), so all seven tables share
            // one exact-fit label column width — no guessing, no truncation.
            // The label column itself is left at this natural width — it's
            // sized to fit specific text ("Rate/Cash", "Grand Total"), not
            // stretched like the data columns.
            summaryLabelColWidth = tables.length ? measureSummaryLabelWidth(tables) : summaryLabelColWidth;
            summaryDataColWidth = SUMMARY_DATA_COL_WIDTH_BASE;

            const emptyHtml = '<div class="empty-state">No rows match the current view.</div>';
            area.classList.remove('is-loading');   // let the fit-content width rule take over
            area.innerHTML = tables.map(renderSummaryTableHtml).join('') || emptyHtml;

            area.querySelectorAll('button.collapse-btn').forEach(btn => {
                // Same height as the header row it sits beside.
                const th = btn.parentElement.querySelector('thead th');
                if (th) btn.style.height = th.getBoundingClientRect().height + 'px';
                btn.addEventListener('click', () => {
                    const k = btn.dataset.dim;
                    if (collapsedTables.has(k)) collapsedTables.delete(k);
                    else collapsedTables.add(k);
                    saveCollapsedTables();
                    renderSummary();
                });
            });

            area.querySelectorAll('button.btn-view').forEach(btn => {
                btn.addEventListener('click', () =>
                    openVehicleModal(btn.dataset.dim, btn.dataset.label, btn.dataset.period || null));
            });

            // Expand/collapse. Only the row's own label cell toggles — clicking
            // a figure or the View button must not move the table under the
            // pointer. Re-renders in place; expandedSummaryRows survives it.
            area.querySelectorAll('td.dim-label.expandable').forEach(cell => {
                cell.addEventListener('click', () => {
                    const k = expandKey(cell.dataset.dim, cell.dataset.label);
                    if (expandedSummaryRows.has(k)) expandedSummaryRows.delete(k);
                    else expandedSummaryRows.add(k);
                    renderSummary();
                });
            });

            // The summary card sizes itself to its own content (fit-content).
            // The filter panels above are deliberately NOT tied to it — the
            // top section is a fixed width in both views, set once in
            // renderSlicers().
        }

        // ── Vehicle list popup ──────────────────────────────────────────────
        // Rebuilds the exact set of rows behind one summary cell's Units
        // figure, using the same filtering the table itself used, so the
        // popup's row count always equals the Units number next to the button.
        // `period` is set when the click came from an expanded sub-row, which
        // narrows the group to that one week/month as well.
        function rowsForSummaryGroup(dimKey, label, period) {
            const dim = SUMMARY_DIMENSIONS.find(d => d.key === dimKey);
            if (!dim) return [];
            const base = baseRows(lensApplied);
            // Sales Type's table ignores the Sales Type slicer (skipOwnFilter),
            // exactly as renderSummary() builds it.
            let rows = base.filter(r => passesSlicers(
                r, slicerSel, lensApplied, granularityApplied,
                dim.skipOwnFilter ? 'sales_type' : undefined));
            if (period) {
                rows = rows.filter(r =>
                    (periodOf(r, lensApplied, granularityApplied) || '(blank)') === period);
            }
            if (label === GRAND_TOTAL_LABEL) return rows;

            const valueOf = r => {
                if (dim.key === '__sales_type_bucket') return normVal(r.sales_type_bucket) || '(blank)';
                if (dim.key === '__period') return periodOf(r, lensApplied, granularityApplied) || '(blank)';
                return normVal(r[dim.key]) || '(blank)';
            };
            return rows.filter(r => valueOf(r) === label);
        }

        function closeVehicleModal() {
            document.getElementById('vehicle-modal-backdrop')?.remove();
            document.getElementById('vehicle-modal')?.remove();
            document.removeEventListener('keydown', vehicleModalKeydown);
        }
        function vehicleModalKeydown(e) { if (e.key === 'Escape') closeVehicleModal(); }

        // Sort state for the popup, independent of the vehicle grid's own.
        // Reset on each open so a popup always starts in Make Model order.
        let modalRows = [], modalSortKey = 'description', modalSortDir = 1;
        let modalLabel = '';   // the open popup's group label, for its CSV filename
        let modalVrmQuery = '';  // VRM search box text; '' shows every row

        // Rows the popup is actually showing. VRMs are stored without spaces but
        // get typed with them ("WT18 XUM"), so both sides have spaces stripped
        // before matching. Substring, not prefix, so a partial plate found on a
        // key or a screenshot still matches.
        function filteredModalRows() {
            const q = modalVrmQuery.replace(/\s+/g, '').toUpperCase();
            if (!q) return modalRows;
            return modalRows.filter(r =>
                String(r.vrm || '').replace(/\s+/g, '').toUpperCase().includes(q));
        }

        function sortedModalRows() {
            const col = MODAL_COLS.find(c => c.key === modalSortKey);
            const numeric = col && (col.type === 'num' || col.type === 'money' || col.type === 'pct');
            // Blanks always sort to the bottom regardless of direction, so a
            // descending sort still leads with real values rather than gaps.
            return [...filteredModalRows()].sort((a, b) => {
                let av = cellValue(col, a), bv = cellValue(col, b);
                const aEmpty = av === null || av === undefined || av === '';
                const bEmpty = bv === null || bv === undefined || bv === '';
                if (aEmpty && bEmpty) return 0;
                if (aEmpty) return 1;
                if (bEmpty) return -1;
                if (numeric) return (Number(av) - Number(bv)) * modalSortDir;
                return String(av).localeCompare(String(bv)) * modalSortDir;
            });
        }

        // The popup uses the vehicle grid's own width measurement
        // (measureVehicleColumnWidths, declared further down) — same columns,
        // same font, so the same widths. It used to have its own near-copy with
        // slightly different min/max/padding constants, which is why the two
        // tables never quite looked alike.

        function renderModalTable() {
            const head = MODAL_COLS.map(c => {
                const caret = modalSortKey === c.key ? (modalSortDir === 1 ? '▲' : '▼') : '⇅';
                const cls = (c.type === 'num' || c.type === 'money' || c.type === 'pct') ? ' class="num"' : '';
                // title only when there is no info marker — see the summary
                // table's header for why.
                const t = c.info ? '' : ` title="${escapeHtml(c.label)}"`;
                // .th-label wraps only the text, so the 3-line cap applies to
                // the label without clipping the marker or caret beside it.
                return `<th${cls} data-key="${c.key}"${t}><span class="th-label">${escapeHtml(c.label)}</span>${infoMarkHtml(c)}<span class="sort-caret">${caret}</span></th>`;
            }).join('');

            const rows = sortedModalRows();
            // Count follows the search box: "3 of 57 vehicles" while filtering,
            // the plain total otherwise, so the number never contradicts the
            // rows underneath it.
            const sub = document.querySelector('.vehicle-modal-sub');
            if (sub) {
                const total = modalRows.length;
                const n = rows.length;
                const unit = `vehicle${total === 1 ? '' : 's'}`;
                sub.textContent = (n === total)
                    ? `${total.toLocaleString('en-GB')} ${unit}`
                    : `${n.toLocaleString('en-GB')} of ${total.toLocaleString('en-GB')} ${unit}`;
            }
            const modalWidths = measureVehicleColumnWidths(rows);
            const colgroup = `<colgroup>${modalWidths.map(w => `<col style="width:${w}px">`).join('')}</colgroup>`;
            // Same red/yellow/green scale as the summary tables, computed over
            // this popup's own rows (min->max within THIS group, same as a
            // single summary table's shading — not across the whole page).
            const shaders = {};
            if (shadeOpts.shade_popup) Object.keys(SHADE_DIRECTION).forEach(k => {
                if (MODAL_COLS.some(c => c.key === k)) shaders[k] = columnShader(rows, k);
            });
            const body = rows.length
                ? rows.map(r => '<tr>' + MODAL_COLS.map(c => {
                    const v = cellValue(c, r);
                    // Link columns (Apex Vehicle/Order/Invoice) render as an icon, same as
                    // the vehicle grid — otherwise the raw URL prints as text.
                    if (c.type === 'link') {
                        return v ? `<td><a href="${escapeHtml(v)}" target="_blank" rel="noopener"><i class="fas fa-external-link-alt"></i></a></td>` : '<td></td>';
                    }
                    const numCls = (c.type === 'num' || c.type === 'money' || c.type === 'pct') ? ' num' : '';
                    const negCls = isNeg(c, v) ? ' neg' : '';
                    // title so a value trimmed by the fixed column width is
                    // still readable on hover.
                    const text = escapeHtml(fmtCell(c, v));
                    return `<td class="${(numCls + negCls).trim()}" title="${text}"${shadeAttr(shaders[c.key], v, SHADE_DIRECTION[c.key])}>${text}</td>`;
                }).join('') + '</tr>').join('')
                : `<tr><td colspan="${MODAL_COLS.length}" style="text-align:center;color:var(--c-text-muted);padding:20px 0;">${
                    modalVrmQuery ? `No VRM matching &ldquo;${escapeHtml(modalVrmQuery)}&rdquo;` : 'No vehicles'
                  }</td></tr>`;

            const table = document.getElementById('vehicle-modal-table');
            // Pin the table to the colgroup's own total. With table-layout:
            // fixed and width: auto, the browser sizes the table to its content
            // and then redistributes the surplus over the colgroup widths — a
            // nowrap column like Description simply took the space it wanted
            // (558px against the 145px the colgroup asked for), so every
            // reduction to the measured width did nothing. With the table
            // pinned to the sum there is no surplus and the colgroup is
            // honoured exactly.
            table.style.width = modalWidths.reduce((a, b) => a + b, 0) + 'px';
            table.innerHTML = `${colgroup}<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
            table.querySelectorAll('th[data-key]').forEach(th => {
                th.addEventListener('click', () => {
                    const key = th.dataset.key;
                    if (modalSortKey === key) modalSortDir = -modalSortDir;
                    else { modalSortKey = key; modalSortDir = 1; }
                    renderModalTable();
                });
            });
        }

        // Top bar Search VRM (Enter): the popup with just that vehicle's rows,
        // from everything loaded, whatever the filters and lens. An exact
        // match first; failing that, every VRM containing the text.
        function searchVrm() {
            const box = document.getElementById('vrm-search');
            const q = box.value.replace(/\s+/g, '').toUpperCase();
            if (!q) return;
            const norm = r => String(r.vrm || '').replace(/\s+/g, '').toUpperCase();
            let rows = allRows.filter(r => norm(r) === q);
            if (!rows.length) rows = allRows.filter(r => norm(r).includes(q));
            openVehicleModal(null, null, null, { rows, heading: 'VRM', label: box.value.trim().toUpperCase() });
        }

        // override: { rows, heading, label } to show given rows (Search VRM)
        // rather than a summary group's.
        function openVehicleModal(dimKey, label, period, override = null) {
            closeVehicleModal();
            modalRows = override ? override.rows : rowsForSummaryGroup(dimKey, label, period);
            modalSortKey = 'description';
            modalSortDir = 1;
            modalVrmQuery = '';   // each open starts showing the whole group

            const dim = SUMMARY_DIMENSIONS.find(d => d.key === dimKey);
            const heading = override ? override.heading : dim
                ? (dim.key === '__period' ? slicerHeadingApplied('period') : dim.heading)
                : '';
            let shownLabel = override ? override.label : label === GRAND_TOTAL_LABEL
                ? 'Grand Total'
                : (dimKey === '__period' ? periodDisplayLabel(label) : label);
            // From an expanded sub-row: name both the group and the period, so
            // the popup title and the CSV filename say which week this is.
            if (period) shownLabel += ` — ${periodDisplayLabel(period)}`;
            // Remembered for the popup's CSV filename, so a group's export is
            // distinguishable from the full Vehicle view one.
            modalLabel = shownLabel;

            const backdrop = document.createElement('div');
            backdrop.id = 'vehicle-modal-backdrop';
            backdrop.className = 'modal-backdrop-custom';
            backdrop.addEventListener('click', closeVehicleModal);

            const modal = document.createElement('div');
            modal.id = 'vehicle-modal';
            modal.className = 'vehicle-modal';
            modal.innerHTML = `
                <div class="vehicle-modal-head">
                    <div>
                        <div class="vehicle-modal-title">${escapeHtml(heading)}: ${escapeHtml(shownLabel)}</div>
                        <div class="vehicle-modal-sub">${modalRows.length.toLocaleString('en-GB')} vehicle${modalRows.length === 1 ? '' : 's'}</div>
                    </div>
                    <div class="d-flex align-items-center gap-2">
                        <input type="search" class="vehicle-modal-search" id="vehicle-modal-search"
                               placeholder="Search VRM" aria-label="Search VRM" autocomplete="off" spellcheck="false">
                        <!-- Same classes/icon as the Vehicle view's CSV button,
                             and the same columns — see downloadCsvRows(). -->
                        <button type="button" class="btn btn-csv" id="vehicle-modal-csv"><i class="fas fa-download"></i> CSV</button>
                        <button type="button" class="vehicle-modal-close" aria-label="Close">&times;</button>
                    </div>
                </div>
                <div class="vehicle-modal-body">
                    <div class="spinner-wrap" id="vehicle-modal-loading"><div class="css-spinner"></div><div style="margin-top:12px;font-size:0.9rem;">Loading vehicles...</div></div>
                    <table class="vehicle-modal-table" id="vehicle-modal-table"></table>
                </div>`;
            modal.querySelector('.vehicle-modal-close').addEventListener('click', closeVehicleModal);
            modal.querySelector('#vehicle-modal-csv').addEventListener('click', downloadModalCsv);

            const search = modal.querySelector('#vehicle-modal-search');
            // 'input' rather than 'keyup' so the type-to-filter also fires on
            // paste and on the search box's own clear (x) button.
            search.addEventListener('input', () => {
                modalVrmQuery = search.value;
                renderModalTable();
            });
            // Escape inside the box clears the search rather than closing the
            // popup — closing on the first Escape would throw away the group
            // the user drilled into just to undo a search. A second Escape on
            // an empty box falls through to vehicleModalKeydown and closes.
            search.addEventListener('keydown', e => {
                if (e.key === 'Escape' && search.value) {
                    e.stopPropagation();
                    search.value = '';
                    modalVrmQuery = '';
                    renderModalTable();
                }
            });

            document.body.appendChild(backdrop);
            document.body.appendChild(modal);
            document.addEventListener('keydown', vehicleModalKeydown);

            // Building the table for a large group (Grand Total over every
            // month is 6000+ rows) blocks the page for a few seconds, so let
            // the popup and its spinner paint first. requestAnimationFrame
            // runs before the paint; the setTimeout inside it runs after.
            requestAnimationFrame(() => setTimeout(() => {
                // Closed, or replaced by another View click, while waiting
                if (document.getElementById('vehicle-modal') !== modal) return;
                renderModalTable();
                modal.querySelector('#vehicle-modal-loading')?.remove();
            }, 0));
        }

        // ── Slicer rendering & interaction ──────────────────────────────────
        function sortSlicerValues(key, values) {
            const arr = [...values];
            if (key === 'period') return arr.sort().reverse();        // newest first
            const order = SLICER_ORDER[key];
            if (order) {
                // Explicit order; anything unexpected goes to the end, alphabetical
                return arr.sort((a, b) => {
                    const ia = order.indexOf(a), ib = order.indexOf(b);
                    if (ia === -1 && ib === -1) return String(a).localeCompare(String(b));
                    if (ia === -1) return 1;
                    if (ib === -1) return -1;
                    return ia - ib;
                });
            }
            return arr.sort((a, b) => String(a).localeCompare(String(b), 'en', { numeric: true }));
        }

        // Slicer panels reflect APPLIED state — the whole bar (headings,
        // which values exist) is frozen exactly as it was at the last Apply.
        // The ONLY thing that changes on a click, before Apply, is which
        // pills are highlighted "selected" — that's the point of the
        // pending/applied split, and it must not leak into anything else
        // (heading text, which values are shown, counts).
        //
        // Cross-filtered-out values are DROPPED, not shown greyed out — a
        // disabled pill still took up a row and forced scrolling for no
        // benefit, since it can't be clicked anyway.
        // Slicers are live: clicking re-renders slicer panels + tables/grid
        // immediately (rerender()). Cross-filtering, the panel headings, and
        // which values show all reflect the currently-applied lens/granularity
        // (those three only change on Apply).
        function renderSlicers() {
            const base = baseRows(lensApplied);
            const bar = document.getElementById('slicer-bar');
            bar.innerHTML = SLICERS.map(s => {
                // Values available in this panel = distinct values across rows
                // passing every OTHER panel's live selection (cross-filter).
                const eligible = base.filter(r => passesSlicers(r, slicerSel, lensApplied, granularityApplied, s.key));
                const avail = new Set();
                eligible.forEach(r => { const v = normVal(slicerValue(r, s.key, lensApplied, granularityApplied)); if (v) avail.add(v); });
                // Period is REPLACED by periodRange(), not merely topped up by
                // it. The row scan above cannot be trusted for periods: the
                // fetch pulls rows in on EITHER date (see the OR clause in
                // _build_data), so a vehicle ordered before the fetch floor but
                // delivered after it puts its old ORDER week into `avail` —
                // e.g. W13 2026 showing "2 units" from two vehicles ordered
                // 23-Mar and delivered 4-May. Those weeks are real rows but
                // wildly incomplete (W18 held 2 of its 484 real orders), so
                // offering them as if they were normal weeks is misleading.
                // periodRange() is anchored on the server's own fetch floor and
                // is the authoritative list of periods whose data is whole; it
                // also still includes periods with zero rows (e.g. the
                // in-progress current week before any sale lands), since
                // "nothing this period" is real information.
                if (s.key === 'period') {
                    avail.clear();
                    periodRange(granularityApplied).forEach(k => avail.add(k));
                }
                // A currently-selected value must stay visible even if it's not
                // in avail, so the user can still see and deselect it —
                // everything else not in avail is left out entirely.
                const shown = new Set(avail);
                slicerSel[s.key].forEach(v => shown.add(v));

                const sel = slicerSel[s.key];
                const items = sortSlicerValues(s.key, shown).map(v => {
                    const isSel = sel.has(v);
                    const cls = 'slicer-item' + (isSel ? ' selected' : '');
                    // The pill shows a friendly label for period keys (e.g. "W30
                    // 20-Jul"); data-val stays the raw key so filtering is unaffected.
                    const label = s.key === 'period' ? periodDisplayLabel(v) : v;
                    return `<div class="${cls}" data-key="${s.key}" data-val="${escapeHtml(v)}" role="button">${escapeHtml(label)}</div>`;
                }).join('');

                const clearCls = 'slicer-clear' + (sel.size ? '' : ' hidden');
                return `<div class="slicer">
                    <div class="slicer-head"><span>${escapeHtml(slicerHeadingApplied(s.key))}</span>
                        <button class="${clearCls}" data-clear="${s.key}" title="Clear">Clear</button></div>
                    <div class="slicer-list">${items}</div>
                </div>`;
            }).join('');

            bar.querySelectorAll('.slicer-item').forEach(el => {
                el.addEventListener('click', (ev) => {
                    const key = el.dataset.key, val = el.dataset.val;
                    const sel = slicerSel[key];

                    // Shift-click selects everything between the last pill
                    // clicked in this panel and this one, in the order the
                    // panel actually displays them (which for periods is
                    // newest-first, not alphabetical) — so "W30 to W25" picks
                    // up the five weeks in between regardless of direction.
                    // Range selection only ever ADDS: it would be too easy to
                    // wipe a careful selection with one mis-aimed shift-click.
                    const anchor = slicerAnchor[key];
                    if (ev.shiftKey && anchor && anchor !== val) {
                        const order = [...bar.querySelectorAll(`.slicer-item[data-key="${key}"]`)]
                            .map(n => n.dataset.val);
                        const from = order.indexOf(anchor), to = order.indexOf(val);
                        if (from !== -1 && to !== -1) {
                            const [lo, hi] = from < to ? [from, to] : [to, from];
                            for (let i = lo; i <= hi; i++) sel.add(order[i]);
                            // Anchor stays put, so a second shift-click can
                            // extend or redraw the range from the same origin.
                            rerender();
                            return;
                        }
                    }

                    sel.has(val) ? sel.delete(val) : sel.add(val);
                    slicerAnchor[key] = val;
                    rerender();   // live — filters the tables/grid immediately
                });
            });
            bar.querySelectorAll('.slicer-clear').forEach(el => {
                el.addEventListener('click', () => {
                    slicerSel[el.dataset.clear].clear();
                    rerender();
                });
            });

            // Panels are always their base width — the top section is a fixed
            // size independent of the view (see matchFilterWidthToTable).
            document.documentElement.style.setProperty('--slicer-width', SLICER_WIDTH_BASE + 'px');
            // Size the control bar to those panels. Done here rather than in
            // renderSummary() so it happens in BOTH views — the top section
            // must look identical whichever view is applied.
            matchFilterWidthToTable();
        }

        // Sizes the control bar to the filter panels' own rendered right edge.
        //
        // The whole top section (control bar + filter panels) is a FIXED width
        // that does not depend on which view is showing: the panels stay at
        // SLICER_WIDTH_BASE and the control bar is measured to match them.
        // Previously the panels were widened to the summary card's right edge,
        // which made the top section a different width in Summary than in
        // Vehicle view and moved every filter when the view changed.
        function matchFilterWidthToTable() {
            const bar = document.getElementById('slicer-bar');
            const panels = bar.children;
            if (!panels.length) return;

            // Measured off the live DOM, not computed from the CSS: the panels
            // are laid out by flex with gaps, so their real right edge already
            // accounts for the gaps and any sub-pixel rounding.
            const n = panels.length;
            const barLeft = bar.getBoundingClientRect().left;
            const lastRight = panels[n - 1].getBoundingClientRect().right;
            const w = Math.ceil(lastRight - barLeft) + 'px';
            // Both the control bar above the slicers and the Ask AI panel below
            // them line up with the panels' right edge, so the three stack as
            // one column. Same measurement for both — they cannot disagree.
            // The Ask AI panel is only rendered for tdrayson (see the template),
            // so it may not exist here.
            document.getElementById('control-bar').style.width = w;
            const aiPanel = document.getElementById('ai-panel');
            if (aiPanel) aiPanel.style.width = w;
        }

        // Full refresh of everything actually on screen — slicer panels, row
        // count, and whichever view is APPLIED. This is the only function
        // that changes what's visibly displayed; it only runs on load and on
        // Apply (see applyFilters()), never from a bare control click.
        function rerender() {
            renderSlicers();
            document.getElementById('summary-area').style.display = viewApplied === 'summary' ? '' : 'none';
            document.getElementById('vehicle-area').style.display = viewApplied === 'vehicle' ? '' : 'none';
            if (viewApplied === 'vehicle') renderGrid(); else renderSummary();
        }

        // Apply is green by default and greys out (with a spinning icon)
        // only while data is loading (initial load / Refresh), turning back
        // green when done — its colour + spinner is a loading indicator, not
        // an "unsaved changes" cue.
        function setApplyLoading(isLoading) {
            const btn = document.getElementById('apply-btn');
            btn.classList.toggle('loading', isLoading);
            btn.disabled = isLoading;
            document.getElementById('apply-label').innerHTML =
                isLoading ? '<i class="fas fa-spinner fa-spin"></i> Loading' : 'Apply';
        }

        // Commits the three staged toggles (Lens, View, Group By). Slicers
        // are already live and untouched here.
        function applyFilters() {
            // Week keys (yyyy-Www) and month keys (yyyy-MM) aren't
            // interchangeable — if granularity is changing on this Apply, the
            // live period selection (from the old shape) would match nothing,
            // so reset it to the new granularity's default window.
            const granularityChanged = granularityPending !== granularityApplied;

            lensApplied = lensPending;
            viewApplied = viewPending;
            granularityApplied = granularityPending;

            if (granularityChanged && todayIso) {
                slicerSel['period'] = new Set(defaultPeriodSelection(todayIso, granularityApplied));
            }
            rerender();
        }

        // Per-column widths estimated from actual content, not guessed. Since
        // headers wrap onto up to 2 lines, a column only needs to be as wide
        // as its LONGEST SINGLE WORD in the header (the phrase itself can
        // wrap) — so most columns end up sized to their (usually shorter)
        // data content, exactly as intended: many values are shorter than
        // their header, and wrapping the header is what makes that possible.
        const VEHICLE_COL_MIN_WIDTH = 46;
        const VEHICLE_COL_MAX_WIDTH = 220;
        // Description is set outright, not measured or capped — see the
        // description branch in measureVehicleColumnWidths for why.
        const DESCRIPTION_COL_WIDTH = 400;
        const VEHICLE_COL_PADDING = 22;      // matches td/th left+right padding
        const VEHICLE_SAMPLE_ROWS = 400;     // measuring every row is unnecessary and slower

        function measureVehicleColumnWidths(rows) {
            const canvas = measureVehicleColumnWidths._canvas || (measureVehicleColumnWidths._canvas = document.createElement('canvas'));
            const ctx = canvas.getContext('2d');
            const size = getComputedStyle(document.documentElement).getPropertyValue('--vehicle-font').trim() || '11px';
            const sample = rows.length > VEHICLE_SAMPLE_ROWS
                ? rows.filter((_, i) => i % Math.ceil(rows.length / VEHICLE_SAMPLE_ROWS) === 0)
                : rows;

            return COLS.map(c => {
                if (c.type === 'link') return 54;   // icon-only column, fixed

                // Header: only the longest WORD matters, since the phrase wraps.
                ctx.font = `600 ${size} 'DM Sans', system-ui, sans-serif`;
                let headerWordMax = Math.max(...c.label.split(' ').map(w => ctx.measureText(w).width));
                // The info marker and sort caret sit on the header's last line,
                // so the column has to be wide enough to hold them beside a word.
                if (c.info) headerWordMax += ctx.measureText('  i ').width;

                // Data: measure the sample's formatted values at regular weight.
                ctx.font = `400 ${size} 'DM Sans', system-ui, sans-serif`;
                let dataMax = 0;
                sample.forEach(r => {
                    const w = ctx.measureText(fmtCell(c, cellValue(c, r))).width;
                    if (w > dataMax) dataMax = w;
                });

                const px = Math.ceil(Math.max(headerWordMax, dataMax)) + VEHICLE_COL_PADDING;
                const width = Math.min(VEHICLE_COL_MAX_WIDTH, Math.max(VEHICLE_COL_MIN_WIDTH, px));

                // Description gets a fixed width rather than a measured one.
                // Full descriptions run well past any sensible column width, so
                // the measurement only ever hit VEHICLE_COL_MAX_WIDTH — this is
                // the one column where the right width is a judgement about how
                // much of the text to show, not a fit to its content. Values
                // longer than this truncate with an ellipsis (the full text is
                // on the cell's title attribute).
                if (c.key === 'description') return DESCRIPTION_COL_WIDTH;
                return width;
            });
        }

        function vehicleColgroupHtml(widths) {
            return `<colgroup>${widths.map(w => `<col style="width:${w}px">`).join('')}</colgroup>`;
        }

        function renderGrid() {
            const rows = visibleRows();
            const area = document.getElementById('grid-area');
            if (!rows.length) {
                area.innerHTML = '<div class="empty-state">No rows match the current view.</div>';
                return;
            }

            const widths = measureVehicleColumnWidths(rows);

            const head = COLS.map((c, i) => {
                const caret = sortKey === c.key ? (sortDir === 1 ? '▲' : '▼') : '⇅';
                // title only when there is no info marker — same reasoning as
                // the summary/popup headers: a native title tooltip and the
                // info-mark's own tooltip would both fire on hover otherwise.
                const t = c.info ? '' : ` title="${escapeHtml(c.label)}"`;
                return `<th data-key="${c.key}"${t}>${escapeHtml(c.label)}${infoMarkHtml(c)}<span class="sort-caret">${caret}</span></th>`;
            }).join('');

            const body = rows.map(r => '<tr>' + COLS.map(c => {
                const v = cellValue(c, r);
                if (c.type === 'link') {
                    return v ? `<td><a href="${escapeHtml(v)}" target="_blank" rel="noopener"><i class="fas fa-external-link-alt"></i></a></td>` : '<td></td>';
                }
                const numCls = (c.type === 'num' || c.type === 'money' || c.type === 'pct') ? ' num' : '';
                const negCls = isNeg(c, v) ? ' neg' : '';
                const text = escapeHtml(fmtCell(c, v));
                return `<td class="${(numCls + negCls).trim()}" title="${text}">${text}</td>`;
            }).join('') + '</tr>').join('');

            // Explicit width = the colgroup's own total, overriding the CSS
            // width: 100%. Left at 100% the table stretches to the container
            // and table-layout: fixed spreads the surplus across the columns,
            // so the measured widths (Description's reduction especially) are
            // not what actually renders. See renderModalTable for the same fix.
            const gridWidth = widths.reduce((a, b) => a + b, 0);
            area.innerHTML = `<div class="grid-wrap"><table class="grid" style="width:${gridWidth}px">${vehicleColgroupHtml(widths)}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;

            area.querySelectorAll('th[data-key]').forEach(th => {
                th.addEventListener('click', () => {
                    const key = th.getAttribute('data-key');
                    if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
                    renderGrid();
                });
            });
        }

        // Every one of these three is now staged, same as the slicers: only
        // the button's own active-class and the panel preview update; nothing
        // actually displayed changes until Apply (applyFilters -> rerender()).

        // These three only record the staged choice and toggle the clicked
        // button's own active state. NOTHING else happens — no re-render, no
        // change to the slicer panels, the tables, the grid, or any selection
        // — until Apply is clicked.
        function setLens(newLens) {
            lensPending = newLens;
            document.querySelectorAll('#lens-toggle button').forEach(b =>
                b.classList.toggle('active', b.dataset.lens === newLens));
        }

        function setGranularity(newGranularity) {
            granularityPending = newGranularity;
            document.querySelectorAll('#granularity-toggle button').forEach(b =>
                b.classList.toggle('active', b.dataset.granularity === newGranularity));
        }

        function setView(newView) {
            viewPending = newView;
            document.querySelectorAll('#view-toggle button').forEach(b =>
                b.classList.toggle('active', b.dataset.view === newView));
        }

        function csvEscape(v) {
            if (v === null || v === undefined) return '';
            const s = String(v);
            return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
        }

        // Shared by the Vehicle view's CSV button and the popup's — same
        // columns (COLS === MODAL_COLS), same row/column shape, just a
        // different row source and filename suffix.
        function downloadCsvRows(rows, filenameSuffix) {
            const header = COLS.map(c => csvEscape(c.label)).join(',');
            const lines = rows.map(r => COLS.map(c => {
                // Raw values in the CSV (not the formatted display) so numbers stay numeric
                return csvEscape(cellValue(c, r));
            }).join(','));
            const csv = [header, ...lines].join('\r\n');
            const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `orders_deliveries_${filenameSuffix}_${new Date().toISOString().slice(0,10)}.csv`;
            a.click();
            URL.revokeObjectURL(url);
        }
        function downloadCsv() {
            downloadCsvRows(visibleRows(), lensApplied);
        }
        function downloadModalCsv() {
            // Group labels contain spaces and punctuation ("C2B2C - PX",
            // "W34 17-Aug"), so reduce to a filename-safe slug.
            const slug = String(modalLabel).replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
            downloadCsvRows(sortedModalRows(), slug ? `${lensApplied}_${slug}` : lensApplied);
        }

        // Default period selection:
        //   Week  — the current (in-progress) week plus the 6 complete weeks
        //           before it, 7 in total. The full fetched window is ~4
        //           months of weeks, which is more than anyone wants on first
        //           load; the panel still offers every one of them.
        //   Month — every month in range.
        // Taken from the front of periodRange() (newest first), so the default
        // can never include a period the panel does not offer. Recomputed
        // whenever granularity changes, since week keys and month keys aren't
        // the same shape — a stale selection from the other granularity would
        // just silently match nothing.
        const DEFAULT_WEEKS_SELECTED = 7;   // current + 6 complete
        // Day: today plus the 7 days before it. Unlike weeks and months, the
        // day range offers more days than this (the whole fetch window), so
        // the default is a slice of it rather than all of it.
        const DEFAULT_DAYS_SELECTED = 8;    // today + 7 previous

        function defaultPeriodSelection(today, granularityVal) {
            const range = periodRange(granularityVal);
            if (granularityVal === 'day')  return range.slice(0, DEFAULT_DAYS_SELECTED);
            if (granularityVal === 'week') return range.slice(0, DEFAULT_WEEKS_SELECTED);
            return range;
        }

        // Period options are anchored on the server's own fetch floor
        // (fetchStartIso — the 1st of the month N months back; see
        // _fetch_start() in orders_deliveries.py), never inferred from
        // allRows. Row data is NOT a reliable guide to which periods are
        // complete: a row can appear earlier than the real fetch floor via
        // the OTHER lens's date (e.g. ordered 4 months ago but delivered
        // last week, pulled in by the Deliveries-date OR clause in the SQL),
        // so scanning for "the earliest row" can find a single stray row
        // sitting in an otherwise near-empty period and wrongly offer it as
        // available. (This bit us for real: W18 2026 had 484 orders but only
        // 1 row made it into the fetch window — the old earliest-row logic
        // still offered "W18" as if it had data.)
        //
        // Months: a fixed count (current + PERIOD_MONTHS_SHOWN complete
        // months), matching _FETCH_MONTHS_BACK on the server exactly — the
        // fetch floor is the 1st of that oldest month, so every month shown
        // is complete by construction. No per-month check needed.
        //
        // Weeks: NOT fixed — the true number of complete weeks between the
        // fetch floor and today, computed here rather than guessed. The
        // fetch window is sized in calendar months, and a calendar month is
        // not a whole number of weeks, so this count varies run to run.
        const PERIOD_MONTHS_SHOWN = 3;   // keep in step with _FETCH_MONTHS_BACK

        function periodRange(granularityVal) {
            if (!todayIso || !fetchStartIso) return [];
            const keys = [];
            if (granularityVal === 'day') {
                // Every day from today back to the fetch floor, newest first.
                // No completeness question to answer here, unlike weeks: a day
                // is either inside the fetched window or it is not, and today's
                // own figures are in progress in exactly the way the current
                // week's already are.
                const floor = new Date(fetchStartIso + 'T00:00:00');
                let d = new Date(todayIso + 'T00:00:00');
                while (d >= floor) {
                    keys.push(ymdLocal(d));
                    d.setDate(d.getDate() - 1);
                }
            } else if (granularityVal === 'week') {
                let monday = isoWeekMonday(isoWeekKey(todayIso));
                // The FIRST MONDAY ON OR AFTER the fetch floor, not the Monday
                // of the floor's own week: fetchStartIso is the 1st of a
                // calendar month, which is rarely itself a Monday. Rounding it
                // to that week's Monday (isoWeekMonday(isoWeekKey(...))) would
                // land BEFORE the real floor, offering a week whose early days
                // the SQL WHERE clause actually excluded — the same
                // incomplete-period bug this whole rewrite exists to fix, via
                // a different path.
                let floorMonday = isoWeekMonday(isoWeekKey(fetchStartIso));
                if (floorMonday < new Date(fetchStartIso + 'T00:00:00')) {
                    floorMonday.setDate(floorMonday.getDate() + 7);
                }
                while (monday >= floorMonday) {
                    keys.push(isoWeekKey(ymdLocal(monday)));
                    monday.setDate(monday.getDate() - 7);
                }
            } else {
                const now = new Date(todayIso + 'T00:00:00');
                let y = now.getFullYear(), m = now.getMonth();
                for (let i = 0; i <= PERIOD_MONTHS_SHOWN; i++) {
                    keys.push(`${y}-${String(m + 1).padStart(2, '0')}`);
                    m--; if (m < 0) { m = 11; y--; }
                }
            }
            return keys;
        }

        let todayIso = null;          // remembered so a granularity toggle can recompute the default
        // The server's own fetch floor — the 1st of the month
        // _FETCH_MONTHS_BACK months before the current one; see
        // _fetch_start() in orders_deliveries.py. NOT the same as "the
        // earliest date seen in allRows": a row can appear before this floor
        // via the OTHER lens's date (e.g. ordered before the floor but
        // delivered after it, included via the Deliveries-date OR clause) —
        // that row does not mean its order period is complete. Only this real
        // server floor tells us whether a week or month's data is actually
        // whole.
        let fetchStartIso = null;
        let periodDefaulted = false;
        function defaultPeriodFilter(today) {
            // A page left open past midnight: the new day can start a new
            // period, so the selection is reset to the default for today
            // rather than staying on yesterday's periods.
            if (periodDefaulted && todayIso && today && today !== todayIso) periodDefaulted = false;
            todayIso = today;
            // Only set once per page load — a manual refresh must not stomp a
            // user's own period selection. Switching granularity resets it
            // deliberately (see setGranularity), since old week/month keys
            // don't carry over to the other granularity.
            if (periodDefaulted) {
                // Drop any selected period that is no longer in range. The
                // panel re-adds a selected value even when it is not offered
                // (so it can still be deselected), so without this a period
                // selected before a refresh — or one left over from an older
                // fetch window — would stay selected and keep filtering, while
                // showing the incomplete data that made it unofferable.
                const inRange = new Set(periodRange(granularityApplied));
                slicerSel['period'].forEach(k => { if (!inRange.has(k)) slicerSel['period'].delete(k); });
                return;
            }
            slicerSel['period'] = new Set(defaultPeriodSelection(today, granularityApplied));
            periodDefaulted = true;
        }

        function showLoading(msg) {
            const html = `<div class="spinner-wrap"><div class="css-spinner"></div><div style="margin-top:12px;font-size:0.9rem;">${escapeHtml(msg || 'Loading...')}</div></div>`;
            document.getElementById('grid-area').innerHTML = html;
            const summaryArea = document.getElementById('summary-area');
            summaryArea.classList.add('is-loading');   // full width to match the control bar, like the initial load state
            summaryArea.innerHTML = html;

            // The panels are a fixed width and the control bar is measured
            // from them, so both are already correct and must NOT be reset
            // here — clearing the control bar's width would drop it back to
            // the 1410px CSS fallback for the length of the spinner, then
            // snap it back once data renders. Only #summary-area changes
            // width during loading (the is-loading class above).
            document.documentElement.style.setProperty('--slicer-width', SLICER_WIDTH_BASE + 'px');
        }

        // ── Ask AI ──────────────────────────────────────────────────────────
        // The model never sees vehicle rows. This builds a table of per-slice
        // means per period, plus the change and the impact (change x volume)
        // against the previous period, and sends that. Impact is the column
        // that answers "what caused the drop" — it ranks slices by how much of
        // the total movement each one actually accounts for, so a big per-unit
        // swing on a handful of vehicles cannot outrank a small one on many.
        // `rate` metrics are a percentage over the whole slice (finance
        // penetration), not a mean of per-vehicle values — see aiRenderBreakdown.
        // `context: true` marks a metric that describes WHAT WAS SOLD rather
        // than how well it was sold. Purchase and sell price move whenever the
        // mix of vehicles changes, so they never explain a margin movement on
        // their own; they are sent so the model can recognise a mix shift, and
        // the prompt forbids presenting them as a cause.
        const AI_METRICS = [
            { key: 'gpu_m',                label: 'GPU-M' },
            { key: 'gpu_s',                label: 'GPU-S' },
            { key: 'gpu_ms',               label: 'GPU-MS' },
            { key: 'gpu_m_costs',          label: 'GPU-M Costs' },
            { key: 'gpu_s_costs',          label: 'GPU-S Costs' },
            { key: 'chassis_mgn_aft_subs', label: 'Chassis Margin After Subsidy' },
            { key: 'metal_mgn',            label: 'Metal Margin' },
            { key: 'vehicle_costs',        label: 'Vehicle Costs' },
            // The three finance columns the summary tables already compute
            // (summariseGroup) — reused here rather than recalculated, so the
            // model sees the same numbers the page shows. Together they
            // separate "we financed a smaller share of deals" (penetration)
            // from "we earned less on each financed deal" (comm per financed).
            { key: 'fin_pen_pct',          label: 'Finance Penetration %', group: 'fin_pen_pct', pct: true },
            { key: 'fin_comm_fin',         label: 'Finance Commission per Financed Deal', group: 'fin_comm_fin' },
            { key: 'fin_comm_all',         label: 'Finance Commission per Unit (all deals)', group: 'fin_comm_all' },
            { key: 'product_mgn',          label: 'Total Product Margin' },
            { key: 'finance_subsidy',      label: 'Finance Subsidy' },
            { key: 'write_down',           label: 'Write Down' },
            { key: 'discount',             label: 'Discount' },
            { key: 'purch_price',          label: 'Purchase Price', context: true },
            { key: 'sell_price_incl_fee',  label: 'Sell Price (incl fee)', context: true },
        ];
        // Dimensions the model is allowed to slice by. Period is excluded — it
        // is the comparison axis, not a slice. Fuel, mileage band and age band
        // are excluded entirely — the focus is suppliers and individual
        // vehicles that lost money, not vehicle-spec breakdowns. Supplier is
        // excluded from the PAIRED breakdown only (too many values; it stays
        // as a single).
        const AI_EXCLUDED_DIMS = new Set(['period', 'fuel', 'mileage_band', 'age_band']);
        const AI_DIMS = SLICERS.filter(s => !AI_EXCLUDED_DIMS.has(s.key));
        const AI_PAIR_DIMS = AI_DIMS.filter(s => s.key !== 'supplier');
        // A slice needs this many vehicles in a period before its mean is
        // reported. Below it the mean is noise and the model would narrate it
        // as a finding.
        const AI_MIN_UNITS = 3;

        function aiRound(v) { return v === null || v === undefined ? null : Math.round(v); }

        // Per-slice figures for one grouping across every selected period.
        // groupOf(r) returns the slice label, or null to drop the row.
        function aiBreakdown(rows, periods, groupOf) {
            const byGroup = new Map();
            for (const r of rows) {
                const g = groupOf(r);
                if (g === null || g === undefined || g === '') continue;
                const p = periodOf(r, lensApplied, granularityApplied);
                if (!p) continue;
                if (!byGroup.has(g)) byGroup.set(g, new Map());
                const pm = byGroup.get(g);
                if (!pm.has(p)) pm.set(p, []);
                pm.get(p).push(r);
            }
            const out = [];
            for (const [group, pm] of byGroup) {
                const series = periods.map(p => {
                    const rs = pm.get(p) || [];
                    return { period: p, units: rs.length, rows: rs };
                });
                // Drop a slice that never reaches the volume floor in any period.
                if (!series.some(s => s.units >= AI_MIN_UNITS)) continue;
                out.push({ group, series });
            }
            return out;
        }

        // Most slices in a full cross-tab contribute nothing to a movement —
        // sending them costs tokens and makes the model read past noise to
        // find the answer. Each metric emits only its biggest movers by
        // absolute impact, in both directions (a metric can fall overall while
        // one slice rises sharply, and that contrast is part of the answer).
        const AI_TOP_SLICES = 12;

        // Render one dimension's breakdown as text lines, one metric at a time.
        function aiRenderBreakdown(title, breakdown, periods, metrics) {
            const lines = [];
            for (const m of metrics) {
                const scored = [];
                for (const b of breakdown) {
                    // A `group` metric (finance penetration, commission per
                    // financed deal) is computed over the whole slice by
                    // summariseGroup — averaging it per row would be wrong, and
                    // would also disagree with the figure on the summary table.
                    const cells = b.series.map(s => ({
                        period: s.period,
                        units: s.units,
                        mean: s.units < AI_MIN_UNITS ? null
                            : m.group ? summariseGroup(s.rows, s.rows.length)[m.group]
                            : mean(s.rows.map(r => r[m.key])),
                    }));
                    if (cells.every(c => c.mean === null || c.mean === undefined)) continue;
                    // Penetration is a 0-1 fraction — show it as a percentage so
                    // the model does not read 0.55 as pounds.
                    const show = v => m.pct ? (v * 100).toFixed(1) + '%' : aiRound(v);
                    const parts = cells.map(c => (c.mean === null || c.mean === undefined)
                        ? `${c.period}: n=${c.units} mean=-`
                        : `${c.period}: n=${c.units} mean=${show(c.mean)}`);
                    // Change and impact against the immediately previous period.
                    const last = cells[cells.length - 1], prev = cells[cells.length - 2];
                    let tail = '', impact = 0;
                    if (last && prev && last.mean != null && prev.mean != null) {
                        const chg = last.mean - prev.mean;
                        if (m.pct) {
                            // Percentage points, not pounds — an "impact" in
                            // currency is meaningless for a rate, so rank these
                            // by the size of the move against the slice's volume.
                            tail = ` | change=${(chg * 100).toFixed(1)}pp`;
                            impact = chg * 100 * last.units;
                        } else {
                            impact = chg * last.units;
                            tail = ` | change=${aiRound(chg)} impact=${aiRound(impact)}`;
                        }
                    }
                    scored.push({ impact, line: `  ${b.group} | ${parts.join(' | ')}${tail}` });
                }
                if (!scored.length) continue;
                scored.sort((a, b) => Math.abs(b.impact) - Math.abs(a.impact));
                const kept = scored.slice(0, AI_TOP_SLICES);
                const omitted = scored.length - kept.length;
                lines.push(`${m.label} by ${title}:`, ...kept.map(s => s.line));
                if (omitted > 0) {
                    lines.push(`  (${omitted} smaller slice${omitted === 1 ? '' : 's'} omitted — lower impact than those above)`);
                }
            }
            return lines;
        }

        // Which metrics to report. A question naming specific metrics gets
        // those plus their components, so the model can explain a GPU-M move
        // through costs and margin rather than being handed all 15 regardless.
        // Anything unrecognised falls back to the headline set.
        // Price columns ride along with the GPU-M set so the model can SEE a mix
        // shift, but the prompt forbids presenting them as a cause — a purchase
        // price move means different cars were sold, not worse buying.
        const AI_METRIC_GROUPS = {
            gpu_m:  ['gpu_m', 'gpu_m_costs', 'chassis_mgn_aft_subs', 'metal_mgn', 'vehicle_costs', 'write_down', 'discount', 'purch_price', 'sell_price_incl_fee'],
            gpu_s:  ['gpu_s', 'gpu_s_costs', 'fin_pen_pct', 'fin_comm_fin', 'fin_comm_all', 'product_mgn', 'finance_subsidy'],
            gpu_ms: ['gpu_ms', 'gpu_m', 'gpu_s', 'chassis_mgn_aft_subs', 'fin_pen_pct', 'fin_comm_fin', 'product_mgn'],
        };
        const AI_DEFAULT_METRICS = ['gpu_ms', 'gpu_m', 'gpu_s', 'chassis_mgn_aft_subs', 'fin_pen_pct', 'fin_comm_fin', 'product_mgn'];

        // Returns null when the question names no metric at all, so callers can
        // tell "asked about nothing in particular" apart from "asked about the
        // headline metrics". buildAiContext treats null as the default set; a
        // follow-up treats it as "no new data needed".
        function aiNamedMetrics(question) {
            const q = (question || '').toLowerCase();
            const wanted = new Set();
            // Order matters: check the compound name first so "GPU-MS" is not
            // matched as "GPU-M". Hyphen, space and underscore all accepted.
            const named = [
                [/gpu[\s_-]*ms/, 'gpu_ms'],
                [/gpu[\s_-]*m\b/, 'gpu_m'],
                [/gpu[\s_-]*s\b/, 'gpu_s'],
            ];
            for (const [re, key] of named) {
                if (re.test(q)) AI_METRIC_GROUPS[key].forEach(k => wanted.add(k));
            }
            // Free-text mentions of a metric by its exact name, for questions
            // that do not use the GPU shorthand at all.
            let namedExactly = false;
            for (const m of AI_METRICS) {
                if (q.includes(m.label.toLowerCase())) { wanted.add(m.key); namedExactly = true; }
            }
            // The category rules below are deliberately broad, for vague
            // questions ("was it costs?"). A question that named a metric
            // exactly ("vehicle costs") already has what it asked for, and
            // widening it there would pull in unrelated metrics — and, in a
            // follow-up, force a needless rebuild of the whole data table.
            if (/\bcost/.test(q) && !namedExactly) ['gpu_m_costs', 'gpu_s_costs', 'vehicle_costs'].forEach(k => wanted.add(k));
            if (/financ|commission/.test(q)) ['fin_pen_pct', 'fin_comm_fin', 'fin_comm_all', 'finance_subsidy'].forEach(k => wanted.add(k));
            if (/penetration|\bpen\b/.test(q)) ['fin_pen_pct', 'fin_comm_fin', 'gpu_s'].forEach(k => wanted.add(k));
            if (/\bproduct/.test(q))  ['product_mgn', 'gpu_s'].forEach(k => wanted.add(k));
            if (/margin|chassis/.test(q)) ['chassis_mgn_aft_subs', 'metal_mgn'].forEach(k => wanted.add(k));
            if (/price|discount/.test(q)) ['sell_price_incl_fee', 'purch_price', 'discount'].forEach(k => wanted.add(k));

            return wanted.size ? wanted : null;
        }

        // Metrics to report for a question — the named ones, or the headline
        // set when the question names none.
        function aiMetricsForQuestion(question) {
            const keys = aiNamedMetrics(question) || new Set(AI_DEFAULT_METRICS);
            return AI_METRICS.filter(m => keys.has(m.key));
        }

        // Build the whole context string sent to the model. The question is used
        // to decide which metrics to report — not to filter the data.
        function buildAiContext(question) {
            const rows = filteredRows();
            if (!rows.length) return { text: null, rows: 0, periods: [] };

            // Only the metrics the question is about — see aiMetricsForQuestion.
            const metrics = aiMetricsForQuestion(question);

            // Selected periods in chronological order. These are the comparison
            // axis — every figure below is reported per period.
            const periodSet = new Set();
            rows.forEach(r => {
                const p = periodOf(r, lensApplied, granularityApplied);
                if (p) periodSet.add(p);
            });
            let periods = [...periodSet].sort((a, b) =>
                granularityApplied === 'week'
                    ? isoWeekMonday(a) - isoWeekMonday(b)
                    : a.localeCompare(b));
            // Cap to the last 4 periods plus the current one — older history
            // was inflating the prompt (every breakdown line repeats n=/mean=
            // for every period) without adding anything the change/impact
            // maths (which only ever looks at the last two) actually uses.
            periods = periods.slice(-5);

            const out = [];
            out.push(`Lens: ${lensApplied === 'deliveries' ? 'Delivery date (delivered vehicles)' : 'Order date (acknowledged + delivered)'}`);
            out.push(`Granularity: ${granularityApplied === 'day' ? 'calendar day'
                                   : granularityApplied === 'week' ? 'ISO week' : 'calendar month'}`);
            out.push(`Periods (chronological, last is most recent): ${periods.join(', ')}`);
            const activeSlicers = SLICERS
                .filter(s => s.key !== 'period' && slicerSel[s.key].size)
                .map(s => `${s.label}=${[...slicerSel[s.key]].join('/')}`);
            out.push(`Active filters: ${activeSlicers.length ? activeSlicers.join('; ') : 'none beyond the period selection'}`);
            out.push(`Vehicles in scope: ${rows.length}`);
            out.push(`Slices below ${AI_MIN_UNITS} vehicles in a period show mean as "-".`);
            out.push(`Metrics reported: ${metrics.map(m => m.label).join(', ')}.`);
            out.push('');
            out.push('impact = change x units in the latest period = that slice\'s contribution to the overall movement, in GBP.');
            out.push(`Each breakdown lists its ${AI_TOP_SLICES} biggest movers by absolute impact; smaller slices are counted but not listed.`);
            out.push('');

            // Overall, then each dimension on its own, then every pair.
            out.push(...aiRenderBreakdown('Total', aiBreakdown(rows, periods, () => 'All vehicles'), periods, metrics));
            out.push('');
            for (const d of AI_DIMS) {
                const bd = aiBreakdown(rows, periods,
                    r => normVal(slicerValue(r, d.key, lensApplied, granularityApplied)) || null);
                const lines = aiRenderBreakdown(d.label, bd, periods, metrics);
                if (lines.length) { out.push(...lines); out.push(''); }
            }
            for (let i = 0; i < AI_PAIR_DIMS.length; i++) {
                for (let j = i + 1; j < AI_PAIR_DIMS.length; j++) {
                    const a = AI_PAIR_DIMS[i], b = AI_PAIR_DIMS[j];
                    const bd = aiBreakdown(rows, periods, r => {
                        const av = normVal(slicerValue(r, a.key, lensApplied, granularityApplied));
                        const bv = normVal(slicerValue(r, b.key, lensApplied, granularityApplied));
                        return (av && bv) ? `${av} + ${bv}` : null;
                    });
                    const lines = aiRenderBreakdown(`${a.label} x ${b.label}`, bd, periods, metrics);
                    if (lines.length) { out.push(...lines); out.push(''); }
                }
            }
            return { text: out.join('\n'), rows: rows.length, periods, metrics };
        }

        // ── Conversation state ──────────────────────────────────────────────
        // aiHistory is the thread as model turns: [{role, text}, ...]. Turn 0
        // holds the whole data table, so it is sent once and every later turn
        // rides on it — that is what keeps a long thread short. aiContextKey
        // records which selection the table describes; if
        // the user changes lens/granularity/slicers the thread no longer
        // matches what is on screen and is started fresh.
        let aiHistory = [];
        let aiContextKey = null;
        let aiFirstTurnMetrics = [];

        // Identity of the current on-screen selection. A change here means the
        // data in turn 0 no longer describes what the user is looking at.
        function aiSelectionKey() {
            const sel = SLICERS.map(s => `${s.key}:${[...slicerSel[s.key]].sort().join(',')}`).join('|');
            return `${lensApplied}/${granularityApplied}/${sel}`;
        }

        // A follow-up reuses the data already in the thread unless it EXPLICITLY
        // names a metric that was never sent — e.g. opening on GPU-M, then
        // asking about finance commission. A vague follow-up ("why?", "which
        // suppliers?") names nothing, so it rides on the existing table; using
        // the default metric set here instead would rebuild on every follow-up
        // and defeat the point of the thread.
        function aiNeedsFreshData(question) {
            const named = aiNamedMetrics(question);
            if (!named) return false;
            return [...named].some(k => !aiFirstTurnMetrics.includes(k));
        }

        function aiRenderMarkdown(text) {
            return escapeHtml(text)
                .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                .replace(/^[-*]\s+/gm, '• ')
                .replace(/\n/g, '<br>');
        }

        // Redraw the whole thread. Questions and answers alternate; the meta
        // line under each answer reports what that turn actually cost.
        function aiRenderThread(pending) {
            const outEl = document.getElementById('ai-answer');
            const parts = [];
            for (const t of aiHistory) {
                if (t.hidden) continue;
                parts.push(t.role === 'user'
                    ? `<div class="ai-turn-q">${escapeHtml(t.display ?? t.text)}</div>`
                    : `<div class="ai-answer-body">${aiRenderMarkdown(t.text)}</div>`
                      + (t.meta ? `<div class="ai-meta">${t.meta}</div>` : ''));
            }
            if (pending) {
                parts.push(`<div class="ai-turn-q">${escapeHtml(pending)}</div>`);
                parts.push('<div class="ai-thinking"><div class="css-spinner"></div><span>Analysing…</span></div>');
            }
            outEl.innerHTML = parts.join('');
            outEl.style.display = parts.length ? 'block' : 'none';
            outEl.scrollTop = outEl.scrollHeight;
        }

        function aiResetThread() {
            aiHistory = [];
            aiContextKey = null;
            aiFirstTurnMetrics = [];
            const outEl = document.getElementById('ai-answer');
            outEl.innerHTML = '';
            outEl.style.display = 'none';
            document.getElementById('ai-question').value = '';
            updateAiControls();
        }

        function updateAiControls() {
            const started = aiHistory.length > 0;
            document.getElementById('ai-new-btn').style.display = started ? '' : 'none';
            document.getElementById('ai-question').placeholder = started
                ? 'Ask a follow-up…'
                : 'e.g. what caused the drop in GPU-M this week?';
        }

        async function askAi() {
            const input = document.getElementById('ai-question');
            const outEl = document.getElementById('ai-answer');
            const btn = document.getElementById('ai-ask-btn');
            const question = input.value.trim();
            if (!question) { input.focus(); return; }

            // Changing the selection invalidates the thread's data table.
            const key = aiSelectionKey();
            const staleThread = aiHistory.length > 0 && key !== aiContextKey;
            const isFollowUp = aiHistory.length > 0 && !staleThread && !aiNeedsFreshData(question);

            let ctx = null;
            if (!isFollowUp) {
                ctx = buildAiContext(question);
                if (!ctx.text) {
                    outEl.style.display = 'block';
                    outEl.innerHTML = '<div class="ai-error">No vehicles in the current selection to analyse.</div>';
                    return;
                }
                // A new data table starts a new thread — the old answers were
                // about different figures.
                aiHistory = [];
                aiContextKey = key;
                aiFirstTurnMetrics = ctx.metrics.map(m => m.key);
            }

            input.value = '';
            btn.disabled = true;
            aiRenderThread(question);

            try {
                const body = isFollowUp
                    ? { question, history: aiHistory.map(t => ({ role: t.role, text: t.text })) }
                    : { question, context: ctx.text };
                const resp = await fetch(BASE_URL + '/reports/orders-deliveries/ask-ai', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await resp.json();
                if (data.status !== 'ok') throw new Error(data.message || 'Request failed');

                // Turn 0 must store the SAME text the server built its first
                // prompt from — data table included — or replaying the thread
                // on a follow-up would send the question without the figures.
                // `display` keeps the table off the screen.
                aiHistory.push(isFollowUp
                    ? { role: 'user', text: question }
                    : { role: 'user', text: data.first_turn_text, display: question });
                const meta = (isFollowUp
                        ? 'follow-up · uses the data already sent'
                        : `${ctx.rows.toLocaleString('en-GB')} vehicles · `
                          + `${ctx.periods.length} period${ctx.periods.length === 1 ? '' : 's'} · `
                          + `${ctx.metrics.length} metric${ctx.metrics.length === 1 ? '' : 's'} · `
                          + `${(ctx.text.length / 1024).toFixed(0)}KB sent`)
                    + (data.tokens_in ? ` · ${data.tokens_in.toLocaleString('en-GB')} tokens in` : '');
                aiHistory.push({
                    role: 'model', text: data.answer,
                    meta: (data.truncated ? '<span class="ai-truncated">Answer was cut off — ask a narrower question for the rest.</span> · ' : '') + meta,
                });
                aiRenderThread(null);
            } catch (e) {
                let msg = escapeHtml(e.message);
                if (/quota|rate.?limit|resource_exhausted/i.test(e.message)) {
                    msg = 'The request was too large for the model to answer. '
                        + (isFollowUp
                            ? 'Try "New question" to start a shorter thread.'
                            : 'Narrow the period selection or the slicers and try again'
                              + (ctx ? ` — this one sent ${(ctx.text.length / 1024).toFixed(0)}KB.` : '.'))
                        + `<div class="ai-meta">${escapeHtml(e.message)}</div>`;
                }
                aiRenderThread(null);
                outEl.insertAdjacentHTML('beforeend',
                    `<div class="ai-turn-q">${escapeHtml(question)}</div><div class="ai-error">${msg}</div>`);
                input.value = question;   // so it can be retried without retyping
            } finally {
                btn.disabled = false;
                updateAiControls();
                input.focus();
            }
        }

        function showCacheNote(data) {
            const note = document.getElementById('cache-note');
            if (data.stale) {
                note.textContent = 'Showing last cached data (refresh failed)';
                return;
            }
            if (!data.cached_at) { note.textContent = ''; return; }
            const when = new Date(data.cached_at * 1000);
            note.textContent = 'Data as of ' + when.toLocaleString('en-GB',
                { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
        }

        async function load(force) {
            const btn = document.getElementById('refresh-btn');
            btn.disabled = true;
            setApplyLoading(true);   // Apply greys out while loading, back to green when done
            showLoading(force ? 'Refreshing from Snowflake...' : 'Loading vehicles...');
            try {
                const url = BASE_URL + '/reports/orders-deliveries/data' + (force ? '?refresh=1' : '');
                const resp = await fetch(url);
                const data = await resp.json();
                if (data.status !== 'ok') throw new Error(data.message || 'Failed to load data');
                allRows = data.rows || [];
                applyBandOrders(data.band_orders);
                fetchStartIso = data.fetch_start || null;
                defaultPeriodFilter(data.today);
                showCacheNote(data);
                rerender();
            } catch (e) {
                const html = `<div class="empty-state">Could not load data: ${escapeHtml(e.message)}</div>`;
                document.getElementById('grid-area').innerHTML = html;
                const summaryArea = document.getElementById('summary-area');
                summaryArea.classList.add('is-loading');   // full width, matching the control bar, rather than shrink-wrapping the message
                summaryArea.innerHTML = html;
                awsLoginIfExpired(document.getElementById('grid-area').firstElementChild, e.message);
            } finally {
                btn.disabled = false;
                setApplyLoading(false);
            }
        }

        // Refresh Data starts clean: every filter cleared and the period back to
        // today's default, so nobody is left looking at a filter set earlier
        // (e.g. in a tab left open overnight) and taking it for the full figures.
        // The Lens / View / Group By buttons show their staged choice; Refresh
        // also commits it, so the figures always match what the buttons show.
        function refreshData() {
            SLICERS.forEach(s => slicerSel[s.key].clear());
            periodDefaulted = false;
            lensApplied = lensPending;
            viewApplied = viewPending;
            granularityApplied = granularityPending;
            load(true);
        }

        document.querySelectorAll('#lens-toggle button').forEach(b =>
            b.addEventListener('click', () => setLens(b.dataset.lens)));
        document.querySelectorAll('#view-toggle button').forEach(b =>
            b.addEventListener('click', () => setView(b.dataset.view)));
        document.querySelectorAll('#granularity-toggle button').forEach(b =>
            b.addEventListener('click', () => setGranularity(b.dataset.granularity)));

        // The Ask AI panel is only rendered for tdrayson (see the template).
        const aiQuestionEl = document.getElementById('ai-question');
        if (aiQuestionEl) {
            aiQuestionEl.addEventListener('keydown', e => {
                if (e.key === 'Enter') { e.preventDefault(); askAi(); }
            });
        }

        loadFont();
        loadCollapsedTables();
        load();
