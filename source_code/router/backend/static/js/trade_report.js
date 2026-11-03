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
    let view = 'month_site';
    let category = '';             // '' = every supplier category
    let period = null;             // 'M:yyyy-MM-dd' or 'Y:yyyy'
    // Date range on the invoice date, 'yyyy-MM-dd' or ''. While either is set
    // it replaces the period, on every view.
    let dateFrom = '';
    let dateTo = '';
    let show = 'both';
    let charts = [];               // live Chart instances; destroyed on re-render
    let gridSort = { key: 'invoice_date', dir: -1 };
    let gridSearch = '';

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
        { key: 'purchase', label: 'Purchase Price',       fmt: fmtMoney },
        { key: 'sale',     label: 'Sale Price',           fmt: fmtMoney },
        { key: 'gross',    label: 'Gross Margin',         fmt: fmtMoney },
        { key: 'cap',      label: 'CAP Clean',            fmt: fmtMoney },
        { key: 'purchPct', label: 'Purchase % CAP Clean', fmt: fmtPct },
        { key: 'sellPct',  label: 'Sell % CAP Clean',     fmt: fmtPct },
        { key: 'costs',    label: 'Total Costs',          fmt: fmtMoney },
        { key: 'net',      label: 'Net Margin',           fmt: fmtMoney },
        { key: 'total',    label: 'Total Margin',         fmt: fmtMoney, emphasis: true },
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
    function siteNames() {
        return distinct(rows.filter(r => r.branch_category === SITE), r => r.branch_name).sort();
    }
    function bcbPrepNames() {
        const names = distinct(rows.filter(r => r.branch_category === 'BCB' || r.branch_category === 'Prep'), r => r.branch_name);
        return names.sort((a, b) => (a === 'BCB' ? -1 : b === 'BCB' ? 1 : a.localeCompare(b)));
    }
    // Auctions in the rows given, busiest first.
    function auctionNames(list) {
        const counts = {};
        list.forEach(r => { const a = r.auction || 'Unknown'; counts[a] = (counts[a] || 0) + 1; });
        return Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    }
    function monthsAvailable() {
        return distinct(rows, r => r.month).sort();
    }
    function yearsAvailable() {
        return distinct(rows, r => r.financial_year).map(Number).sort((a, b) => a - b);
    }

    const rangeOn = () => !!(dateFrom || dateTo);

    // Rows for the chosen date range or period, and supplier category.
    function scopeRows() {
        let list;
        if (rangeOn()) {
            // Dates are 'yyyy-MM-dd', so text order is date order.
            list = rows.filter(r => {
                const day = String(r.invoice_date || '').slice(0, 10);
                return day && (!dateFrom || day >= dateFrom) && (!dateTo || day <= dateTo);
            });
        } else {
            if (!period) return [];
            const [kind, value] = [period.slice(0, 1), period.slice(2)];
            list = kind === 'M' ? rows.filter(r => r.month === value) : rows.filter(r => String(r.financial_year) === value);
        }
        if (category) list = list.filter(r => r.supplier_category === category);
        return list;
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
        const cards = metricKeys.map(key => {
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
    function sectionHtml(title, note, tableHtml, chartsBlock = '') {
        return `
        <div class="report-section">
            <h3 class="section-title">${escHtml(title)}${note ? `<span class="section-note">${escHtml(note)}</span>` : ''}</h3>
            ${tableHtml ? `<div class="tables-block"><div class="panel shadow-sm">${tableHtml}</div></div>` : ''}
            ${chartsBlock ? `<div class="charts-block mt-2">${chartsBlock}</div>` : ''}
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
                chartsHtml(cols, YEAR_CHARTS, name, 2));
        });
        return html;
    }

    // Year By Auction: the "YTD by auction" asked for in the workbook's
    // comments - every auction's year across the top, then a block each.
    function renderYearAuction(scope) {
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
            return `<th class="${c.num ? 'num' : ''}" data-sort="${c.key}">${escHtml(c.label)}${caret}</th>`;
        }).join('');
        const body = list.map(r => '<tr>' + GRID_COLUMNS.map(c => {
            const v = r[c.key];
            const text = c.fmt ? c.fmt(v) : (v === null || v === undefined ? '' : String(v));
            const neg = c.num && num(v) !== null && num(v) < 0 ? ' neg' : '';
            return `<td class="${c.num ? 'num' : ''}${c.cls ? ' ' + c.cls : ''}${neg}">${escHtml(text)}</td>`;
        }).join('') + '</tr>').join('');
        return `
        <div class="report-section">
            <h3 class="section-title">Vehicles - ${escHtml(periodText())}<span class="section-note">${escHtml(category || 'All supplier categories')} - ${list.length.toLocaleString('en-GB')} vehicles</span></h3>
            <div class="panel shadow-sm" style="width: 100%;">
                <div class="grid-toolbar">
                    <input type="search" class="grid-search" id="grid-search" placeholder="Search VRM, stock ID, supplier, site, customer, make or model" value="${escHtml(gridSearch)}" autocomplete="off" spellcheck="false">
                    <button class="btn btn-csv" id="csv-btn"><i class="fas fa-download"></i> CSV</button>
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
        a.download = `trade_sales_${periodText().replace(/[^A-Za-z0-9]+/g, '_')}.csv`;
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
        area.innerHTML = html;
        pendingCharts.forEach(buildBarChart);
        pendingCharts = [];
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
        sel.innerHTML = options.join('');
        sel.disabled = !options.length || rangeOn();
        // Keep the current choice where the new list still offers it,
        // otherwise the latest period of the right kind.
        const values = Array.from(sel.options).map(o => o.value);
        if (!period || !values.includes(period)) {
            if (view !== 'vehicles' && viewIsYear(view)) period = years.length ? `Y:${years[years.length - 1]}` : null;
            else period = months.length ? `M:${months[months.length - 1]}` : null;
        }
        if (period) sel.value = period;
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
        document.getElementById('view-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-view]');
            if (!btn || btn.dataset.view === view) return;
            view = btn.dataset.view;
            setActive('view-toggle', 'view', view);
            fillPeriodSelect();
            renderReport();
        });
        document.getElementById('category-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-category]');
            if (!btn) return;
            category = btn.dataset.category;
            setActive('category-toggle', 'category', category);
            renderReport();
        });
        document.getElementById('show-toggle').addEventListener('click', e => {
            const btn = e.target.closest('button[data-show]');
            if (!btn) return;
            show = btn.dataset.show;
            setActive('show-toggle', 'show', show);
            document.body.classList.toggle('show-tables', show === 'tables');
            document.body.classList.toggle('show-charts', show === 'charts');
            // Charts drawn while hidden have no size; redraw them.
            if (show !== 'tables') renderReport();
        });
        document.getElementById('period-select').addEventListener('change', e => {
            period = e.target.value;
            renderReport();
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
            rows = data.rows.map(arr => {
                const o = {};
                cols.forEach((c, i) => { o[c] = arr[i]; });
                return o;
            });
            cachedAt = data.cached_at || null;
            cacheNote();
            setDateLimits();
            fillPeriodSelect();
            renderReport();
        } catch (e) {
            document.getElementById('report-area').innerHTML = `<div class="panel shadow-sm"><div class="error-state">Could not load the trade sales: ${escHtml(e.message || e)}</div></div>`;
        } finally {
            btn.disabled = false;
        }
    }
    window.refreshData = function () { loadData(true); };

    document.addEventListener('DOMContentLoaded', () => {
        bindControls();
        applyFont();
        loadData(false);
    });
})();
