// Self-Analytics: the parts that do not touch the page, so they can be run
// with node as well. buildSeries turns a query's rows and one chart's options
// into labels and series; describe turns a plain request ("average purchase
// price by month, line chart") into chart options. No external AI: describe
// matches words against the column names and the Also Called words on the
// Semantic & Context Layer page.
(function (root) {
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const UPPER = new Set(['vat', 'rrp', 'gpu', 'vrm', 'id', 'at', 'cap', 'ms', 'b2b', 'b2c', 'b2b2c', 'dis', 'dts', 'kpi', 'crm', 'uk']);

    // ── Labels ──────────────────────────────────────────────────────────────
    function columnLabel(name) {
        return String(name || '').toLowerCase().split(/[_\s]+/).filter(Boolean)
            .map(w => UPPER.has(w) ? w.toUpperCase() : w === 'pct' ? '%' : w[0].toUpperCase() + w.slice(1)).join(' ');
    }

    const AGG_LABELS = {sum: 'Total', avg: 'Average', count: 'Count', count_distinct: 'Distinct Count',
                        min: 'Lowest', max: 'Highest', median: 'Median'};

    function measureLabel(m) {
        if (m.label) return m.label;
        if (m.agg === 'count' && !m.column) return 'Rows';
        let text = `${AGG_LABELS[m.agg] || m.agg} ${columnLabel(m.column)}`;
        if (m.denominator) text += ` / Total ${columnLabel(m.denominator)} (%)`;
        if (m.minus) text += ` Minus ${columnLabel(m.minus)}`;
        if (m.minus && m.as_pct) text += ' (%)';
        return text;
    }

    // ── Dates ───────────────────────────────────────────────────────────────
    function parseDate(v) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v || ''));
        return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
    }

    const iso = d => d.toISOString().slice(0, 10);

    // [sortable key, label] for a value grouped by day, week, month, quarter or year.
    function bucket(v, grain) {
        if (v === null || v === undefined || v === '') return ['', '(Blank)'];
        if (!grain || grain === 'none') return [String(v), String(v)];
        const d = parseDate(v);
        if (!d) return [String(v), String(v)];
        const y = d.getUTCFullYear(), mo = d.getUTCMonth();
        if (grain === 'day') return [iso(d), `${String(d.getUTCDate()).padStart(2, '0')} ${MONTHS[mo]} ${y}`];
        if (grain === 'week') {
            const monday = new Date(d.getTime() - ((d.getUTCDay() + 6) % 7) * 86400000);
            return [iso(monday), `W/C ${String(monday.getUTCDate()).padStart(2, '0')} ${MONTHS[monday.getUTCMonth()]} ${monday.getUTCFullYear()}`];
        }
        if (grain === 'month') return [`${y}-${String(mo + 1).padStart(2, '0')}`, `${MONTHS[mo]} ${y}`];
        if (grain === 'quarter') return [`${y}-Q${Math.floor(mo / 3) + 1}`, `Q${Math.floor(mo / 3) + 1} ${y}`];
        return [String(y), String(y)];
    }

    // ── Filters and calculations ────────────────────────────────────────────
    const num = v => (v === null || v === undefined || (typeof v === 'string' && !v.trim()) || typeof v === 'boolean') ? null
        : (Number.isFinite(Number(v)) ? Number(v) : null);

    function passes(row, filters, idx) {
        return filters.every(f => {
            const i = idx(f.column);
            if (i < 0) return true;
            const v = row[i];
            const text = v === null || v === undefined ? '' : String(v);
            const want = String(f.value ?? '');
            switch (f.op) {
                case 'is_empty': return text === '';
                case 'not_empty': return text !== '';
                case 'contains': return text.toLowerCase().includes(want.toLowerCase());
                case 'not_contains': return !text.toLowerCase().includes(want.toLowerCase());
            }
            const a = num(v), b = num(want);
            const both = a !== null && b !== null && want.trim() !== '';
            const cmp = both ? a - b : text.toLowerCase().localeCompare(want.toLowerCase());
            switch (f.op) {
                case '=': return both ? a === b : text.toLowerCase() === want.toLowerCase();
                case '!=': return both ? a !== b : text.toLowerCase() !== want.toLowerCase();
                case '>': return cmp > 0;
                case '>=': return cmp >= 0;
                case '<': return cmp < 0;
                case '<=': return cmp <= 0;
            }
            return true;
        });
    }

    function aggregate(values, agg) {
        if (agg === 'count') return values.filter(v => v !== null && v !== undefined && v !== '').length;
        if (agg === 'count_distinct') return new Set(values.filter(v => v !== null && v !== undefined && v !== '').map(String)).size;
        const nums = values.map(num).filter(v => v !== null);
        if (!nums.length) return null;
        if (agg === 'sum') return nums.reduce((a, b) => a + b, 0);
        if (agg === 'avg') return nums.reduce((a, b) => a + b, 0) / nums.length;
        if (agg === 'min') return nums.reduce((a, b) => Math.min(a, b));
        if (agg === 'max') return nums.reduce((a, b) => Math.max(a, b));
        if (agg === 'median') {
            const s = nums.slice().sort((a, b) => a - b), m = Math.floor(s.length / 2);
            return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
        }
        return null;
    }

    function measureValue(rows, m, idx) {
        const values = col => { const i = idx(col); return i < 0 ? [] : rows.map(r => r[i]); };
        if (m.agg === 'count' && !m.column) return rows.length;
        const a = aggregate(values(m.column), m.agg);
        if (m.denominator) {
            const b = aggregate(values(m.denominator), 'sum');
            return a === null || b === null || b === 0 ? null : a / b * 100;
        }
        if (!m.minus) return a;
        const b = aggregate(values(m.minus), m.agg);
        if (a === null || b === null) return null;
        if (m.as_pct) return b === 0 ? null : (a - b) / Math.abs(b) * 100;
        return a - b;
    }

    // ── One chart's data ────────────────────────────────────────────────────
    const MAX_SERIES = 25;

    function cohortRows(chart, columns, rows) {
        const idx = name => columns.findIndex(c => c.name.toLowerCase() === String(name).toLowerCase());
        const needed = [chart.x, chart.split].concat(chart.measures.flatMap(m => [m.column, m.minus, m.denominator]),
            (chart.filters || []).map(f => f.column)).filter(Boolean);
        if (needed.some(c => idx(c) < 0)) return [];
        const complete = chart.measures.flatMap(m => [m.column, m.minus, m.denominator].filter(Boolean)
            .map(column => ({column, numeric: !['count', 'count_distinct'].includes(m.agg)})));
        return rows.filter(r => passes(r, chart.filters || [], idx) && (chart.cohort !== 'complete' ||
            complete.every(c => c.numeric ? num(r[idx(c.column)]) !== null :
                r[idx(c.column)] !== null && r[idx(c.column)] !== undefined && r[idx(c.column)] !== '')));
    }

    function drillRows(chart, columns, rows, key, splitValue) {
        const idx = name => columns.findIndex(c => c.name.toLowerCase() === String(name).toLowerCase());
        const xi = idx(chart.x), si = idx(chart.split);
        return cohortRows(chart, columns, rows).filter(r =>
            (xi < 0 || bucket(r[xi], chart.grain)[0] === key) &&
            (si < 0 || (r[si] == null ? '(Blank)' : String(r[si])) === splitValue));
    }

    function buildSeries(chart, columns, rows) {
        const names = columns.map(c => c.name.toLowerCase());
        const idx = name => name ? names.indexOf(String(name).toLowerCase()) : -1;
        const missing = [chart.x, chart.split].concat(chart.measures.flatMap(m => [m.column, m.minus, m.denominator]))
            .concat(chart.filters.map(f => f.column)).filter(c => c && idx(c) < 0);
        const notes = missing.length ? [`Not in the query's columns: ${[...new Set(missing)].join(', ')}`] : [];
        const kept = cohortRows(chart, columns, rows);
        if (chart.cohort === 'complete') notes.push(`${kept.length.toLocaleString()} Matched Rows; Missing Measures Excluded.`);
        const measures = chart.measures.length ? chart.measures : [{column: null, agg: 'count'}];

        if (chart.type === 'scatter') {
            const xi = idx(chart.x), m = measures[0], yi = idx(m.column);
            const points = xi < 0 || yi < 0 ? [] : kept.map(r => ({x: num(r[xi]), y: num(r[yi])}))
                .filter(p => p.x !== null && p.y !== null);
            return {labels: [], series: [{label: columnLabel(m.column), data: points, measure: m}], notes, rows: kept.length};
        }

        // Group rows by the X value (bucketed for dates).
        const xi = idx(chart.x);
        const groups = new Map();
        kept.forEach(r => {
            const [key, label] = xi < 0 ? ['', 'All'] : bucket(r[xi], chart.grain);
            if (!groups.has(key)) groups.set(key, {label, rows: []});
            groups.get(key).rows.push(r);
        });
        let keys = [...groups.keys()];

        // Series: each value, and each split value when there is a split.
        const si = idx(chart.split);
        let splitValues = [null];
        if (si >= 0) {
            const totals = new Map();
            kept.forEach(r => { const v = r[si] === null || r[si] === undefined ? '(Blank)' : String(r[si]); totals.set(v, (totals.get(v) || 0) + 1); });
            splitValues = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(e => e[0]);
            if (splitValues.length > MAX_SERIES) {
                notes.push(`Only the ${MAX_SERIES} largest of ${splitValues.length} ${columnLabel(chart.split)} values are shown.`);
                splitValues = splitValues.slice(0, MAX_SERIES);
            }
            splitValues.sort();
        }
        const series = [];
        measures.forEach(m => splitValues.forEach(sv => {
            const counts = [];
            const data = keys.map(k => {
                let groupRows = groups.get(k).rows;
                if (sv !== null) groupRows = groupRows.filter(r => (r[si] === null || r[si] === undefined ? '(Blank)' : String(r[si])) === sv);
                counts.push(!m.column ? groupRows.length : groupRows.filter(r => ['count', 'count_distinct'].includes(m.agg) ?
                    r[idx(m.column)] !== null && r[idx(m.column)] !== undefined && r[idx(m.column)] !== '' :
                    num(r[idx(m.column)]) !== null).length);
                return groupRows.length ? measureValue(groupRows, m, idx) : null;
            });
            const label = sv === null ? measureLabel(m) : (measures.length > 1 ? `${measureLabel(m)} - ${sv}` : sv);
            series.push({label, data, measure: m, splitValue: sv, counts});
        }));

        // Order and top N, by the first series.
        const firstValue = k => { const v = series[0] ? series[0].data[keys.indexOf(k)] : null; return v === null ? -Infinity : v; };
        const byX = (a, b) => {
            const na = num(a), nb = num(b);
            return na !== null && nb !== null ? na - nb : String(a).localeCompare(String(b));
        };
        let order = keys.slice();
        if (chart.top_n > 0) order = order.sort((a, b) => firstValue(b) - firstValue(a)).slice(0, chart.top_n);
        if (chart.sort === 'value_desc') order.sort((a, b) => firstValue(b) - firstValue(a));
        else if (chart.sort === 'value_asc') order.sort((a, b) => firstValue(a) - firstValue(b));
        else order.sort(byX);
        const pos = order.map(k => keys.indexOf(k));
        series.forEach(s => { s.data = pos.map(p => s.data[p]); s.counts = pos.map(p => s.counts[p]); });
        return {labels: order.map(k => groups.get(k).label), keys: order, series, notes, rows: kept.length};
    }

    // ── Describe A Chart ────────────────────────────────────────────────────
    const AGG_WORDS = {
        average: 'avg', avg: 'avg', mean: 'avg', total: 'sum', sum: 'sum', count: 'count', number: 'count',
        distinct: 'count_distinct', unique: 'count_distinct', min: 'min', minimum: 'min', lowest: 'min',
        smallest: 'min', max: 'max', maximum: 'max', highest: 'max', largest: 'max', biggest: 'max', median: 'median',
    };
    const TYPE_WORDS = {
        line: 'line', lines: 'line', trend: 'line', area: 'area', stacked: 'stacked_bar', horizontal: 'horizontal_bar',
        bar: 'bar', bars: 'bar', column: 'bar', columns: 'bar', pie: 'pie', doughnut: 'doughnut', donut: 'doughnut',
        scatter: 'scatter', table: 'table',
    };
    const GRAIN_WORDS = {
        day: 'day', days: 'day', daily: 'day', week: 'week', weeks: 'week', weekly: 'week', month: 'month',
        months: 'month', monthly: 'month', quarter: 'quarter', quarters: 'quarter', quarterly: 'quarter',
        year: 'year', years: 'year', yearly: 'year', annual: 'year', annually: 'year',
    };
    const BY_WORDS = new Set(['by', 'per', 'over', 'across', 'each']);
    const SPLIT_LEADS = ['split by', 'broken down by', 'break down by', 'coloured by', 'colored by', 'grouped by', 'for each'];
    const FILTER_WORDS = new Set(['where', 'only', 'excluding', 'without', 'when', 'if']);
    const DIFF_WORDS = new Set(['difference', 'differences', 'diff', 'gap', 'gaps']);
    const MINUS_WORDS = new Set(['minus', 'less', 'vs', 'versus']);
    const FILLER = new Set(['show', 'me', 'the', 'a', 'an', 'of', 'and', 'with', 'chart', 'graph', 'plot', 'as', 'in',
        'on', 'to', 'for', 'all', 'please', 'i', 'want', 'see', 'between', 'value', 'values', 'make', 'create', 'draw',
        'using', 'from', 'it', 'is', 'are', 'my', 'then', 'also', 'percentage', 'percent', 'pct', 'top', 'compare',
        'compared', 'comparing', '%']);

    function words(text) {
        return String(text || '').replace(/([<>!]?=|[<>])/g, ' $1 ').split(/[\s,]+/).map(w => w.trim()).filter(Boolean);
    }
    const norm = w => w.toLowerCase().replace(/[^a-z0-9%<>=!.]/g, '');
    const singular = w => w.length > 3 && w.endsWith('s') && !w.endsWith('ss') ? w.slice(0, -1) : w;

    // Every phrase that names a column: the column's own words, and the
    // semantic layer's titles and Also Called words for that column.
    function phraseTable(columns, hints) {
        const table = [];
        columns.forEach(c => table.push({tokens: c.name.toLowerCase().split(/[_\s]+/).filter(Boolean), column: c.name, exact: true}));
        const names = columns.map(c => c.name);
        (hints || []).forEach(h => {
            const listed = String(h.columns || '').toLowerCase();
            const targets = names.filter(n => new RegExp(`(^|[^a-z0-9_])${n.toLowerCase()}([^a-z0-9_]|$)`).test(listed));
            if (targets.length !== 1) return;
            [h.title].concat(String(h.synonyms || '').split(',')).forEach(p => {
                const tokens = words(p).map(norm).filter(Boolean);
                if (tokens.length) table.push({tokens, column: targets[0], exact: true});
            });
        });
        return table;
    }

    // The column the words from position i name, longest phrase first.
    function matchColumn(tokens, i, table, columns) {
        for (let n = Math.min(8, tokens.length - i); n >= 1; n--) {
            const gram = tokens.slice(i, i + n).map(singular);
            if (n === 1 && (FILLER.has(gram[0]) || BY_WORDS.has(gram[0]))) return null;
            const hits = [];
            table.forEach(p => {
                const pt = p.tokens.map(singular);
                for (let s = 0; s + n <= pt.length; s++) {
                    if (gram.every((g, k) => pt[s + k] === g)) {
                        // Score: whole phrase beats start of phrase beats middle; then shorter names.
                        const score = (n === pt.length ? 0 : s === 0 ? 1 : 2) * 100 + pt.length;
                        hits.push({column: p.column, score});
                        break;
                    }
                }
            });
            if (!hits.length) continue;
            hits.sort((a, b) => a.score - b.score);
            const best = hits.filter(h => h.score === hits[0].score).map(h => h.column);
            const unique = [...new Set(hits.map(h => h.column))];
            // "the prices": one plural word matching several number columns means all of them.
            const plural = n === 1 && tokens[i].endsWith('s') && tokens[i] !== gram[0];
            if (plural && unique.length > 1) {
                const numeric = unique.filter(c => (columns.find(x => x.name === c) || {}).type === 'number');
                if (numeric.length > 1) return {columns: numeric, length: n};
            }
            return {columns: [best[0]], length: n};
        }
        return null;
    }

    const OPS = [
        [['is', 'not'], '!='], [['isnt'], '!='], [['not'], '!='], [['!='], '!='], [['greater', 'than'], '>'],
        [['more', 'than'], '>'], [['over'], '>'], [['above'], '>'], [['>'], '>'], [['>='], '>='], [['at', 'least'], '>='],
        [['less', 'than'], '<'], [['under'], '<'], [['below'], '<'], [['<'], '<'], [['<='], '<='], [['at', 'most'], '<='],
        [['contains'], 'contains'], [['containing'], 'contains'], [['like'], 'contains'], [['is'], '='],
        [['equals'], '='], [['='], '='], [['are'], '='],
    ];

    function describe(text, columns, hints, defaults) {
        defaults = defaults || {};
        const raw = words(text), tokens = raw.map(norm);
        const table = phraseTable(columns, hints);
        const type = c => (columns.find(x => x.name === c) || {}).type;
        const chart = {type: defaults.type || 'bar', x: null, grain: 'none', split: null, measures: [], filters: [],
                       sort: 'x', top_n: 0, format: 'number', decimals: 0, from_zero: true, data_labels: false, legend: true, width: 1};
        const unknown = [];
        let agg = null, mode = 'measures', pendingDiff = null, pct = false, typeSet = false;
        const lower = tokens.join(' ');
        if (/%|percent|percentage|pct/.test(lower)) pct = true;

        let i = 0;
        while (i < tokens.length) {
            const t = tokens[i];
            const rest = tokens.slice(i).join(' ');
            const lead = SPLIT_LEADS.find(p => rest.startsWith(p + ' '));
            if (lead) { mode = 'split'; i += lead.split(' ').length; continue; }
            if (FILTER_WORDS.has(t)) { mode = 'filter'; i++; continue; }
            if (TYPE_WORDS[t]) {
                // "line chart", "bar" etc., unless the word starts a longer column name.
                const asColumn = matchColumn(tokens, i, table, columns);
                if (!asColumn || asColumn.length === 1) {
                    // "stacked bar" and "horizontal bar" stay stacked / horizontal.
                    const keep = TYPE_WORDS[t] === 'bar' && ['stacked_bar', 'horizontal_bar'].includes(chart.type) && typeSet;
                    if (!keep) chart.type = TYPE_WORDS[t];
                    typeSet = true; i++; continue;
                }
            }
            if (GRAIN_WORDS[t] && (mode === 'x' || mode === 'measures' || BY_WORDS.has(tokens[i - 1]))) {
                chart.grain = GRAIN_WORDS[t]; i++; continue;
            }
            if (BY_WORDS.has(t)) { mode = mode === 'x' && chart.x ? 'split' : 'x'; i++; continue; }
            if (t === 'top' && /^\d+$/.test(tokens[i + 1] || '')) {
                chart.top_n = +tokens[i + 1]; chart.sort = 'value_desc'; i += 2; continue;
            }
            if (AGG_WORDS[t] && mode === 'measures') {
                agg = AGG_WORDS[t];
                if (t === 'number' && tokens[i + 1] === 'of') i++;
                i++; continue;
            }
            if (DIFF_WORDS.has(t)) { pendingDiff = 'between'; i++; continue; }
            if (MINUS_WORDS.has(t) && chart.measures.length && mode === 'measures') { pendingDiff = 'minus'; i++; continue; }

            if (mode === 'filter') {
                const hit = matchColumn(tokens, i, table, columns);
                if (!hit) { if (!FILLER.has(t)) unknown.push(raw[i]); i++; continue; }
                let j = i + hit.length, op = '=';
                const found = OPS.find(([ws]) => ws.every((w, k) => tokens[j + k] === w));
                if (found) { op = found[1]; j += found[0].length; }
                const value = [];
                while (j < tokens.length && !FILTER_WORDS.has(tokens[j]) && !BY_WORDS.has(tokens[j]) && !TYPE_WORDS[tokens[j]]
                       && !(tokens[j] === 'and' && value.length) && !SPLIT_LEADS.some(p => tokens.slice(j).join(' ').startsWith(p + ' '))) {
                    value.push(raw[j]); j++;
                }
                if (tokens[j] === 'and') j++;
                let v = value.join(' ').replace(/^['"]|['"]$/g, '');
                if (type(hit.columns[0]) === 'bool') v = /^(true|yes|y|1)$/i.test(v) ? 'true' : /^(false|no|n|0)$/i.test(v) ? 'false' : v;
                chart.filters.push({column: hit.columns[0], op, value: v});
                i = j; continue;
            }

            const hit = matchColumn(tokens, i, table, columns);
            if (!hit) { if (!FILLER.has(t) && !/^\d+$/.test(t)) unknown.push(raw[i]); i++; continue; }
            i += hit.length;
            if (mode === 'x') {
                chart.x = hit.columns[0];
                if (GRAIN_WORDS[tokens[i]]) { chart.grain = GRAIN_WORDS[tokens[i]]; i++; }
                continue;
            }
            if (mode === 'split') { chart.split = hit.columns[0]; continue; }
            const a = agg || (type(hit.columns[0]) === 'number' ? (defaults.agg || 'avg') : 'count_distinct');
            if (pendingDiff === 'minus' && chart.measures.length) {
                chart.measures[chart.measures.length - 1].minus = hit.columns[0];
                pendingDiff = null;
                continue;
            }
            if (pendingDiff === 'between' && chart.measures.length && chart.measures[chart.measures.length - 1].diffOpen) {
                const last = chart.measures[chart.measures.length - 1];
                last.minus = hit.columns[0]; delete last.diffOpen; pendingDiff = null;
                continue;
            }
            hit.columns.forEach(c => chart.measures.push({column: c, agg: a, minus: null, as_pct: false, label: '', color: '',
                                                          diffOpen: pendingDiff === 'between' || undefined}));
            if (pendingDiff === 'between' && hit.columns.length > 1) pendingDiff = 'all';
        }
        chart.measures.forEach(m => { delete m.diffOpen; });

        // "the difference between the prices": each price minus the first one.
        if (pendingDiff === 'all' && chart.measures.length > 1) {
            const base = chart.measures[0];
            chart.measures = chart.measures.slice(1).map(m => ({...m, minus: base.column}));
        }
        chart.measures.forEach(m => { if (m.minus && pct) m.as_pct = true; });

        // A grain with no X column: group by the first date column.
        const dates = columns.filter(c => c.type === 'date' || c.type === 'datetime');
        if (!chart.x && chart.grain !== 'none' && dates.length) chart.x = dates[0].name;
        if (chart.x && chart.grain === 'none' && (type(chart.x) === 'date' || type(chart.x) === 'datetime')) chart.grain = defaults.grain || 'month';
        if (!typeSet && chart.x && chart.grain !== 'none' && chart.measures.length > 1) chart.type = 'line';
        if (chart.measures.some(m => m.as_pct)) { chart.format = 'percent'; chart.decimals = 1; }
        else if (chart.measures.length && chart.measures.every(m => /price|cost|margin|gpu|rrp|amount|value|fee|valuation|profit|commission/i.test(m.column)))
            chart.format = 'currency';

        chart.title = titleFor(chart);
        const understood = summary(chart);
        if (unknown.length) understood.push(`Words not used: ${[...new Set(unknown)].join(', ')}`);
        return {chart, understood, ok: chart.measures.length > 0 || !!chart.x};
    }

    function titleFor(chart) {
        const ms = chart.measures.length ? chart.measures.map(measureLabel).join(', ') : 'Rows';
        const by = chart.x ? ` By ${chart.grain !== 'none' ? columnLabel(chart.grain) : columnLabel(chart.x)}` : '';
        const split = chart.split ? ` And ${columnLabel(chart.split)}` : '';
        return (ms + by + split).slice(0, 200);
    }

    function summary(chart) {
        const lines = [`Chart: ${chart.type.replace('_', ' ')}`];
        if (chart.x) lines.push(`Along the bottom: ${columnLabel(chart.x)}${chart.grain !== 'none' ? ' by ' + chart.grain : ''}`);
        lines.push('Values: ' + (chart.measures.length ? chart.measures.map(measureLabel).join('; ') : 'number of rows'));
        if (chart.split) lines.push(`Split by: ${columnLabel(chart.split)}`);
        chart.filters.forEach(f => lines.push(`Only rows where ${columnLabel(f.column)} ${f.op} ${f.value}`));
        if (chart.top_n) lines.push(`Top ${chart.top_n} only`);
        return lines;
    }

    const api = {columnLabel, measureLabel, bucket, passes, aggregate, buildSeries, describe, titleFor, cohortRows, drillRows};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.SACore = api;
})(this);
