/* Trade Report.
 *
 * The server sends every row of trade_sales (uk_dbt_product_data ->
 * trade_sales__p) once; everything on the page is a cut of those rows worked
 * out here, the same way the old "Trade report LIVE.xlsx" did it with
 * COUNTIFS / AVERAGEIFS over its data sheet.
 *
 * Metrics (one row each in every table, matching the workbook):
 *   Units                 count of rows
 *   Purchase Price        average purchase price ex VAT, rows with a CAP value only
 *   Sale Price            average sale price ex VAT, rows with a CAP value only
 *   Gross Margin          average gross margin, all rows
 *   CAP Clean             average CAP live clean, rows with a CAP value only
 *   Purchase % CAP Clean  Purchase Price / CAP Clean
 *   Sell % CAP Clean      Sale Price / CAP Clean
 *   Total Costs           average total costs, all rows
 *   Net Margin            average net margin (gross margin - total costs)
 *   Total Margin          sum of net margin
 *
 * The workbook's Total columns were SUMPRODUCTs of per-site averages; here a
 * total is worked out from the pooled rows, which is the same for the counts
 * and margins and slightly truer for the CAP-only averages.
 *
 * Loads after Chart.js, charts.js (chart cards, escHtml) and trade_vehicles.js
 * (the View Vehicles popup and Settings).
 */
