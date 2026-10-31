// Adds the Daily OBEYA Targets workbook's charts to the Intelligence Hub KPI
// page, every one Admins Only (no one else is sent them until you press Show
// To Everyone on the chart, or tick Everyone in Settings > Charts).
//
// add_obeya_charts.js is built from this file by build_add_obeya_charts.py,
// which puts the workbook's targets (cs_daily target columns) in TARGET_RUNS.
// Paste the whole of add_obeya_charts.js into the browser console (F12 >
// Console) on the KPI page, as the admin, in Admin View, after Load Data has
// finished and the SharePoint files have loaded.
//
// DRY_RUN = true (as built) only lists what would happen. Change it to false
// and paste again to add the charts, then reload the page. Running it again
// never adds a chart twice: a chart already on the page (same title, or the
// same metrics and division) is left alone.
//
// Every figure daily_summary holds is read from Snowflake. Only the figures
// it does not hold yet are read from the DSE (Daily Spend and Enquiries)
// SharePoint file: DOC Orders, Staff Numbers, Outbound calls, Price Changes,
// AutoConvert - CS and AutoConvert - CSMF.
(async () => {
    const DRY_RUN = true;

    // ── Built in by build_add_obeya_charts.py ──
    // { 'cs_daily target column header': [[first date, last date, figure], ...] }
    const TARGET_RUNS = {} /*TARGET_RUNS*/;

    // Section for each group: the first name the page already has, else the
    // first name is created. Edit these to use other sections.
    const SECTION_NAMES = {
        upper: ['Upper Funnel'],
        results: ['Results'],
        stock: ['Stock'],
        pricing: ['Pricing'],
        telesales: ['Telesales', 'Results'],
        autotrader: ['Autotrader'],
    };

    // DSE SharePoint columns, by the names the page may have them under (case,
    // spaces and punctuation ignored). Add a name here if a column was renamed
    // in Settings > SharePoint > Column Renames.
    const DSE_COLUMNS = {
        doc: ['DOC Orders'],
        staff: ['Staff Numbers', 'Staffing level'],
        outbound: ['Outbound calls', 'Outgoing calls'],
        price_changes: ['Price Changes'],
        ac_cs: ['AutoConvert - CS', 'AutoConvert CS'],
        ac_csmf: ['AutoConvert - CSMF', 'AutoConvert CSMF'],
    };

    // ── The charts ──
    // top / bottom: the metrics added (or subtracted) on each side of the
    // ratio. S = a daily_summary column, D = a DSE column (optional ones are
    // left out when the page does not have them, and the subtitle says so).
    // agg: weeks and months as Sum or Average. bottomAgg: the bottom's own
    // Sum / Average, where the workbook divides a week's total by its average.
    // as: 'pct' (x 100) or 'dec' for a ratio. target: a cs_daily target column
    // header, { snowflake: true } for the <METRIC>_TARGET column, or { value }.
    const S = (metric, op) => ({ kind: 'sf', metric, op });
    const D = (key, opts = {}) => ({ kind: 'dse', key, op: opts.op, optional: !!opts.optional });
    const WEB3 = () => [S('WEBSITE_ENQUIRE_NOW'), S('WEBSITE_RESERVATIONS'), S('WEBSITE_MOVE_CLOSER')];
    const WEB4 = () => [...WEB3(), S('WEBSITE_RENEWAL')];
    const CHARTS = [
        // Upper Funnel
        { group: 'upper', title: 'Website Sessions', top: [S('WEBSITE_SESSIONS')], agg: 'sum',
          target: { snowflake: true, fallback: 'Website Sessions Target' } },
        { group: 'upper', title: 'Total Website Users', top: [S('WEBSITE_TOTAL_USERS')], agg: 'sum',
          subtitle: 'Weeks and months add up the daily users, as the workbook does', target: 'Total Users target' },
        { group: 'upper', title: 'Website Sessions Per Advertised Vehicle', top: [S('WEBSITE_SESSIONS')], bottom: [S('STOCK_ON_SALE')],
          agg: 'sum', as: 'dec', decimals: 1, target: 'Sessions per vehicle Target' },
        { group: 'upper', title: 'Enquiries (Website & Unique Incoming Calls, Excl. Classifieds)',
          top: [...WEB3(), D('ac_cs', { optional: true }), S('VALUATIONS_PART_EXCHANGE'), S('UNIQUE_INCOMING_CALLS')], agg: 'sum',
          target: 'Digital & Call Enquiries Target' },
        { group: 'upper', title: 'Sessions To Enquiries (Excl. Classifieds) Conversion',
          top: [...WEB3(), D('ac_cs', { optional: true }), S('VALUATIONS_PART_EXCHANGE'), S('UNIQUE_INCOMING_CALLS')],
          bottom: [S('WEBSITE_SESSIONS')], agg: 'sum', as: 'pct', decimals: 2, target: 'Sessions to enquiries conversion % Target' },
        { group: 'upper', title: 'Email Enquiries', top: [...WEB4(), D('ac_cs', { optional: true })], agg: 'sum',
          target: 'Email Enquiries Target' },
        { group: 'upper', title: 'Enquiries (Email & Valuations)',
          top: [...WEB4(), D('ac_cs', { optional: true }), S('VALUATIONS_PART_EXCHANGE')], agg: 'sum',
          target: 'Email & valuation enquiries Target' },
        { group: 'upper', title: 'Sessions To Website Enquiries Conversion', top: [...WEB4(), S('VALUATIONS_PART_EXCHANGE')],
          bottom: [S('WEBSITE_SESSIONS')], agg: 'sum', as: 'pct', decimals: 2, target: 'Website enquiries / sessions target' },
        { group: 'upper', title: 'Total Incoming Calls', top: [S('TOTAL_INCOMING_CALLS')], agg: 'sum',
          target: 'Total Incoming calls target' },
        { group: 'upper', title: 'Unique Incoming Calls To Phone Enquiries Transformation', top: [S('WEBSITE_PHONE')],
          bottom: [S('UNIQUE_INCOMING_CALLS')], agg: 'sum', as: 'pct', target: 'Phone enquiries / incoming calls % target' },
        { group: 'upper', title: 'Classified Email Enquiries',
          top: [S('AT_PORTAL_ENQUIRIES'), S('PORTAL_MOTORS'), S('PORTAL_CARGURUS'), S('PORTAL_CARWOW'), S('PORTAL_HEYCAR'), S('PORTAL_CAZOO')],
          agg: 'sum', subtitle: 'Autotrader, Motors, CarGurus, Carwow, Heycar and Cazoo from daily_summary', target: 'Classified Leads Target2' },
        { group: 'upper', title: 'Part Exchange Valuations', top: [S('VALUATIONS_PART_EXCHANGE')], agg: 'sum', target: 'PX Valuations Target' },

        // Results
        { group: 'results', title: 'Apex Orders', top: [S('ORDERS')], agg: 'sum', target: { snowflake: true, fallback: 'Apex Orders Target' } },
        { group: 'results', title: 'DOC Orders', top: [D('doc')], agg: 'sum', subtitle: 'From the DSE spreadsheet', target: 'DOC Orders Target' },
        { group: 'results', title: 'Orders / Digital Enquiries & Calls', top: [S('ORDERS')],
          bottom: [...WEB3(), D('ac_cs', { optional: true }), S('VALUATIONS_PART_EXCHANGE'), S('UNIQUE_INCOMING_CALLS')],
          agg: 'sum', as: 'pct', target: 'Orders / digital enquiries & calls % target' },
        { group: 'results', title: 'Apex Orders % Of Stock On Sale', top: [S('ORDERS')], bottom: [S('STOCK_ON_SALE')],
          agg: 'sum', bottomAgg: 'avg', as: 'pct', subtitle: 'Weeks and months: orders over the average stock on sale',
          target: 'Orders % advertised vehicles Target' },
        { group: 'results', title: 'Orders Published In The Last 7 Days %', top: [S('ORDERS_PUBLISHED_LAST_7_DAYS')],
          bottom: [S('ORDERS')], agg: 'sum', as: 'pct', target: 'Orders published 7d target' },
        { group: 'results', title: 'Finance Penetration - Total Orders (Excl. 0%)', top: [S('ORDERS_RATE_TOTAL')],
          bottom: [S('ORDERS_RATE_TOTAL'), S('ORDERS_CASH_TOTAL')], agg: 'sum', as: 'pct', target: 'Apex orders total penetration target' },
        { group: 'results', title: 'Finance Penetration - Orders Under £15k (Excl. 0%)', top: [S('ORDERS_RATE_0_15K')],
          bottom: [S('ORDERS_RATE_0_15K'), S('ORDERS_CASH_0_15K')], agg: 'sum', as: 'pct', target: 'Apex orders 0-15k penetration target' },
        { group: 'results', title: 'Finance Penetration - Orders £15k-£25k (Excl. 0%)', top: [S('ORDERS_RATE_15_25K')],
          bottom: [S('ORDERS_RATE_15_25K'), S('ORDERS_CASH_15_25K')], agg: 'sum', as: 'pct', target: 'Apex orders 15-25k penetration target' },
        { group: 'results', title: 'Finance Penetration - Orders £25k+ (Excl. 0%)', top: [S('ORDERS_RATE_25K_PLUS')],
          bottom: [S('ORDERS_RATE_25K_PLUS'), S('ORDERS_CASH_25K_PLUS')], agg: 'sum', as: 'pct', target: 'Apex orders 25k+ penetration target' },

        // Stock
        { group: 'stock', title: 'Vehicles Advertised Online (4am Position)', top: [S('STOCK_ON_SALE')], agg: 'avg',
          target: { snowflake: true, fallback: 'Stock on sale Target' } },
        { group: 'stock', title: 'Total Stock (Excl. Courtesy)', top: [S('TOTAL_STOCK')], agg: 'avg', target: 'Total stock target' },
        { group: 'stock', title: 'Unpublished Stock (Excl. Courtesy)', top: [S('TOTAL_STOCK'), S('STOCK_ON_SALE', '-')], agg: 'avg',
          invert: true, target: 'Unpublished stock target' },
        { group: 'stock', title: 'Average Days In Stock (All Stock Excl. Courtesy)', top: [S('AVG_DAYS_IN_STOCK')], agg: 'avg',
          weight: 'TOTAL_STOCK', decimals: 1, invert: true, target: 'Avg DIS Target' },
        { group: 'stock', title: 'Stock % 45+ Days', top: [S('STOCK_45_PLUS_DAYS')], bottom: [S('TOTAL_STOCK')], agg: 'avg',
          as: 'pct', invert: true, target: '% Stock 45+ days target' },
        { group: 'stock', title: 'Stock % 60+ Days', top: [S('STOCK_60_PLUS_DAYS')], bottom: [S('TOTAL_STOCK')], agg: 'avg',
          as: 'pct', invert: true, target: '% Stock 60+ days target' },
        { group: 'stock', title: 'Freshly Published Adverts', top: [S('PUBLISHED')], agg: 'sum', target: 'Freshly Published Stock Target' },
        { group: 'stock', title: 'B2C Purchases', top: [S('PURCHASES')], agg: 'sum', target: 'B2C Purchases target' },
        { group: 'stock', title: 'EVs Advertised % Of Total Stock Advertised (Rate Only)', top: [S('STOCK_ON_SALE_RATE_EV')],
          bottom: [S('STOCK_ON_SALE_RATE')], agg: 'avg', as: 'pct', target: 'Stock on sale - rate EV % Target' },

        // Pricing
        { group: 'pricing', title: 'Price Changes', top: [D('price_changes')], agg: 'sum', subtitle: 'From the DSE spreadsheet',
          target: 'Price Changes Target' },
        { group: 'pricing', title: 'Prices Changed % Of Average Stock On Sale', top: [D('price_changes')], bottom: [S('STOCK_ON_SALE')],
          agg: 'sum', bottomAgg: 'avg', as: 'pct', subtitle: 'Price changes from the DSE spreadsheet. Weeks and months: changes over the average stock on sale',
          target: 'Price changes / stock on sale Target' },
        { group: 'pricing', title: 'Orders With A Price Change In The Last 7 Days %', top: [S('ORDERS_PRICE_CHANGED_LAST_7_DAYS')],
          bottom: [S('ORDERS')], agg: 'sum', as: 'pct', target: 'Orders repriced % target' },
        { group: 'pricing', title: 'Average Autotrader PTM Of Unsold Stock', top: [S('AVG_AT_PRICE_POSITION_UNSOLD_STOCK_RATE')],
          agg: 'avg', weight: 'STOCK_ON_SALE_RATE', fmt: 'pct100', decimals: 1, target: 'PTM Unsold stock target' },
        { group: 'pricing', title: 'Average Autotrader PTM Of Orders', top: [S('AVG_AT_PRICE_POSITION_ORDERS_RATE')], agg: 'avg',
          fmt: 'pct100', decimals: 1, subtitle: 'Simple daily average', target: 'Orders PTM target' },

        // Telesales
        { group: 'telesales', title: 'Staffing Level (Telesales)', top: [D('staff')], agg: 'avg', decimals: 1,
          subtitle: 'From the DSE spreadsheet', target: 'Staff numbers target' },
        { group: 'telesales', title: 'Outgoing Calls Per Telesales Advisor', top: [D('outbound')], bottom: [D('staff')], agg: 'sum',
          as: 'dec', decimals: 1, subtitle: 'From the DSE spreadsheet', target: 'Outgoing calls per advisor target' },
        { group: 'telesales', title: 'Digital Enquiries Per Telesales Advisor',
          top: [...WEB4(), S('VALUATIONS_PART_EXCHANGE'), D('ac_cs', { optional: true }), D('ac_csmf', { optional: true })],
          bottom: [D('staff')], agg: 'sum', as: 'dec', decimals: 1, subtitle: 'Staff numbers from the DSE spreadsheet',
          target: 'Digital enquiries / advisor target' },

        // Autotrader
        { group: 'autotrader', title: 'Autotrader Total Search Appearances', top: [S('AT_SEARCH_APPEARANCES_TOTAL')], agg: 'sum',
          fmt: 'km', decimals: 1, target: 'Autotrader total search apps target' },
        { group: 'autotrader', title: 'Autotrader Natural Search Appearances', top: [S('AT_SEARCH_APPEARANCES_NATURAL')], agg: 'sum',
          fmt: 'km', decimals: 1, target: 'Autotrader natural search apps target' },
        { group: 'autotrader', title: 'Autotrader PPC Search Appearances', top: [S('AT_SEARCH_APPEARANCES_PPC')], agg: 'sum',
          fmt: 'km', decimals: 1, target: 'Autotrader PPC search apps target' },
        { group: 'autotrader', title: 'Autotrader Total Ad Views', top: [S('AT_ADVERT_VIEWS_TOTAL')], agg: 'sum',
          fmt: 'km', decimals: 1, target: 'Autotrader total ad views target' },
        { group: 'autotrader', title: 'Autotrader Natural Ad Views', top: [S('AT_ADVERT_VIEWS_NATURAL')], agg: 'sum',
          fmt: 'km', decimals: 1, target: 'Autotrader natural ad views target' },
        { group: 'autotrader', title: 'Autotrader PPC Ad Views', top: [S('AT_ADVERT_VIEWS_PPC')], agg: 'sum',
          target: 'Autotrader PPC ad views target' },
        { group: 'autotrader', title: 'Autotrader Total CTR %', top: [S('AT_ADVERT_VIEWS_TOTAL')], bottom: [S('AT_SEARCH_APPEARANCES_TOTAL')],
          agg: 'sum', as: 'pct', decimals: 2, target: 'Autotrader total CTR target' },
        { group: 'autotrader', title: 'Autotrader Natural CTR %', top: [S('AT_ADVERT_VIEWS_NATURAL')], bottom: [S('AT_SEARCH_APPEARANCES_NATURAL')],
          agg: 'sum', as: 'pct', decimals: 2, target: 'Autotrader natural CTR target' },
        { group: 'autotrader', title: 'Autotrader PPC CTR %', top: [S('AT_ADVERT_VIEWS_PPC')], bottom: [S('AT_SEARCH_APPEARANCES_PPC')],
          agg: 'sum', as: 'pct', decimals: 2, target: { value: 0.015 } },
        { group: 'autotrader', title: 'Autotrader Emails', top: [S('AT_PORTAL_ENQUIRIES')], agg: 'sum', target: 'AT Emails target' },
        { group: 'autotrader', title: 'Autotrader Calls', top: [S('MEDIAHAWK_CALLS_AUTOTRADER')], agg: 'sum', target: 'AT Calls target' },
        { group: 'autotrader', title: 'Autotrader Ad Views To Emails/Calls Transformation %',
          top: [S('AT_PORTAL_ENQUIRIES'), S('MEDIAHAWK_CALLS_AUTOTRADER')], bottom: [S('AT_ADVERT_VIEWS_TOTAL')],
          agg: 'sum', as: 'pct', decimals: 2, target: 'AT Emails & Calls / Ad views target' },
        { group: 'autotrader', title: 'Autotrader Emails & Calls / Advertised Vehicles %',
          top: [S('AT_PORTAL_ENQUIRIES'), S('MEDIAHAWK_CALLS_AUTOTRADER')], bottom: [S('STOCK_ON_SALE')],
          agg: 'sum', as: 'pct', target: 'AT Emails & Calls / advertised Target' },
        { group: 'autotrader', title: 'Autotrader Email Enquiries To Orders Transformation %', top: [S('AT_PORTAL_ORDERS_FROM_ENQUIRIES')],
          bottom: [S('AT_PORTAL_ENQUIRIES')], agg: 'sum', as: 'pct', target: 'AT Email Orders / AT Emails Target' },
        { group: 'autotrader', title: 'Autotrader Call Enquiries Created', top: [S('MEDIAHAWK_CALLS_AUTOTRADER_ENQUIRIES')], agg: 'sum',
          target: 'AT Calls - Enquiries Target' },
        { group: 'autotrader', title: 'Autotrader Calls To Enquiries Transformation', top: [S('MEDIAHAWK_CALLS_AUTOTRADER_ENQUIRIES')],
          bottom: [S('MEDIAHAWK_CALLS_AUTOTRADER')], agg: 'sum', as: 'pct', target: 'AT Call Enquiries / Calls Target' },
        { group: 'autotrader', title: 'Autotrader Call Orders', top: [S('MEDIAHAWK_CALLS_AUTOTRADER_ORDERS')], agg: 'sum',
          target: 'AT Call Orders Target' },
        { group: 'autotrader', title: 'Autotrader Call Enquiries To Orders Transformation', top: [S('MEDIAHAWK_CALLS_AUTOTRADER_ORDERS')],
          bottom: [S('MEDIAHAWK_CALLS_AUTOTRADER_ENQUIRIES')], agg: 'sum', as: 'pct', target: 'AT Call Orders / Enquiries Target' },
        { group: 'autotrader', title: 'Autotrader Calls To Orders Transformation', top: [S('MEDIAHAWK_CALLS_AUTOTRADER_ORDERS')],
          bottom: [S('MEDIAHAWK_CALLS_AUTOTRADER')], agg: 'sum', as: 'pct', target: 'AT Call Orders / Calls Target' },
    ];

    // ── Checks before anything is read ──
    if (typeof IS_ADMIN === 'undefined' || typeof layout === 'undefined' || typeof saveLayout !== 'function') {
        console.error('Run this on the KPI page (Intelligence Hub > KPI).');
        return;
    }
    if (!IS_ADMIN || (typeof WBR !== 'undefined' && WBR)) { console.error('Run this on the KPI page as the admin.'); return; }
    if (!layoutLoaded || layoutLoadFailed) { console.error('The page has not loaded its charts - reload it and try again.'); return; }
    if (!Object.keys(TARGET_RUNS).length) console.warn('No workbook targets in this file: run build_add_obeya_charts.py and paste add_obeya_charts.js, not the template.');

    const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

    // ── Columns on the page ──
    const spColumns = metricsCatalogue.filter(m => m.source === 'sharepoint').map(m => m.column);
    if (!spColumns.length) console.warn('No SharePoint columns on the page yet: press Load Data and wait for the SharePoint files, or the DSE charts are skipped.');
    const dseColumn = key => {
        const wanted = new Set(DSE_COLUMNS[key].map(norm));
        return spColumns.find(c => wanted.has(norm(c))) || null;
    };
    const hasSnowflake = metric => !!metricsByKey[metricKey('snowflake', metric)];

    // One side of a chart as the page stores it, or { missing } naming what is not there.
    function resolveSide(parts, notes) {
        const out = [];
        for (const p of parts) {
            const ref = p.kind === 'sf'
                ? (hasSnowflake(p.metric) ? { source: 'snowflake', metric: p.metric } : null)
                : (dseColumn(p.key) ? { source: 'sharepoint', metric: dseColumn(p.key) } : null);
            if (!ref) {
                const name = p.kind === 'sf' ? `daily_summary ${p.metric}` : `DSE "${DSE_COLUMNS[p.key][0]}"`;
                if (p.optional) { notes.push(`${DSE_COLUMNS[p.key][0]} not on the page, left out`); continue; }
                return { missing: name };
            }
            out.push({ ...ref, op: p.op });
        }
        return { parts: out };
    }

    // "a + b - c / d + e", names only, for spotting a chart already on the page.
    const sideKey = list => list.map(p => (p.op === '-' ? '-' : '') + norm(p.metric)).sort().join('+');
    const rowKey = r => sideKey([{ metric: r.metric }, ...(r.metric_plus || [])])
        + '/' + (r.divide_by ? sideKey([r.divide_by, ...(r.divide_plus || [])]) : '');
    const existingTitles = new Map(layout.rows.map(r => [norm(rowTitle(r)), r]));
    const existingKeys = new Map(layout.rows.map(r => [rowKey(r), r]));

    // ── Sections ──
    const sectionFor = (group, sources) => {
        const names = SECTION_NAMES[group];
        const name = names.find(n => SECTIONS.includes(n)) || names[0];
        const lock = sectionSources[name];
        // A section locked to one data source only takes charts that read nothing else.
        if (lock && [...sources].some(s => s !== lock)) return `${name} (Obeya)`;
        return name;
    };

    // ── Targets ──
    const runsFor = header => (TARGET_RUNS[header] || []).filter(r => r[2] !== null && r[2] !== undefined);
    const expandRuns = (runs, scale) => {
        const rows = [];
        runs.forEach(([first, last, value]) => {
            for (let d = first; d <= last; d = addIsoDays(d, 1)) rows.push({ date: d, value: Math.round(value * scale * 1e8) / 1e8 });
        });
        return rows;
    };
    // { mode, fixed?, dates?, from } for the chart, before any stored target is looked at.
    function plannedTarget(spec, row) {
        const scale = row.divide_by && row.ratio_as === 'pct' ? 100 : 1;
        const t = spec.target;
        if (t && t.snowflake) {
            if ((metricsByKey[metricKey('snowflake', row.metric)] || {}).has_target) return { mode: 'snowflake', from: `${row.metric}_TARGET` };
            return plannedTarget({ ...spec, target: t.fallback }, row);
        }
        if (t && typeof t === 'object' && 'value' in t) return { mode: 'fixed', fixed: t.value * scale, from: 'fixed, as in the workbook' };
        const runs = runsFor(t);
        if (!runs.length) return { mode: 'none', from: 'none in the workbook' };
        if (runs.length === 1) return { mode: 'fixed', fixed: Math.round(runs[0][2] * scale * 1e8) / 1e8, from: t };
        return { mode: 'import', dates: expandRuns(runs, scale), from: t };
    }
    const targetText = p => p.mode === 'snowflake' ? `Snowflake ${p.from}`
        : p.mode === 'fixed' ? `Fixed ${+p.fixed.toFixed(4)}` + (p.keep ? ' (yours kept)' : '')
        : p.mode === 'import' ? (p.keep ? 'Per date (yours kept)' : `Per date, ${p.dates.length} days`) : 'None';

    // ── Plan every chart ──
    const plan = [];
    for (const spec of CHARTS) {
        const notes = [];
        const top = resolveSide(spec.top, notes);
        const bottom = spec.bottom ? resolveSide(spec.bottom, notes) : { parts: [] };
        const missing = top.missing || bottom.missing;
        const item = { Chart: spec.title, Section: '', Action: '', Reads: '', Target: '' };
        plan.push(item);
        if (missing) { item.Action = `skip - not found: ${missing}`; continue; }
        if (spec.weight && !hasSnowflake(spec.weight)) { item.Action = `skip - not found: daily_summary ${spec.weight}`; continue; }
        const sources = new Set([...top.parts, ...bottom.parts].map(p => p.source));
        item.Reads = [...sources].map(s => (s === 'snowflake' ? 'daily_summary' : 'DSE')).join(' + ');
        const [first, ...plus] = top.parts;
        const pct = !!spec.bottom && spec.as !== 'dec';
        const row = {
            id: newChartId(), type: 'custom', section: sectionFor(spec.group, sources),
            source: first.source, metric: first.metric, title: spec.title.slice(0, 100),
            subtitle: [spec.subtitle, ...notes].filter(Boolean).join('. ').slice(0, 200),
            agg: spec.agg,
            fmt: spec.fmt || (pct ? 'pct' : 'num'),
            decimals: spec.decimals ?? (pct ? 1 : spec.bottom ? 2 : 0),
            admin_only: true,
        };
        if (spec.invert) row.invert = true;
        const asPart = p => ({ source: p.source, metric: p.metric, ...(p.op && p.op !== '+' ? { op: p.op } : {}) });
        if (plus.length) row.metric_plus = plus.map(asPart);
        if (spec.bottom) {
            row.divide_by = { source: bottom.parts[0].source, metric: bottom.parts[0].metric };
            if (bottom.parts.length > 1) row.divide_plus = bottom.parts.slice(1).map(asPart);
            row.ratio_as = pct ? 'pct' : 'dec';
            if (spec.bottomAgg) row.divide_agg = spec.bottomAgg;
        }
        if (spec.weight) row.weight_by = spec.weight;
        item.Section = row.section;
        const already = existingTitles.get(norm(row.title)) || existingKeys.get(rowKey(row));
        if (already) {
            const reads = [already, ...(already.metric_plus || []), already.divide_by, ...(already.divide_plus || [])]
                .filter(Boolean).map(p => p.source || 'snowflake');
            item.Action = `already there: ${rowTitle(already)}` + (reads.includes('sharepoint') ? ' (reads SharePoint)' : '');
            item.Section = already.section;
            continue;
        }
        item.Action = 'ADD';
        item.row = row;
        item.target = plannedTarget(spec, row);
        item.Target = targetText(item.target);
    }

    // A single-metric chart's fixed and per-date targets belong to the metric
    // (every chart on it shares them), so any already stored are kept.
    const own = r => !!r.divide_by || !!(r.metric_plus || []).length;
    for (const item of plan.filter(i => i.row && !own(i.row) && ['fixed', 'import'].includes(i.target.mode))) {
        const r = item.row;
        const resp = await fetch(`${BASE_URL}/kpi/metric-target?${new URLSearchParams({ source: r.source, metric: r.metric })}`).then(x => x.json());
        if (resp.status !== 'ok') { item.Target += ` (could not check stored targets: ${resp.message})`; continue; }
        if (resp.imported) item.target = { mode: 'import', keep: true };
        else if (resp.fixed !== null && resp.fixed !== undefined) item.target = { mode: 'fixed', fixed: resp.fixed, keep: true };
        item.Target = targetText(item.target);
    }

    const adding = plan.filter(i => i.row);
    console.table(plan.map(({ Chart, Section, Action, Reads, Target }) => ({ Chart, Section, Action, Reads, Target })));
    const newSections = [...new Set(adding.map(i => i.row.section))].filter(s => !SECTIONS.includes(s));
    const unhide = [...new Set(adding.map(i => i.row.section))].filter(s => hiddenSections.has(s));
    console.log(`${adding.length} to add, ${plan.filter(i => i.Action.startsWith('already')).length} already there, `
        + `${plan.filter(i => i.Action.startsWith('skip')).length} skipped.`
        + (newSections.length ? ` New sections: ${newSections.join(', ')}.` : '')
        + (unhide.length ? ` Sections to unhide: ${unhide.join(', ')}.` : ''));
    if (DRY_RUN) { console.log('Dry run: nothing was changed. Set DRY_RUN = false at the top and paste again to add them.'); return; }
    if (!adding.length) { console.log('Nothing to add.'); return; }

    // ── Apply ──
    // The saved layout is replaced in one go, so start from the server's
    // copy, not the page's, and keep a copy of it first.
    const fresh = await fetch(`${BASE_URL}/kpi/layout`).then(x => x.json());
    if (fresh.status !== 'ok') { console.error('Could not read the saved layout: ' + fresh.message); return; }
    const ids = rows => rows.map(r => r.id).sort().join(',');
    if (ids(fresh.layout.rows) !== ids(layout.rows)) { console.error('The saved charts have changed since this page loaded - reload the page and run it again.'); return; }
    const backup = new Blob([JSON.stringify(fresh.layout, null, 1)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(backup);
    link.download = `kpi_layout_backup_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.json`;
    link.click();
    console.log('Backup of the saved layout downloaded: ' + link.download);

    let baseRows = fresh.layout.rows;
    const needSections = new Set(adding.map(i => i.row.section));
    if ([...needSections].some(s => !fresh.sections.includes(s) || (fresh.hidden_sections || []).includes(s))) {
        const companies = fresh.section_companies || {};
        const locks = fresh.section_sources || {};
        const hidden = new Set(fresh.hidden_sections || []);
        const items = fresh.sections.map(s => ({
            name: s, from: s, hidden: hidden.has(s) && !needSections.has(s),
            source: locks[s] || null, company: companies[s] || 'carsupermarket',
        })).concat([...needSections].filter(s => !fresh.sections.includes(s))
            .map(s => ({ name: s, from: null, hidden: false, source: null, company: 'carsupermarket' })));
        const resp = await fetch(`${BASE_URL}/kpi/sections`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sections: items }),
        }).then(x => x.json());
        if (resp.status !== 'ok') { console.error('Sections save failed: ' + resp.message); return; }
        baseRows = resp.layout.rows;
        console.log('Sections saved: ' + resp.sections.join(', '));
    }

    // Targets first: a chart's own per-date list is stored under its id,
    // and a target that fails leaves that chart without one rather than
    // stopping the rest.
    for (const item of adding) {
        const r = item.row, t = item.target;
        try {
            if (t.mode === 'snowflake') r.target_mode = 'snowflake';
            else if (t.mode === 'fixed') {
                if (own(r)) r.ratio_target = t.fixed;
                else if (!t.keep) await saveMetricFixed(r.source, r.metric, t.fixed);
                r.target_mode = 'fixed';
            } else if (t.mode === 'import') {
                if (!t.keep) await saveTargetDates(own(r) ? { row_id: r.id } : { source: r.source, metric: r.metric }, t.dates);
                r.target_mode = 'import';
            } else {
                r.target_mode = 'none';
            }
        } catch (err) {
            r.target_mode = 'none';
            console.warn(`${r.title}: target not saved - ${err.message}`);
        }
    }

    let rows = baseRows;
    adding.forEach(i => { rows = placeRow(rows, i.row, i.row.section, Number.MAX_SAFE_INTEGER); });
    try {
        await saveLayout(rows);
    } catch (err) {
        console.error('Saving the charts failed, nothing was added: ' + err.message);
        return;
    }
    console.log(`${adding.length} charts added, all Admins Only. Reload the page to see them.`);
})();
