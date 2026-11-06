// Guided source selection and explicit report review. No AI or prose-to-SQL.
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    let source = null, catalog = null, review = null, saved = null, settings = null;
    let mounted = false, loading = false, changeVersion = 0;
    const B = () => window.SAReportBuilder;
    const node = (tag, text, cls) => B().el(tag, cls || '', text);
    function input(parent, id, label, type = 'text') {
        const wrap = node('div', null, 'modal-field'), lab = node('label', label);
        lab.htmlFor = id;
        const value = document.createElement('input'); value.type = type; value.id = id;
        wrap.append(lab, value); parent.appendChild(wrap); return value;
    }
    function select(parent, id, label, values) {
        const wrap = node('div', null, 'modal-field'), lab = node('label', label);
        lab.htmlFor = id;
        const value = document.createElement('select'); value.id = id;
        B().fillSelect(value, values, ''); wrap.append(lab, value); parent.appendChild(wrap); return value;
    }
    function button(parent, text, action) {
        const b = node('button', text, 'btn btn-sm btn-outline-primary'); b.type = 'button';
        b.addEventListener('click', action); parent.appendChild(b); return b;
    }
    function invalidate() {
        changeVersion += 1;
        review = null;
        if ($('sa-b-run')) $('sa-b-run').disabled = true;
        if ($('sa-b-confirm')) $('sa-b-confirm').checked = false;
        if ($('sa-b-review')) $('sa-b-review').replaceChildren();
    }
    async function work(action) {
        if (loading) return;
        loading = true; $('sa-b-controls').disabled = true;
        $('sa-b-status').textContent = 'Working...';
        try { await action(); } catch (e) { $('sa-b-status').textContent = e.message; }
        finally { loading = false; $('sa-b-controls').disabled = false; }
    }
    function mount() {
        const box = $('sa-guided'); box.appendChild(node('h2', 'Build A Report With Questions'));
        const controls = node('fieldset'); controls.id = 'sa-b-controls'; box.appendChild(controls);
        const prompt = input(controls, 'sa-b-prompt', 'What Do You Want To Compare?'); prompt.maxLength = 3000;
        controls.appendChild(node('p', 'Your Words Find Relevant Knowledge And Fields. They Do Not Choose Calculations Or Apply Filters. Confirm Those Below.', 'sa-help'));
        button(controls, 'Find Knowledge And Sources', () => work(async () => {
            catalog = await B().api('/self-analytics/builder/catalog');
            B().fillSelect($('sa-b-source'), [['', 'Choose A Source'], ...catalog.sources.map(s => [s.id, s.name])], saved?.source_spec?.id || '');
            $('sa-b-status').textContent = catalog.issues.join(' ') || 'Choose A Source, Then Load Its Fields.';
            search();
        }));
        const knowledge = node('div'); knowledge.id = 'sa-b-knowledge'; controls.appendChild(knowledge);
        select(controls, 'sa-b-source', 'Which Dataset?', [['', 'Load Sources First']]);
        button(controls, 'Load Fields / Edit Saved Guided Source', () => work(async () => {
            const id = $('sa-b-source').value || saved?.source_spec?.id;
            if (!id) throw new Error('Choose A Source First. If None Are Listed, Add Starting Entries In Semantic & Context Layer And Approve The Report Source Definitions.');
            const version = changeVersion;
            const data = await B().api('/self-analytics/builder/source', {id});
            if (version !== changeVersion) throw new Error('The Report Changed While Loading. Choose The Source Again.');
            const restoring = saved?.source_spec?.id === id;
            if (!restoring && !confirm('Start A New Guided Report? Unsaved Changes To The Current Report Will Be Replaced.')) return;
            const previous = restoring ? saved : null;
            if (!restoring) B().start({id, revision: data.source.revision}, []);
            source = data.source;
            renderSource(previous);
            $('sa-b-status').textContent = 'Select Exact Fields, Answer The Questions, Then Add Charts. No Data Query Has Run.';
        }));
        const form = node('div'); form.id = 'sa-b-form'; controls.appendChild(form);
        const status = node('p', '', 'sa-note'); status.id = 'sa-b-status'; box.appendChild(status);
        prompt.addEventListener('input', () => { invalidate(); search(); });
        $('sa-b-source').addEventListener('change', () => { source = null; form.replaceChildren(); invalidate(); });
    }
    function search() {
        const box = $('sa-b-knowledge'); if (!box || !catalog) return;
        box.replaceChildren();
        const words = $('sa-b-prompt').value.toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length > 2);
        const entries = (catalog.knowledge || []).map(e => ({e, score: words.reduce((n, w) =>
            n + (JSON.stringify(e).toLowerCase().includes(w) ? 1 : 0), 0)})).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
        box.appendChild(node('p', catalog.limits, 'sa-help'));
        entries.slice(0, 8).forEach(({e}) => {
            const detail = node('details'); detail.append(node('summary', e.title), node('p', e.content)); box.appendChild(detail);
        });
        if (!entries.length) box.appendChild(node('p', 'No Approved Knowledge Matched. Clarify The Meaning With The Data Owner Before Continuing.', 'sa-help'));
        if (source) highlight();
    }
    function highlight() {
        const words = $('sa-b-prompt').value.toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length > 2);
        document.querySelectorAll('#sa-b-fields label').forEach(label => {
            label.style.fontWeight = words.some(w => label.textContent.toLowerCase().includes(w)) ? 'bold' : '';
        });
    }
    function selectedFields() {
        return Array.from(document.querySelectorAll('#sa-b-fields input:checked')).map(i => i.value);
    }
    function renderSource(report) {
        const form = $('sa-b-form'); form.replaceChildren(); invalidate();
        const spec = report?.source_spec || {};
        if (report) $('sa-b-prompt').value = spec.request || '';
        input(form, 'sa-b-name', 'Report Name').value = report?.name || '';
        form.appendChild(node('p', source.evidence, 'sa-help'));
        form.appendChild(node('p', 'One Row: ' + source.grain + '. Source Freshness: ' + (source.freshness || 'Checked When Run'), 'sa-help'));
        const fields = node('details'); fields.open = true; fields.id = 'sa-b-fields';
        fields.appendChild(node('summary', 'Which Measures, Dimensions And Drill-Down Details Do You Need?'));
        source.columns.forEach(c => {
            const label = node('label', null, 'd-block');
            const check = document.createElement('input'); check.type = 'checkbox'; check.value = c.name;
            check.checked = (spec.fields || []).includes(c.name);
            label.append(check, document.createTextNode(' ' + B().labels(c.name) + ' (' + c.type + ')'));
            fields.appendChild(label);
        });
        form.appendChild(fields);
        const dates = source.columns.filter(c => ['date', 'datetime'].includes(c.type));
        select(form, 'sa-b-date', 'Which Event Date?', [['', 'Choose'], ...dates.map(c => [c.name, B().labels(c.name)])]).value = spec.date || '';
        const range = node('div', null, 'sa-row two'); form.appendChild(range);
        input(range, 'sa-b-from', 'From (Inclusive)', 'date').value = spec.from || '';
        input(range, 'sa-b-to', 'To (Inclusive)', 'date').value = spec.to || '';
        source.questions.forEach((q, i) => select(form, 'sa-b-q-' + i, q.label,
            [['', 'Choose'], ...q.choices.map(v => [v.id, v.label])]).value = (spec.answers || {})[q.column] || '');
        const extra = node('div'); extra.id = 'sa-b-filters'; form.appendChild(extra);
        (spec.filters || []).forEach(f => addFilter(f));
        button(form, 'Add Report Filter', () => { addFilter({}); B().changed(); });
        const interactive = select(form, 'sa-b-interactive', 'Allow Interactive Filtering By (Ctrl-Click For Several; Empty Means None)',
            source.columns.filter(c => c.type === 'text' || c.type === 'bool').map(c => [c.name, B().labels(c.name)]));
        interactive.multiple = true;
        Array.from(interactive.options).forEach(o => { o.selected = (spec.interactive || []).includes(o.value); });
        form.appendChild(node('p', 'Confirm Price Basis, Cancelled Deals, Credits, Interest-Free Deals, Finance Switches, Consignments, Buybacks And Missing Estimates Where Relevant. Add Filters Above Or Per Chart. No Fallback Or Custom Channel Mapping Is Applied.', 'sa-help'));
        button(form, 'Add Chart / Table / Number Tile', () => { B().columns(source.columns.filter(c => selectedFields().includes(c.name) || c.name === $('sa-b-date').value)); B().addChart(); });
        button(form, 'Review Complete Report', () => work(preview));
        const summary = node('div'); summary.id = 'sa-b-review'; form.appendChild(summary);
        const check = input(form, 'sa-b-confirm', 'I Confirm The Sources, Row Meaning, Missing-Data Treatment, Filters, Calculations, Dates, Charts, Axes And Appearance', 'checkbox');
        const run = button(form, 'Confirm And Build Report', () => work(async () => {
            if (!review || !check.checked) throw new Error('Review And Confirm First');
            const payload = {...collect(), review_hash: review.review_hash, confirmed: true};
            const version = changeVersion;
            B().clearResult();
            const data = await B().api('/self-analytics/builder/run', payload);
            if (version !== changeVersion) throw new Error('The Report Changed While Running. Review It Again; The Old Result Was Discarded.');
            B().accept(data, $('sa-b-name').value.trim());
            $('sa-b-status').textContent = 'Report Built. Save Keeps Its Source, Choices And Charts. Click A Chart Point To Inspect Its Rows.';
        }));
        run.id = 'sa-b-run'; run.disabled = true;
        check.addEventListener('change', () => { run.disabled = !review || !check.checked; });
        form.addEventListener('input', e => { if (e.target !== check) B().changed(); });
        form.addEventListener('change', e => { if (e.target !== check) B().changed(); });
        fields.addEventListener('change', () => B().columns(source.columns.filter(c => selectedFields().includes(c.name) || c.name === $('sa-b-date').value)));
        B().columns(source.columns.filter(c => selectedFields().includes(c.name))); highlight();
    }
    function addFilter(f) {
        const row = node('div', null, 'sa-row four'); row.dataset.filter = '1';
        const col = document.createElement('select'), op = document.createElement('select'), value = document.createElement('input');
        B().fillSelect(col, [['', 'Choose Column'], ...source.columns.map(c => [c.name, B().labels(c.name)])], f.column);
        B().fillSelect(op, [['=', 'Is'], ['!=', 'Is Not'], ['>', 'Above'], ['>=', 'At Least'], ['<', 'Below'], ['<=', 'At Most'], ['is_empty', 'Is Empty'], ['not_empty', 'Is Not Empty']], f.op || '=');
        value.value = f.value ?? ''; value.setAttribute('aria-label', 'Filter Value');
        col.setAttribute('aria-label', 'Filter Column'); op.setAttribute('aria-label', 'Filter Operator');
        row.append(col, op, value); button(row, 'Remove', () => { row.remove(); B().changed(); });
        $('sa-b-filters').appendChild(row);
    }
    function collect() {
        if (!source) throw new Error('Choose A Source');
        const answers = {}; source.questions.forEach((q, i) => { answers[q.column] = $('sa-b-q-' + i).value; });
        const filters = Array.from(document.querySelectorAll('#sa-b-filters [data-filter]')).map(row => {
            const selects = row.querySelectorAll('select'); return {column: selects[0].value, op: selects[1].value, value: row.querySelector('input').value};
        });
        return {prompt: $('sa-b-prompt').value, charts: B().charts(), source_spec: {
            id: source.id, revision: source.revision, date: $('sa-b-date').value,
            from: $('sa-b-from').value, to: $('sa-b-to').value, fields: selectedFields(), answers, filters,
            interactive: Array.from($('sa-b-interactive').selectedOptions).map(o => o.value)
        }};
    }
    async function preview() {
        invalidate();
        if (!$('sa-b-name').value.trim()) throw new Error('Give The Report A Name');
        const version = changeVersion;
        const plan = await B().api('/self-analytics/builder/preview', collect());
        if (version !== changeVersion) throw new Error('The Report Changed During Review. Review Again.');
        review = plan;
        const box = $('sa-b-review');
        box.appendChild(node('h3', 'Confirm This Interpretation'));
        box.appendChild(node('p', 'Your Request: ' + plan.prompt));
        box.appendChild(node('p', `${plan.source_name}. One Row: ${plan.grain}. Date: ${B().labels(plan.source_spec.date)}. ${plan.source_spec.from} To ${plan.source_spec.to}, Inclusive.`));
        box.appendChild(node('p', 'Fields: ' + plan.source_spec.fields.map(B().labels).join(', ')));
        box.appendChild(node('p', 'Report Filters: ' + (plan.filters.map(f => `${B().labels(f.column)} ${f.op} ${f.value ?? ''}`).join('; ') || 'None')));
        box.appendChild(node('p', 'Interactive Filters: ' + (plan.source_spec.interactive.join(', ') || 'None')));
        box.appendChild(node('p', `Layout: ${settings.charts_per_row} Columns; Chart Height ${settings.chart_height_px}px. Normal Series Colours; No Targets Or Red/Green Performance Bands.`));
        box.appendChild(node('p', 'Default Series Colours: ' + settings.palette.join(', ') + '. Currency Symbol: ' + settings.currency_symbol));
        box.appendChild(node('p', 'Source Freshness: ' + plan.freshness + '. No Missing Values Are Replaced. Ratios With Zero Denominators Are Blank. No Joins Are Added.'));
        plan.charts.forEach((c, i) => {
            const d = node('details'); d.open = true;
            d.appendChild(node('summary', `${i + 1}. ${c.title || 'Untitled'} — ${B().labels(c.type)}`));
            d.appendChild(node('p', `X: ${c.x || 'One Total'}; Grouping: ${c.grain}; Split: ${c.split || 'None'}; Units: ${c.format}; Decimals: ${c.decimals}; Rows: ${c.cohort}.`));
            d.appendChild(node('p', 'Measures: ' + c.measures.map(window.SACore.measureLabel).join('; ')));
            d.appendChild(node('p', 'Subtitle: ' + (c.subtitle || 'None') + '. Series Colour Overrides: ' + (c.measures.map(m => m.color || 'Palette').join(', '))));
            d.appendChild(node('p', `Axes: ${c.x_title || 'No X Title'} / ${c.y_title || 'No Y Title'}. ${c.from_zero ? 'Start At Zero' : 'Automatic Range'}. One Value Axis. Legend: ${c.legend}; Values: ${c.data_labels}; Width: ${c.width}; Order: ${c.sort}; Top: ${c.top_n || 'All'}.`));
            d.appendChild(node('p', 'Chart Filters: ' + (c.filters.map(f => `${f.column} ${f.op} ${f.value}`).join('; ') || 'None')));
            box.appendChild(d);
        });
        const sql = node('details'); sql.append(node('summary', 'Source Details'), node('pre', plan.sql || 'Configured SharePoint Copy; No Snowflake Query'));
        box.appendChild(sql);
        box.appendChild(node('p', 'If Any Part Of Your Request Is Missing Here, Do Not Confirm. Change The Choices Or Add The Missing Source Knowledge.', 'sa-help'));
        $('sa-b-status').textContent = 'Review Ready. Nothing Has Been Queried In Snowflake.';
    }
    document.addEventListener('sa-plan-changed', invalidate);
    document.addEventListener('sa-report-opened', e => {
        saved = e.detail; source = null; invalidate();
        if ($('sa-b-form')) $('sa-b-form').replaceChildren();
    });
    document.addEventListener('sa-ready', e => {
        const changed = settings && JSON.stringify(settings) !== JSON.stringify(e.detail.settings);
        settings = e.detail.settings;
        if (changed) {
            invalidate();
            if (source) B().changed();
        }
        const box = $('sa-guided'); if (!box) return;
        box.hidden = !settings.builder_enabled || !e.detail.canSql;
        if (!box.hidden && !mounted) { mount(); mounted = true; }
    });
})();