(function () {
    'use strict';

    const BASE_URL = window.BASE_URL || '';

    // ── State ────────────────────────────────────────────────────────────────
    let rows = [];                 // every trade sale, as objects
    let cachedAt = null;
    let view = 'vehicles';
    let category = '';             // '' = every supplier category
    // Ticked in the Site and Customer filters; none ticked means all.
    const ticked = { site: new Set(), customer: new Set() };
    let period = null;             // 'M:yyyy-MM-dd' or 'Y:yyyy'
    // Date range on the invoice date, 'yyyy-MM-dd' or ''. While either is set
    // it replaces the period, on every view.
    let dateFrom = '';
    let dateTo = '';
    let charts = [];               // live Chart instances; destroyed on re-render
    let gridSort = { key: 'invoice_date', dir: -1 };
    let gridSearch = '';
    let marginChart = 'total';     // 'total' or 'net' (average): which margin the charts show

    // The sub views of each main view, and the one each last showed.
    const VIEW_GROUPS = [
        { key: 'month', subs: [{ view: 'month_site', label: 'By Site' }, { view: 'month_auction', label: 'By Auction' }] },
        { key: 'year', subs: [{ view: 'year_site', label: 'By Site' }, { view: 'year_auction', label: 'By Auction' }, { view: 'year_month', label: 'By Month' }] },
    ];
    const lastSub = { month: 'month_site', year: 'year_site' };

    const SITE = 'Site';
    const CATEGORIES = ['PX/Buy In', 'Buy Back', 'B2B Purchase'];
    const BAR_COLOUR = '#0d3d6e';
    const BAR_NEG_COLOUR = '#c0504d';

    // ── Formatting ───────────────────────────────────────────────────────────
    function fmtMoney(v) {
        if (v === null || v === undefined || v === '' || isNaN(v)) return '';
        const n = Math.round(Number(v));
        return (n < 0 ? '-£' : '£') + Math.abs(n).toLocaleString('en-GB');
    }
    function fmtNum(v) {
        if (v === null || v === undefined || v === '' || isNaN(v)) return '';
        return Number(v).toLocaleString('en-GB');
    }
    function fmtPct(v) {
        if (v === null || v === undefined || v === '' || isNaN(v)) return '';
        return (Number(v) * 100).toFixed(1) + '%';
    }
    function fmtDate(v) {
        if (!v) return '';
        const m = String(v).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/);
        return m ? `${m[3]}/${m[2]}/${m[1]}` : String(v);
    }
    const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    // '2026-10-01' -> 'Oct 2026'; short -> 'Oct 26'
    function monthLabel(iso, short = false) {
        const m = String(iso || '').match(/^(\d{4})-(\d{2})/);
        if (!m) return String(iso || '');
        const name = MONTHS_SHORT[Number(m[2]) - 1];
        return short ? `${name} ${m[1].slice(2)}` : `${name} ${m[1]}`;
    }
    function fyLabel(fy) { return `FY ${fy}-${Number(fy) + 1}`; }
    // The 12 months of a financial year, October to September, as 'yyyy-MM-dd'.
    function fyMonths(fy) {
        const out = [];
        for (let i = 0; i < 12; i++) {
            const y = Number(fy) + (i >= 3 ? 1 : 0);
            const m = ((9 + i) % 12) + 1;
            out.push(`${y}-${String(m).padStart(2, '0')}-01`);
        }
        return out;
    }

    // ── Metrics ──────────────────────────────────────────────────────────────
    const METRICS = [
        { key: 'units',    label: 'Units',                fmt: fmtNum },
        { key: 'purchase', label: 'Avg Purchase Price',   fmt: fmtMoney },
        { key: 'sale',     label: 'Avg Sale Price',       fmt: fmtMoney },
        { key: 'gross',    label: 'Avg Gross Margin',     fmt: fmtMoney },
        { key: 'cap',      label: 'Avg CAP Clean',        fmt: fmtMoney },
        { key: 'purchPct', label: 'Purchase % CAP Clean', fmt: fmtPct },
        { key: 'sellPct',  label: 'Sell % CAP Clean',     fmt: fmtPct },
        { key: 'costs',    label: 'Avg Total Costs',      fmt: fmtMoney },
        { key: 'net',      label: 'Avg Net Margin',       fmt: fmtMoney },
        { key: 'total',    label: 'Total Net Margin',     fmt: fmtMoney, emphasis: true },
    ];
    const METRIC_BY_KEY = Object.fromEntries(METRICS.map(m => [m.key, m]));
    // The workbook's six charts under each site table, in its order.
    const SITE_CHARTS = ['units', 'net', 'total', 'sale', 'sellPct', 'purchPct'];
    // The workbook's two charts for each site's or block's year.
    const YEAR_CHARTS = ['total', 'units'];
    // Buy Backs and B2B Purchases blocks on the month view.
    const SHORT_METRICS = ['units', 'net', 'total'];

    const num = v => (v === null || v === undefined || v === '' || isNaN(v)) ? null : Number(v);
    function avg(list, pick) {
        let sum = 0, n = 0;
        list.forEach(r => { const v = num(pick(r)); if (v !== null) { sum += v; n += 1; } });
        return n ? sum / n : null;
    }

    function calcMetrics(list) {
        if (!list.length) return null;
        const withCap = list.filter(r => num(r.cap_clean) > 0);
        const purchase = avg(withCap, r => r.purchase_price_ex_vat);
        const sale = avg(withCap, r => r.net_sale_amount);
        const cap = avg(withCap, r => r.cap_clean);
        const gross = avg(list, r => r.gross_margin);
        const costs = avg(list, r => r.total_costs);
        const net = avg(list, r => r.net_margin);
        let total = 0;
        list.forEach(r => { total += num(r.net_margin) || 0; });
        return {
            units: list.length,
            purchase, sale, gross, cap,
            purchPct: cap ? purchase / cap : null,
            sellPct: cap ? sale / cap : null,
            costs, net, total,
        };
    }

    // ── Row groupings ────────────────────────────────────────────────────────
    function distinct(list, pick) {
        const seen = new Set();
        list.forEach(r => { const v = pick(r); if (v !== null && v !== undefined && v !== '') seen.add(v); });
        return Array.from(seen);
    }
    // Columns are taken from the whole dataset, not the period, so a site with
    // nothing sold this month still shows (as the workbook's fixed lists did).
    // With sites ticked in the filter, only those, so the rest are not empty columns.
    const siteTicked = r => !ticked.site.size || ticked.site.has(r.branch_name);
    function siteNames() {
        return distinct(rows.filter(r => r.branch_category === SITE && siteTicked(r)), r => r.branch_name).sort();
    }
    function bcbPrepNames() {
        const names = distinct(rows.filter(r => (r.branch_category === 'BCB' || r.branch_category === 'Prep') && siteTicked(r)), r => r.branch_name);
        return names.sort((a, b) => (a === 'BCB' ? -1 : b === 'BCB' ? 1 : a.localeCompare(b)));
    }
    // Auctions in the rows given, busiest first.
    function auctionNames(list) {
        const counts = {};
        list.forEach(r => { const a = r.auction || 'Unknown'; counts[a] = (counts[a] || 0) + 1; });
        return Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    }
    // Only the customers ticked in Settings > Auctions count as auctions.
    function onlyAuctions(list) {
        const chosen = new Set(window.TRADE_AUCTIONS || []);
        return list.filter(r => chosen.has(r.auction));
    }
    function monthsAvailable() {
        return distinct(rows, r => r.month).sort();
    }
    function yearsAvailable() {
        return distinct(rows, r => r.financial_year).map(Number).sort((a, b) => a - b);
    }

    const rangeOn = () => !!(dateFrom || dateTo);

    const customerOf = r => r.auction || 'Unknown';

    // Rows for the chosen date range or period.
    function periodRows() {
        if (rangeOn()) {
            // Dates are 'yyyy-MM-dd', so text order is date order.
            return rows.filter(r => {
                const day = String(r.invoice_date || '').slice(0, 10);
                return day && (!dateFrom || day >= dateFrom) && (!dateTo || day <= dateTo);
            });
        }
        if (!period) return [];
        const [kind, value] = [period.slice(0, 1), period.slice(2)];
        return kind === 'M' ? rows.filter(r => r.month === value) : rows.filter(r => String(r.financial_year) === value);
    }

    const searchOverrides = () => view === 'vehicles' && !!gridSearch.trim();

    // Those rows, narrowed by supplier category, site and customer.
    function scopeRows() {
        // A search on the Vehicles view looks through every sale, ignoring all filters.
        if (searchOverrides()) return rows;
        return periodRows().filter(r => (!category || r.supplier_category === category) && siteTicked(r)
            && (!ticked.customer.size || ticked.customer.has(customerOf(r))));
    }

    // Every filter on now, in words. The period is not one: it is always set.
    function activeFilters() {
        const out = [];
        if (rangeOn()) out.push(`Invoice Date: ${periodText()}`);
        if (ticked.site.size) out.push(`Site: ${[...ticked.site].join(', ')}`);
        if (ticked.customer.size) out.push(`Customer: ${[...ticked.customer].join(', ')}`);
        if (category) out.push(`Supplier Category: ${category}`);
        if (view === 'vehicles' && gridSearch.trim()) out.push(`Search: ${gridSearch.trim()}`);
        return out;
    }
    function periodIsYear() { return !!period && period.startsWith('Y'); }
    function periodValue() { return period ? period.slice(2) : null; }
    function periodText() {
        if (rangeOn()) {
            if (dateFrom && dateTo) return `${fmtDate(dateFrom)} to ${fmtDate(dateTo)}`;
            return dateFrom ? `From ${fmtDate(dateFrom)}` : `Up To ${fmtDate(dateTo)}`;
        }
        if (!period) return '';
        return periodIsYear() ? fyLabel(periodValue()) : monthLabel(periodValue());
    }
    function periodTotalLabel() {
        return rangeOn() ? 'Total' : `${fyLabel(periodValue())} Total`;
    }

    // ── Tables ───────────────────────────────────────────────────────────────
    function cellHtml(metric, m, extraClass = '') {
        const v = m ? m[metric.key] : null;
        if (v === null || v === undefined || isNaN(v)) return `<td class="blank ${extraClass}"></td>`;
        const neg = v < 0 ? ' neg' : '';
        return `<td class="${extraClass}${neg}">${metric.fmt(v)}</td>`;
    }

    // The rows behind each View button drawn, found again by its number when
    // it is clicked (trade_vehicles.js shows them). Reset on every render.
    let viewGroups = [];

    function viewCellHtml(cornerLabel, label, list, extraClass = '') {
        if (!list.length) return `<td class="blank ${extraClass}"></td>`;
        const when = periodText();
        const title = [cornerLabel, label, label.includes(when) ? '' : when].filter(Boolean).join(' - ');
        viewGroups.push({ title, rows: list });
        return `<td class="${extraClass}"><button type="button" class="btn btn-edit btn-view" data-view-group="${viewGroups.length - 1}" title="View the vehicles behind this column">View</button></td>`;
    }

    // columns: [{label, rows}], one per site / month / auction. totalRows (if
    // given) adds a Total column worked out from those pooled rows. The last
    // row holds a View button for each column's vehicles.
    function metricTableHtml({ columns, metricKeys = null, totalRows = null, totalLabel = 'Total', cornerLabel = '' }) {
        const metrics = (metricKeys || METRICS.map(m => m.key)).map(k => METRIC_BY_KEY[k]);
        const colMetrics = columns.map(c => calcMetrics(c.rows));
        const totalMetrics = totalRows ? calcMetrics(totalRows) : null;
        const head = ['<tr>', `<th>${escHtml(cornerLabel)}</th>`]
            .concat(columns.map(c => `<th>${escHtml(c.label)}</th>`))
            .concat(totalRows ? [`<th class="total">${escHtml(totalLabel)}</th>`] : [])
            .concat(['</tr>']).join('');
        const body = metrics.map(metric => {
            const cells = colMetrics.map(m => cellHtml(metric, m)).join('')
                + (totalRows ? cellHtml(metric, totalMetrics, 'total') : '');
            return `<tr${metric.emphasis ? ' class="emphasis"' : ''}><td>${escHtml(metric.label)}</td>${cells}</tr>`;
        }).join('');
        const viewCells = columns.map(c => viewCellHtml(cornerLabel, c.label, c.rows)).join('')
            + (totalRows ? viewCellHtml(cornerLabel, totalLabel, totalRows, 'total') : '');
        const viewRow = `<tr class="view-row"><td>View Vehicles</td>${viewCells}</tr>`;
        return `<div class="summary-table-wrap"><table class="summary"><thead>${head}</thead><tbody>${body}${viewRow}</tbody></table></div>`;
    }

    // ── Charts ───────────────────────────────────────────────────────────────
    // Charts are only drawn once their canvases are in the page, so each
    // section queues its charts here and renderReport() builds them after
    // the HTML is set.
    let pendingCharts = [];
    let chartSeq = 0;

    // One chart card per metric, bars = the columns given.
    function chartsHtml(columns, metricKeys, titlePrefix = '', perRow = 3) {
        const colMetrics = columns.map(c => calcMetrics(c.rows));
        // The margin chart shows the total or the average, as chosen above the
        // page; the two never both show.
        const shown = [...new Set(metricKeys.map(k => (k === 'total' || k === 'net') ? marginChart : k))];
        const cards = shown.map(key => {
            const metric = METRIC_BY_KEY[key];
            const canvasId = `trade-chart-${++chartSeq}`;
            pendingCharts.push({
                canvasId,
                labels: columns.map(c => c.label),
                values: colMetrics.map(m => (m && m[key] !== null && m[key] !== undefined) ? m[key] : null),
                fmt: metric.fmt,
            });
            const title = titlePrefix ? `${metric.label} - ${titlePrefix}` : metric.label;
            return chartCardHtml({ canvasId, title, legend: false });
        }).join('');
        return `<div class="charts-row even${perRow === 2 ? ' two' : ''}">${cards}</div>`;
    }

    // Figures on the bars: above a positive bar, below a negative one.
    function barLabelPlugin(fmt) {
        return {
            id: 'tradeBarLabels',
            afterDatasetsDraw(chart) {
                const meta = chart.getDatasetMeta(0);
                const values = chart.data.datasets[0].data;
                const ctx = chart.ctx;
                ctx.save();
                ctx.font = 'bold 10px DM Sans, system-ui, sans-serif';
                ctx.fillStyle = '#0f172a';
                ctx.textAlign = 'center';
                meta.data.forEach((bar, i) => {
                    const v = values[i];
                    if (v === null || v === undefined) return;
                    const below = v < 0;
                    ctx.textBaseline = below ? 'top' : 'bottom';
                    ctx.fillText(fmt(v), bar.x, bar.y + (below ? 3 : -3));
                });
                ctx.restore();
            },
        };
    }

    function buildBarChart({ canvasId, labels, values, fmt }) {
        const el = document.getElementById(canvasId);
        if (!el) return;
        const vals = values.filter(v => v !== null && v !== undefined);
        const max = vals.length ? Math.max(0, ...vals) : 0;
        const min = vals.length ? Math.min(0, ...vals) : 0;
        const pad = (max - min) * 0.18 || 1;
        const chart = new Chart(el.getContext('2d'), {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: values.map(v => (v !== null && v < 0) ? BAR_NEG_COLOUR : BAR_COLOUR),
                    borderWidth: 0,
                    categoryPercentage: 0.8,
                    barPercentage: 0.85,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: c => fmt(c.parsed.y) } },
                },
                scales: {
                    x: {
                        border: { display: false },
                        grid: { display: false },
                        ticks: { font: { size: 10 }, maxRotation: 45, minRotation: 0, autoSkip: false },
                    },
                    y: {
                        suggestedMin: min < 0 ? min - pad : 0,
                        suggestedMax: max + pad,
                        border: { display: false },
                        grid: { color: 'rgba(0,0,0,0.06)' },
                        ticks: { font: { size: 10 }, callback: v => fmt(v) },
                    },
                },
            },
            plugins: [barLabelPlugin(fmt)],
        });
        charts.push(chart);
    }

    // ── Sections ─────────────────────────────────────────────────────────────
    // Charts are collected here and shown together under the tables (renderReport).
    let chartBlocks = [];

    // Charts go under all the tables, except where a section asks for its
    // charts beside its table (each site's year on Year By Site).
    function sectionHtml(title, note, tableHtml, chartsBlock = '', chartsBeside = false) {
        const beside = chartsBeside && chartsBlock && tableHtml;
        if (chartsBlock && !beside) chartBlocks.push({ title, html: chartsBlock });
        const table = tableHtml ? `<div class="tables-block"><div class="panel shadow-sm">${tableHtml}</div></div>` : '';
        return `
        <div class="report-section">
            <h3 class="section-title">${escHtml(title)}${note ? `<span class="section-note">${escHtml(note)}</span>` : ''}</h3>
            ${beside ? `<div class="table-with-charts">${table}<div class="beside-charts">${chartsBlock}</div></div>` : table}
        </div>`;
    }

    const byBranch = (list, name) => list.filter(r => r.branch_name === name);
    const byCategory = (list, cat) => cat ? list.filter(r => r.supplier_category === cat) : list;
    const sitesOnly = list => list.filter(r => r.branch_category === SITE);
    const bcbPrepOnly = list => list.filter(r => r.branch_category === 'BCB' || r.branch_category === 'Prep');

    function siteColumns(list) {
        return siteNames().map(name => ({ label: name, rows: byBranch(list, name) }));
    }
    // Across the top of a year view: the financial year's 12 months, or with a
    // date range the months that range has sales in.
    function monthColumns(list, fy) {
        const months = rangeOn() ? distinct(scopeRows(), r => r.month).sort() : fyMonths(fy);
        return months.map(m => ({ label: monthLabel(m, true), rows: list.filter(r => r.month === m) }));
    }

    // Month By Site: the workbook's "MTD by site" sheet. With every category
    // shown it keeps the sheet's layout: PX/Buy In in full, Buy Backs and B2B
    // Purchases as three-line blocks, then BCB & Prep Centres and the totals.
    function renderMonthSite(scope) {
        const site = sitesOnly(scope);
        const label = periodText();
        let html = '';
        if (!category) {
            const px = byCategory(site, 'PX/Buy In');
            html += sectionHtml(`Sites - PX/Buy In - ${label}`, '', metricTableHtml({ columns: siteColumns(px), totalRows: px, cornerLabel: 'PX/Buy In' }),
                chartsHtml(siteColumns(px), SITE_CHARTS, 'PX/Buy In'));
            const bb = byCategory(site, 'Buy Back');
            html += sectionHtml(`Sites - Buy Backs - ${label}`, '', metricTableHtml({ columns: siteColumns(bb), metricKeys: SHORT_METRICS, totalRows: bb, cornerLabel: 'Buy Back' }));
            const b2b = byCategory(site, 'B2B Purchase');
            html += sectionHtml(`Sites - B2B Purchases - ${label}`, '', metricTableHtml({ columns: siteColumns(b2b), metricKeys: SHORT_METRICS, totalRows: b2b, cornerLabel: 'B2B Purchase' }));
            html += sectionHtml(`Sites - All Categories - ${label}`, 'PX/Buy In, Buy Backs and B2B Purchases together', metricTableHtml({ columns: siteColumns(site), totalRows: site, cornerLabel: 'All' }));
        } else {
            html += sectionHtml(`Sites - ${category} - ${label}`, '', metricTableHtml({ columns: siteColumns(site), totalRows: site, cornerLabel: category }),
                chartsHtml(siteColumns(site), SITE_CHARTS, category));
        }
        const bp = bcbPrepOnly(scope);
        const bpCols = bcbPrepNames().map(name => ({ label: name, rows: byBranch(bp, name) }));
        html += sectionHtml(`BCB & Prep Centres - ${label}`, category || 'All supplier categories', metricTableHtml({ columns: bpCols, totalRows: bp, cornerLabel: 'BCB & Prep' }));
        html += sectionHtml(`Grand Totals - ${label}`, category || 'All supplier categories', metricTableHtml({
            columns: [{ label: 'Sites', rows: site }, { label: 'BCB & Prep Centres', rows: bp }],
            totalRows: scope, totalLabel: 'Grand Total', cornerLabel: 'Totals',
        }));
        return html;
    }

    // Month By Auction: the "MTD by auction" sheet, every branch together.
    function renderMonthAuction(scope) {
        scope = onlyAuctions(scope);
        const cols = auctionNames(scope).map(a => ({ label: a, rows: scope.filter(r => (r.auction || 'Unknown') === a) }));
        if (!cols.length) return emptyHtml();
        const label = periodText();
        let html = sectionHtml(`Auctions - ${label}`, category || 'All supplier categories', metricTableHtml({ columns: cols, totalRows: scope, cornerLabel: 'Auction' }),
            chartsHtml(cols, SITE_CHARTS));
        // Where each auction's cars came from.
        cols.forEach(c => {
            html += sectionHtml(`${c.label} By Site - ${label}`, '', metricTableHtml({ columns: siteColumns(sitesOnly(c.rows)), totalRows: c.rows, cornerLabel: c.label }));
        });
        return html;
    }

    // Year By Month: the "FYTD by month" sheet - Sites, BCB, Prep Centres and
    // Totals, each with the 12 months across.
    function renderYearMonth(scope) {
        const fy = periodValue();
        const blocks = [
            ['Sites', sitesOnly(scope)],
            ['BCB', scope.filter(r => r.branch_category === 'BCB')],
            ['Prep Centres', scope.filter(r => r.branch_category === 'Prep')],
            ['Totals', scope],
        ];
        return blocks.map(([name, list]) => {
            const cols = monthColumns(list, fy);
            return sectionHtml(`${name} - ${periodText()}`, category || 'All supplier categories',
                metricTableHtml({ columns: cols, totalRows: list, totalLabel: periodTotalLabel(), cornerLabel: name }),
                chartsHtml(cols, YEAR_CHARTS, name, 2));
        }).join('');
    }

    // Year By Site: the "FYTD by site" sheet - every site's year across the
    // top, then one block per site with its months across.
    function renderYearSite(scope) {
        const fy = periodValue();
        const site = sitesOnly(scope);
        const note = category || 'All supplier categories';
        let html = sectionHtml(`Sites Total - ${periodText()}`, note, metricTableHtml({ columns: siteColumns(site), totalRows: site, cornerLabel: 'Site' }),
            chartsHtml(siteColumns(site), SITE_CHARTS));
        const bp = bcbPrepOnly(scope);
        const bpCols = bcbPrepNames().map(name => ({ label: name, rows: byBranch(bp, name) }));
        html += sectionHtml(`BCB & Prep Centres - ${periodText()}`, note, metricTableHtml({ columns: bpCols, totalRows: bp, cornerLabel: 'BCB & Prep' }));
        siteNames().forEach(name => {
            const list = byBranch(site, name);
            const cols = monthColumns(list, fy);
            html += sectionHtml(`${name} - ${periodText()}`, note,
                metricTableHtml({ columns: cols, totalRows: list, totalLabel: periodTotalLabel(), cornerLabel: name }),
                chartsHtml(cols, YEAR_CHARTS, name, 2), true);
        });
        return html;
    }

    // Year By Auction: the "YTD by auction" asked for in the workbook's
    // comments - every auction's year across the top, then a block each.
    function renderYearAuction(scope) {
        scope = onlyAuctions(scope);
        const fy = periodValue();
        const names = auctionNames(scope);
        if (!names.length) return emptyHtml();
        const note = category || 'All supplier categories';
        const cols = names.map(a => ({ label: a, rows: scope.filter(r => (r.auction || 'Unknown') === a) }));
        let html = sectionHtml(`Auctions Total - ${periodText()}`, note, metricTableHtml({ columns: cols, totalRows: scope, cornerLabel: 'Auction' }),
            chartsHtml(cols, SITE_CHARTS));
        cols.forEach(c => {
            const months = monthColumns(c.rows, fy);
            html += sectionHtml(`${c.label} - ${periodText()}`, note,
                metricTableHtml({ columns: months, totalRows: c.rows, totalLabel: periodTotalLabel(), cornerLabel: c.label }),
                chartsHtml(months, YEAR_CHARTS, c.label, 2));
        });
        return html;
    }

    // ── Vehicles grid ────────────────────────────────────────────────────────
    const GRID_COLUMNS = [
        { key: 'vrm', label: 'VRM', cls: 'vrm' },
        { key: 'stock_id', label: 'Stock ID', num: true, fmt: v => v === null || v === undefined ? '' : String(v) },
        { key: 'supplier', label: 'Supplier' },
        { key: 'supplier_category', label: 'Category' },
        { key: 'days_in_stock', label: 'DIS', num: true, fmt: fmtNum },
        { key: 'branch_name', label: 'Site' },
        { key: 'branch_category', label: 'Site Category' },
        { key: 'auction', label: 'Customer' },
        { key: 'make', label: 'Make' },
        { key: 'model', label: 'Model' },
        { key: 'mileage', label: 'Mileage', num: true, fmt: fmtNum },
        { key: 'month', label: 'Month', fmt: v => monthLabel(v) },
        { key: 'invoice_date', label: 'Invoice Date', fmt: fmtDate },
        { key: 'vat_status', label: 'VAT Status' },
        { key: 'purchase_price_ex_vat', label: 'Purch Ex VAT', num: true, fmt: fmtMoney },
        { key: 'purchase_price_inc_vat', label: 'Purch Inc VAT', num: true, fmt: fmtMoney },
        { key: 'net_sale_amount', label: 'Sale Ex VAT', num: true, fmt: fmtMoney },
        { key: 'sale_vat_amount', label: 'VAT', num: true, fmt: fmtMoney },
        { key: 'gross_sale_amount', label: 'Sale Inc VAT', num: true, fmt: fmtMoney },
        { key: 'gross_margin', label: 'Gross Margin', num: true, fmt: fmtMoney },
        { key: 'siv', label: 'SIV', num: true, fmt: fmtMoney },
        { key: 'recon_cost', label: 'Recon', num: true, fmt: fmtMoney },
        { key: 'total_costs', label: 'Total Costs', num: true, fmt: fmtMoney },
        { key: 'net_margin', label: 'Net Margin', num: true, fmt: fmtMoney },
        { key: 'cap_id', label: 'CAP ID', num: true, fmt: v => v === null || v === undefined ? '' : String(v) },
        { key: 'cap_clean', label: 'CAP Clean', num: true, fmt: fmtMoney },
        { key: 'purchase_cap_pct', label: 'Purch CAP %', num: true, fmt: fmtPct },
        { key: 'sale_cap_pct', label: 'Sell CAP %', num: true, fmt: fmtPct },
    ];
    const SEARCH_KEYS = ['vrm', 'stock_id', 'supplier', 'branch_name', 'auction', 'make', 'model'];

    function gridRows(scope) {
        let list = scope;
        const q = gridSearch.trim().toLowerCase();
        if (q) list = list.filter(r => SEARCH_KEYS.some(k => String(r[k] ?? '').toLowerCase().includes(q)));
        const col = GRID_COLUMNS.find(c => c.key === gridSort.key) || GRID_COLUMNS[0];
        const dir = gridSort.dir;
        return list.slice().sort((a, b) => {
            const x = a[col.key], y = b[col.key];
            if (x === y) return 0;
            if (x === null || x === undefined || x === '') return 1;
            if (y === null || y === undefined || y === '') return -1;
            if (col.num) return (Number(x) - Number(y)) * dir;
            return String(x).localeCompare(String(y)) * dir;
        });
    }

    function renderVehicles(scope) {
        const list = gridRows(scope);
        const head = GRID_COLUMNS.map(c => {
            const caret = gridSort.key === c.key ? `<span class="sort-caret">${gridSort.dir > 0 ? '&#9650;' : '&#9660;'}</span>` : '';
            const th = `<th class="${c.num ? 'num' : ''}" data-sort="${c.key}">${escHtml(c.label)}${caret}</th>`;
            return th;
        }).join('');
        // Notes sits to the right of VRM (vehicle_comments.js). A changed site is
        // coloured, with an i that opens the same history.
        const body = list.map(r => '<tr>' + GRID_COLUMNS.map(c => {
            const v = r[c.key];
            const text = c.fmt ? c.fmt(v) : (v === null || v === undefined ? '' : String(v));
            const neg = c.num && num(v) !== null && num(v) < 0 ? ' neg' : '';
            const changed = c.key === 'branch_name' && r.site_change;
            // Settings > Highlighting rules, keyed by the same column names.
            const rule = TV.highlightFor(v, c.key);
            const td = `<td class="${c.num ? 'num' : ''}${c.cls ? ' ' + c.cls : ''}${neg}"${rule ? ` style="${rule}"` : ''}>${escHtml(text)}${changed ? VehicleComments.siteButton(r.stock_id, r.vrm, `Site Changed From ${r.site_change.old_site || 'Unknown'}`) : ''}${c.key === 'vrm' ? VehicleComments.button(r.stock_id, r.vrm) : ''}</td>`;
            return td;
        }).join('') + '</tr>').join('');
        return `
        <div class="report-section">
            <h3 class="section-title">Vehicles - ${escHtml(searchOverrides() ? 'All Sales' : periodText())}<span class="section-note">${escHtml(searchOverrides() ? 'Search ignores the filters' : (category || 'All supplier categories'))} -${list.length.toLocaleString('en-GB')} vehicles</span></h3>
            <div class="panel shadow-sm" style="width: 100%;">
                <div class="grid-toolbar">
                    <label for="grid-search" class="grid-search-label">Search Vehicles</label>
                    <input type="search" class="grid-search${gridSearch.trim() ? ' filter-on' : ''}" id="grid-search" placeholder="VRM, Stock ID, Supplier, Site, Customer, Make Or Model" value="${escHtml(gridSearch)}" autocomplete="off" spellcheck="false">
                    <button class="btn btn-csv${activeFilters().length ? ' filter-on' : ''}" id="csv-btn"><i class="fas fa-download"></i> ${activeFilters().length ? 'CSV (Filtered)' : 'CSV'}</button>
                </div>
                <div class="grid-wrap"><table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
            </div>
        </div>`;
    }

    function downloadCsv() {
        const list = gridRows(scopeRows());
        const esc = s => { s = String(s ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
        const lines = [GRID_COLUMNS.map(c => esc(c.label)).join(',')];
        list.forEach(r => lines.push(GRID_COLUMNS.map(c => esc(r[c.key])).join(',')));
        const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `trade_sales_${periodText().replace(/[^A-Za-z0-9]+/g, '_')}${activeFilters().length ? '_filtered' : ''}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
    }

    // ── Rendering ────────────────────────────────────────────────────────────
    function emptyHtml() {
        return `<div class="panel shadow-sm"><div class="empty-state">No trade sales for ${escHtml(periodText())}${category ? ` (${escHtml(category)})` : ''}.</div></div>`;
    }

    function renderReport() {
        charts.forEach(c => c.destroy());
        charts = [];
        pendingCharts = [];
        chartBlocks = [];
        viewGroups = [];
        const area = document.getElementById('report-area');
        const scope = scopeRows();
        let html;
        if (view === 'vehicles') html = renderVehicles(scope);
        else if (!scope.length) html = emptyHtml();
        else if (view === 'month_site') html = renderMonthSite(scope);
        else if (view === 'month_auction') html = renderMonthAuction(scope);
        else if (view === 'year_month') html = renderYearMonth(scope);
        else if (view === 'year_site') html = renderYearSite(scope);
        else if (view === 'year_auction') html = renderYearAuction(scope);
        // Every table first, then all the charts under one heading.
        if (chartBlocks.length) {
            html += `<div class="report-section"><h3 class="section-title">Charts - ${escHtml(periodText())}</h3>`
                + chartBlocks.map(b => `<h4 class="chart-group-title">${escHtml(b.title)}</h4><div class="charts-block mt-2">${b.html}</div>`).join('')
                + '</div>';
        }
        area.innerHTML = html;
        pendingCharts.forEach(buildBarChart);
        pendingCharts = [];
        showFilterState(scope);
        if (view === 'vehicles') {
            const search = document.getElementById('grid-search');
            search.addEventListener('input', () => { gridSearch = search.value; renderVehiclesBodyOnly(); });
            document.getElementById('csv-btn').addEventListener('click', downloadCsv);
            area.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
                const key = th.dataset.sort;
                if (gridSort.key === key) gridSort.dir = -gridSort.dir;
                else gridSort = { key, dir: GRID_COLUMNS.find(c => c.key === key).num ? -1 : 1 };
                renderReport();
            }));
        }
    }

    // Colours each control with a filter on, and says what is filtered and how
    // many sales that leaves.
    function showFilterState(scope) {
        fillFilterLists();
        const mark = (id, on) => document.getElementById(id).classList.toggle('filter-on', on);
        document.querySelector('#site-filter summary').classList.toggle('filter-on', ticked.site.size > 0);
        document.querySelector('#customer-filter summary').classList.toggle('filter-on', ticked.customer.size > 0);
        mark('date-from', !!dateFrom);
        mark('date-to', !!dateTo);
        document.querySelector('#category-filter summary').classList.toggle('filter-on', !!category);
        document.getElementById('filter-banner').style.display = 'none';
    }

    // The tick-lists offer only the sites and customers left in the data by the
    // period (or dates), supplier category and the other tick-list. What is ticked
    // always stays in its list so it can be unticked.
    function fillFilterLists() {
        const base = periodRows().filter(r => !category || r.supplier_category === category);
        const lists = {
            site: distinct(base.filter(r => !ticked.customer.size || ticked.customer.has(customerOf(r))), r => r.branch_name),
            customer: distinct(base.filter(siteTicked), customerOf),
        };
        Object.keys(lists).forEach(kind => {
            const values = [...new Set([...lists[kind].map(String), ...ticked[kind]])]
                .sort((a, b) => a.localeCompare(b, 'en-GB', { numeric: true }));
            const box = document.querySelector(`#${kind}-filter .veh-multi-list`);
            // Rebuilt only when the choices change, so an open list keeps its scroll place.
            const sig = values.join('\u0001');
            if (box.dataset.sig !== sig) {
                box.dataset.sig = sig;
                box.innerHTML = '<a href="#" data-clear="1">Clear</a>' + values.map(v =>
                    `<label><input type="checkbox" value="${escHtml(v)}"${ticked[kind].has(v) ? ' checked' : ''}> ${escHtml(v)}</label>`).join('');
            }
            showTickSummary(kind);
        });
    }

    function showTickSummary(kind) {
        const set = ticked[kind];
        document.querySelector(`#${kind}-filter summary`).textContent =
            !set.size ? 'All' : set.size === 1 ? [...set][0] : `${set.size} Selected`;
    }

    // The Supplier Category dropdown holds one choice; '' is every category.
    function setCategory(value) {
        category = value;
        document.querySelectorAll('#category-filter input[type=radio]').forEach(b => { b.checked = b.value === value; });
        document.querySelector('#category-filter summary').textContent = value || 'All';
    }

    function clearTicks(kind) {
        ticked[kind].clear();
        document.querySelectorAll(`#${kind}-filter input[type=checkbox]`).forEach(b => { b.checked = false; });
        showTickSummary(kind);
    }

    // Typing in the search box re-renders the whole grid but keeps the box
    // focused and the caret where it was.
    function renderVehiclesBodyOnly() {
        const search = document.getElementById('grid-search');
        const pos = search.selectionStart;
        renderReport();
        const again = document.getElementById('grid-search');
        again.focus();
        again.setSelectionRange(pos, pos);
    }

    // ── Period select ────────────────────────────────────────────────────────
    const viewIsYear = v => v.startsWith('year');

    function fillPeriodSelect() {
        const sel = document.getElementById('period-select');
        const label = document.getElementById('period-label');
        const months = monthsAvailable();
        const years = yearsAvailable();
        let options = [];
        if (view === 'vehicles') {
            label.textContent = 'Period';
            options.push('<optgroup label="Months">' + months.slice().reverse().map(m => `<option value="M:${m}">${escHtml(monthLabel(m))}</option>`).join('') + '</optgroup>');
            options.push('<optgroup label="Financial Years">' + years.slice().reverse().map(y => `<option value="Y:${y}">${escHtml(fyLabel(y))}</option>`).join('') + '</optgroup>');
        } else if (viewIsYear(view)) {
            label.textContent = 'Financial Year';
            options = years.slice().reverse().map(y => `<option value="Y:${y}">${escHtml(fyLabel(y))}</option>`);
        } else {
            label.textContent = 'Month';
            options = months.slice().reverse().map(m => `<option value="M:${m}">${escHtml(monthLabel(m))}</option>`);
        }
        // While a date range is on, the period shows blank; picking one clears the range.
        if (rangeOn()) options.unshift('<option value="">(Date Range)</option>');
        sel.innerHTML = options.join('');
        sel.disabled = !options.length;
        // Keep the current choice where the new list still offers it,
        // otherwise the latest period of the right kind.
        const values = Array.from(sel.options).map(o => o.value);
        if (!period || !values.includes(period)) {
            if (view !== 'vehicles' && viewIsYear(view)) period = years.length ? `Y:${years[years.length - 1]}` : null;
            else period = months.length ? `M:${months[months.length - 1]}` : null;
        }
        sel.value = rangeOn() ? '' : (period || '');
    }

    // ── Controls ─────────────────────────────────────────────────────────────
    function setActive(groupId, attr, value) {
        document.querySelectorAll(`#${groupId} button`).forEach(b => b.classList.toggle('active', b.dataset[attr] === value));
    }

    // A date typed or cleared: the period dropdown is greyed while a range is
    // on, as the range is what the page is showing.
    function applyDateRange() {
        dateFrom = document.getElementById('date-from').value;
        dateTo = document.getElementById('date-to').value;
        document.getElementById('date-clear').style.display = rangeOn() ? '' : 'none';
        fillPeriodSelect();
        renderReport();
    }

    // The boxes offer only the dates the data covers.
    function setDateLimits() {
        const days = distinct(rows, r => String(r.invoice_date || '').slice(0, 10)).sort();
        ['date-from', 'date-to'].forEach(id => {
            const box = document.getElementById(id);
            box.min = days[0] || '';
            box.max = days[days.length - 1] || '';
        });
    }

    function bindControls() {
        ['site', 'customer'].forEach(kind => {
            const box = document.getElementById(`${kind}-filter`);
            box.addEventListener('change', e => {
                if (e.target.checked) ticked[kind].add(e.target.value); else ticked[kind].delete(e.target.value);
                showTickSummary(kind);
                renderReport();
            });
            box.addEventListener('click', e => {
                if (!e.target.dataset.clear) return;
                e.preventDefault();
                clearTicks(kind);
                renderReport();
            });
        });
        document.getElementById('filter-clear-all').addEventListener('click', () => {
            clearTicks('site');
            clearTicks('customer');
            setCategory('');
            gridSearch = '';
            document.getElementById('date-from').value = '';
            document.getElementById('date-to').value = '';
            applyDateRange();
        });
        ['date-from', 'date-to'].forEach(id => document.getElementById(id).addEventListener('change', applyDateRange));
        document.getElementById('date-clear').addEventListener('click', () => {
            document.getElementById('date-from').value = '';
            document.getElementById('date-to').value = '';
            applyDateRange();
        });
        document.getElementById('report-area').addEventListener('click', e => {
            const btn = e.target.closest('button[data-view-group]');
            if (!btn) return;
            const group = viewGroups[Number(btn.dataset.viewGroup)];
            if (group) TV.open(group.title, group.rows);
        });
        document.getElementById('margin-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-margin]');
            if (!btn || btn.dataset.margin === marginChart) return;
            marginChart = btn.dataset.margin;
            setActive('margin-toggle', 'margin', marginChart);
            renderReport();
        });
        const chooseView = next => {
            view = next;
            const group = VIEW_GROUPS.find(g => g.subs.some(s => s.view === next));
            if (group) lastSub[group.key] = next;
            const subs = group ? group.subs : [];
            document.getElementById('subview-group').style.display = subs.length ? '' : 'none';
            document.getElementById('subview-toggle').innerHTML = subs.map(s =>
                `<button data-view="${s.view}" class="${s.view === next ? 'active' : ''}">${s.label}</button>`).join('');
            fillPeriodSelect();
            renderReport();
        };
        document.getElementById('view-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-group]');
            if (!btn || btn.classList.contains('active')) return;
            setActive('view-toggle', 'group', btn.dataset.group);
            chooseView(btn.dataset.group === 'vehicles' ? 'vehicles' : lastSub[btn.dataset.group]);
        });
        document.getElementById('subview-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-view]');
            if (btn && btn.dataset.view !== view) chooseView(btn.dataset.view);
        });
        document.getElementById('category-filter').addEventListener('change', e => {
            if (e.target.type !== 'radio') return;
            setCategory(e.target.value);
            document.getElementById('category-filter').open = false;
            renderReport();
        });
        document.getElementById('period-select').addEventListener('change', e => {
            if (!e.target.value) return;
            period = e.target.value;
            // A period replaces any date range.
            document.getElementById('date-from').value = '';
            document.getElementById('date-to').value = '';
            applyDateRange();
        });
    }

    // ── Font size (saved per user) ───────────────────────────────────────────
    const FONT_MIN = 8, FONT_MAX = 20, FONT_DEFAULT = 13;
    let fontSize = (window.TRADE_FONT && window.TRADE_FONT.size) || FONT_DEFAULT;
    let fontSaveTimer = null;
    function applyFont() {
        document.documentElement.style.setProperty('--page-font', fontSize + 'px');
        document.getElementById('font-size-label').textContent = fontSize;
    }
    async function saveFont() {
        try {
            await fetch(BASE_URL + '/api/user-prefs/' + window.TRADE_FONT.prefKey, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(fontSize),
            });
        } catch (e) { /* the size just will not persist this time */ }
    }
    window.changeFont = function (delta) {
        fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, fontSize + delta));
        applyFont();
        clearTimeout(fontSaveTimer);
        fontSaveTimer = setTimeout(saveFont, 500);
    };
    window.resetFont = function () {
        fontSize = FONT_DEFAULT;
        applyFont();
        clearTimeout(fontSaveTimer);
        fontSaveTimer = setTimeout(saveFont, 500);
    };

    // ── Loading ──────────────────────────────────────────────────────────────
    function cacheNote() {
        const el = document.getElementById('cache-note');
        if (!cachedAt) { el.textContent = ''; return; }
        const d = new Date(cachedAt * 1000);
        const hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0');
        el.textContent = `Data as at ${hh}:${mm} ${fmtDate(d.toISOString())} - ${rows.length.toLocaleString('en-GB')} trade sales`;
    }

    async function loadData(refresh = false) {
        const btn = document.getElementById('refresh-btn');
        btn.disabled = true;
        try {
            const resp = await fetch(`${BASE_URL}/reports/trade/data${refresh ? '?refresh=1' : ''}`);
            const data = await resp.json();
            if (data.status !== 'ok') throw new Error(data.message || 'Failed to load');
            const cols = data.columns;
            const changes = data.site_changes || {};
            rows = data.rows.map(arr => {
                const o = {};
                cols.forEach((c, i) => { o[c] = arr[i]; });
                o.site_change = changes[String(o.stock_id)] || null;
                return o;
            });
            // The Settings > Auctions list offers every customer, busiest first.
            window.TRADE_CUSTOMERS = auctionNames(rows);
            const warning = document.getElementById('site-changes-warning');
            warning.textContent = data.site_changes_error || '';
            warning.style.display = data.site_changes_error ? '' : 'none';
            cachedAt = data.cached_at || null;
            cacheNote();
            setDateLimits();
            fillFilterLists();
            fillPeriodSelect();
            renderReport();
            renderSiteChanges();
        } catch (e) {
            document.getElementById('report-area').innerHTML = `<div class="panel shadow-sm"><div class="error-state">Could not load the trade sales: ${escHtml(e.message || e)}</div></div>`;
        } finally {
            btn.disabled = false;
        }
    }
    window.refreshData = function () { loadData(true); };

    // ── Site Changes popup (only there with the Approve Site Changes permission) ──
    const plate = s => String(s || '').replace(/\s+/g, '').toUpperCase();

    function scError(message) {
        const box = document.getElementById('sc-error');
        box.textContent = message || '';
        box.classList.toggle('show', !!message);
    }

    // The green message after a site change is saved or removed.
    function scSuccess(message) {
        const box = document.getElementById('sc-success');
        box.textContent = message || '';
        box.classList.toggle('show', !!message);
    }

    // Every sale with the typed VRM. A VRM with several stock ids must be picked from.
    function renderSiteMatches() {
        const typed = plate(document.getElementById('sc-vrm').value);
        const matches = typed ? rows.filter(r => plate(r.vrm) === typed) : [];
        document.getElementById('sc-matches').innerHTML = !typed ? ''
            : !matches.length ? '<div class="text-muted">No sale with that VRM in the Trade Report.</div>'
            : '<label>Stock ID</label>' + matches.map((r, i) => `
                <label class="sc-match"><input type="radio" name="sc-stock" value="${escHtml(r.stock_id)}"${i === 0 ? ' checked' : ''}>
                    ${escHtml(r.stock_id)} - ${escHtml(r.branch_name || '')} - ${escHtml([r.make, r.model].filter(Boolean).join(' '))} - ${escHtml(fmtDate(r.invoice_date))}</label>`).join('');
    }

    function renderSiteChanges() {
        const holder = document.getElementById('sc-current');
        if (!holder) return;
        const site = document.getElementById('sc-site');
        const chosen = site.value;
        site.innerHTML = '<option value="">(Choose A Site)</option>' + distinct(rows, r => r.branch_name).filter(Boolean).sort()
            .map(n => `<option value="${escHtml(n)}">${escHtml(n)}</option>`).join('');
        site.value = chosen;
        renderSiteMatches();
        const changed = rows.filter(r => r.site_change);
        holder.innerHTML = !changed.length ? '<div class="text-muted">None.</div>'
            : `<table class="sc-current"><thead><tr><th>VRM</th><th>From</th><th>To</th><th>Reason</th><th>By</th><th></th></tr></thead><tbody>${
                changed.map(r => `<tr><td>${escHtml(r.vrm)}</td><td>${escHtml(r.site_change.old_site || '')}</td><td>${escHtml(r.site_change.new_site)}</td>
                    <td>${escHtml(r.site_change.reason)}</td><td>${escHtml(r.site_change.changed_by_name)}</td>
                    <td><button type="button" class="vc-link" data-remove="${r.site_change.id}">Remove</button></td></tr>`).join('')
            }</tbody></table>`;
    }

    async function postSiteChange(path, body) {
        const resp = await fetch(`${BASE_URL}/reports/trade/site-changes${path}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }).then(r => r.json());
        if (resp.status !== 'ok') throw new Error(resp.message || 'Save failed');
    }

    // While a save runs the popup is greyed out and locked and the button says
    // what is happening, so it is clear something is going on.
    async function whileSaving(button, busyText, work) {
        const box = button.closest('.modal-box');
        const text = button.textContent;
        box.classList.add('busy');
        button.disabled = true;
        button.textContent = busyText;
        try { await work(); } finally {
            box.classList.remove('busy');
            button.disabled = false;
            button.textContent = text;
        }
    }

    // The site changes now in force, one line each.
    function downloadSiteChangesCsv() {
        const esc = s => { s = String(s ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
        const lines = [['VRM', 'Stock ID', 'Old Site', 'New Site', 'Reason', 'Changed By', 'Changed At'].join(',')];
        rows.filter(r => r.site_change).forEach(r => {
            const s = r.site_change;
            lines.push([r.vrm, r.stock_id, s.old_site, s.new_site, s.reason, s.changed_by_name, String(s.changed_at || '').slice(0, 16).replace('T', ' ')].map(esc).join(','));
        });
        // Byte order mark first so Excel reads the file as UTF-8.
        const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'trade_site_changes.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
    }

    function bindSiteChanges() {
        const modal = document.getElementById('siteChangesModal');
        if (!modal) return;
        document.getElementById('sc-csv').addEventListener('click', downloadSiteChangesCsv);
        document.getElementById('site-changes-btn').addEventListener('click', () => {
            scError('');
            scSuccess('');
            renderSiteChanges();
            modal.classList.add('open');
        });
        document.getElementById('sc-close').addEventListener('click', () => modal.classList.remove('open'));
        document.getElementById('sc-vrm').addEventListener('input', renderSiteMatches);
        document.getElementById('sc-save').addEventListener('click', async () => {
            scError('');
            scSuccess('');
            const picked = document.querySelector('input[name="sc-stock"]:checked');
            try {
                if (!picked) throw new Error('Type a VRM and pick the vehicle');
                await whileSaving(document.getElementById('sc-save'), 'Saving...', async () => {
                    await postSiteChange('', {
                        stock_id: picked.value,
                        new_site: document.getElementById('sc-site').value,
                        reason: document.getElementById('sc-reason').value,
                    });
                    document.getElementById('sc-vrm').value = '';
                    document.getElementById('sc-site').value = '';
                    document.getElementById('sc-reason').value = '';
                    await loadData(false);
                    VehicleComments.loadCounts();
                });
                scSuccess('Site change saved successfully');
            } catch (err) { scError(err.message); }
        });
        document.getElementById('sc-current').addEventListener('click', async e => {
            const id = e.target.dataset.remove;
            if (!id || !confirm('Remove this site change? The car goes back to its Autoview site.')) return;
            scError('');
            scSuccess('');
            try {
                await whileSaving(e.target, 'Removing...', async () => {
                    await postSiteChange(`/${id}/remove`, {});
                    await loadData(false);
                    VehicleComments.loadCounts();
                });
                scSuccess('Site change removed successfully');
            } catch (err) { scError(err.message); }
        });
    }

    // Saving Settings can change the highlight rules, which colour this grid too.
    document.addEventListener('trade-settings-saved', () => { renderReport(); });

    document.addEventListener('DOMContentLoaded', () => {
        bindControls();
        bindSiteChanges();
        applyFont();
        loadData(false);
    });
})();
