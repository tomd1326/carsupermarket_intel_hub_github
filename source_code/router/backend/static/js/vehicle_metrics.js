/* Vehicle Metrics section of Cars Bought Settings (admin only): pick a table,
 * column, calculation, date range, filters and what to match each car on.
 * The fields are explained in utils/vehicle_metrics.py.
 *
 * Only table and column names are read here. Snowflake's rows are read only by
 * Test, or once a metric is switched on. Loaded after cars_bought.js, whose
 * $, esc, postJSON, buyerCfg, openTableColumnEditor and loadMetricValues this uses.
 */
(function () {
    const FILTER_OPS = [
        ['equals', 'Equals'], ['not_equals', 'Does Not Equal'], ['in_list', 'Is One Of'], ['contains', 'Contains'],
        ['greater_than', 'Greater Than'], ['at_least', 'At Least'], ['less_than', 'Less Than'], ['at_most', 'At Most'],
        ['is_blank', 'Is Blank'], ['is_not_blank', 'Is Not Blank'], ['is_true', 'Is True'], ['is_false', 'Is False'],
    ];
    const VEHICLE_FIELDS = [['cap_id', 'CAP ID'], ['vrm', 'Reg'], ['make', 'Make'], ['model', 'Model'],
        ['derivative', 'Variant'], ['supplier_name', 'Supplier'], ['colour', 'Colour']];
    const CALC_NAMES = {avg: 'Average', median: 'Median', min: 'Lowest', max: 'Highest', sum: 'Total', count: 'Count Of Rows'};

    let draft = null;       // the metric being edited
    let columns = [];       // [{column, type}] of its table
    let tablesLoaded = false;

    const options = (list, chosen, blank) => (blank ? `<option value="">${esc(blank)}</option>` : '') +
        list.map(([v, l]) => `<option value="${esc(v)}"${v === chosen ? ' selected' : ''}>${esc(l)}</option>`).join('');
    const colOptions = (list, chosen) => {
        const names = list.map(c => c.column);
        const extra = chosen && !names.includes(chosen) ? [[chosen, `${chosen} (Not In The Table)`]] : [];
        return options(extra.concat(names.map(n => [n, n])), chosen, '(Choose A Column)');
    };
    const showError = text => {
        $('metricError').textContent = text || '';
        $('metricError').style.display = text ? '' : 'none';
    };

    function renderList() {
        const list = buyerCfg.metrics || [];
        $('metricList').innerHTML = list.map(m => `<div class="metric-item">
            <span class="grow"><strong>${esc(m.label)}</strong>
                <span class="text-muted">${esc(CALC_NAMES[m.calculation] || '')} Of ${esc(m.column)}, ${esc(m.table)}</span></span>
            <span class="tag ${m.enabled ? 'tag-ev' : 'tag-info'}">${m.enabled ? 'On' : 'Off'}</span>
            <button class="btn btn-sm btn-outline-secondary" data-medit="${esc(m.id)}">Edit</button>
            <button class="btn btn-sm btn-outline-danger" data-mdel="${esc(m.id)}" title="Delete"><i class="fas fa-times"></i></button>
        </div>`).join('') || '<div class="small text-muted">No metrics set up yet.</div>';
    }

    function renderFilters() {
        $('mFilters').innerHTML = draft.filters.map((f, i) => `<div class="metric-row">
            <select class="form-select form-select-sm" data-fcol="${i}">${colOptions(columns, f.column)}</select>
            <select class="form-select form-select-sm" data-fop="${i}">${options(FILTER_OPS, f.operator)}</select>
            <input class="form-control form-control-sm" data-fval="${i}" maxlength="200" value="${esc(f.value)}" placeholder="Value"
                ${['is_blank', 'is_not_blank', 'is_true', 'is_false'].includes(f.operator) ? 'disabled' : ''}>
            <button class="btn btn-sm btn-outline-danger" data-fdel="${i}" title="Remove"><i class="fas fa-times"></i></button>
        </div>`).join('');
    }

    function renderPairs(kind, el) {
        el.innerHTML = draft[kind].map((p, i) => `<div class="metric-row">
            <select class="form-select form-select-sm" data-pfield="${i}" data-kind="${kind}">${options(VEHICLE_FIELDS, p.field)}</select>
            <span class="small text-muted text-nowrap">Equals</span>
            <select class="form-select form-select-sm" data-pcol="${i}" data-kind="${kind}">${colOptions(columns, p.column)}</select>
            <button class="btn btn-sm btn-outline-danger" data-pdel="${i}" data-kind="${kind}" title="Remove"><i class="fas fa-times"></i></button>
        </div>`).join('');
    }

    function renderColumnChoices() {
        const usable = $('mCalculation').value === 'count' ? columns : columns.filter(c => c.is_number);
        $('mColumn').innerHTML = colOptions(usable, draft.column);
        $('mDateColumn').innerHTML = colOptions(columns.filter(c => c.is_date), draft.date_column);
        renderFilters();
        renderPairs('match', $('mMatch'));
        renderPairs('fallback', $('mFallback'));
    }

    function showDateInputs() {
        const mode = $('mDateMode').value;
        const show = (cls, on) => $('metricEditor').querySelectorAll(cls).forEach(el => { el.style.display = on ? '' : 'none'; });
        show('.m-date', mode !== 'all');
        show('.m-rolling', mode === 'rolling');
        show('.m-fixed', mode === 'fixed');
    }

    function loadTables() {
        if (tablesLoaded) return Promise.resolve();
        $('mTable').innerHTML = '<option value="">Loading Tables...</option>';
        return fetch(BASE + '/cars-bought/metric-tables').then(r => r.json()).then(res => {
            if (res.status !== 'ok') { showError(res.message); return; }
            tablesLoaded = true;
            $('mTable').dataset.tables = JSON.stringify(res.tables || []);
        });
    }

    function fillTables() {
        const tables = JSON.parse($('mTable').dataset.tables || '[]');
        const list = draft.table && !tables.includes(draft.table) ? [draft.table].concat(tables) : tables;
        $('mTable').innerHTML = options(list.map(t => [t, t]), draft.table, '(Choose A Table)');
    }

    function loadColumns(table) {
        columns = [];
        renderColumnChoices();
        if (!table) return Promise.resolve();
        return fetch(BASE + '/cars-bought/metric-columns?table=' + encodeURIComponent(table)).then(r => r.json()).then(res => {
            if (res.status !== 'ok') { showError(res.message); return; }
            columns = res.columns || [];
            renderColumnChoices();
        });
    }

    function openEditor(metric) {
        // A rule for a new metric: CAP ID is what Buying Requirements groups on.
        draft = metric ? JSON.parse(JSON.stringify(metric)) : {
            id: 'm_' + Date.now().toString(36), label: '', description: '', enabled: false, table: '', column: '',
            calculation: 'avg', ignore_zero: false, date_column: '', date_mode: 'rolling', days: 90, date_from: '', date_to: '',
            filters: [], match: [{field: 'cap_id', column: ''}], fallback: [], min_rows: 3, format: 'number', decimals: 1,
        };
        $('mLabel').value = draft.label;
        $('mDescription').value = draft.description || '';
        $('mCalculation').value = draft.calculation;
        $('mIgnoreZero').checked = !!draft.ignore_zero;
        $('mDateMode').value = draft.date_mode;
        $('mDays').value = draft.days ?? 90;
        $('mDateFrom').value = draft.date_from || '';
        $('mDateTo').value = draft.date_to || '';
        $('mMinRows').value = draft.min_rows;
        $('mFormat').value = draft.format;
        $('mDecimals').value = draft.decimals;
        $('mEnabled').checked = !!draft.enabled;
        $('mTestResult').innerHTML = '';
        showError('');
        showDateInputs();
        $('metricEditor').style.display = '';
        $('metricListBtns').style.display = 'none';
        loadTables().then(() => { fillTables(); return loadColumns(draft.table); })
            .catch(err => showError('Error: ' + err.message));
    }

    function closeEditor() {
        draft = null;
        $('metricEditor').style.display = 'none';
        $('metricListBtns').style.display = '';
    }

    const wholeOf = el => { const n = parseInt(el.value, 10); return isNaN(n) ? null : n; };

    function fromForm() {
        return {
            id: draft.id, label: $('mLabel').value, description: $('mDescription').value, enabled: $('mEnabled').checked,
            table: $('mTable').value, column: $('mColumn').value, calculation: $('mCalculation').value,
            ignore_zero: $('mIgnoreZero').checked, date_column: $('mDateColumn').value, date_mode: $('mDateMode').value,
            days: wholeOf($('mDays')), date_from: $('mDateFrom').value, date_to: $('mDateTo').value,
            filters: draft.filters, match: draft.match, fallback: draft.fallback,
            min_rows: wholeOf($('mMinRows')), format: $('mFormat').value, decimals: wholeOf($('mDecimals')),
        };
    }

    function afterSave(res) {
        buyerCfg.metrics = res.metrics;
        buyerCfg.table_columns = res.table_columns;
        buyerCfg.table_live = res.table_live;
        renderList();
        openTableColumnEditor();
        renderAllHead();
        renderAllBody();
        loadMetricValues();
    }

    $('metricAddBtn').addEventListener('click', () => openEditor(null));
    $('mCancelBtn').addEventListener('click', closeEditor);
    $('metricRefreshBtn').addEventListener('click', () => loadMetricValues(true));
    $('mDateMode').addEventListener('change', showDateInputs);
    $('mCalculation').addEventListener('change', () => { draft.column = $('mColumn').value; renderColumnChoices(); });
    $('mColumn').addEventListener('change', () => { draft.column = $('mColumn').value; });
    $('mDateColumn').addEventListener('change', () => { draft.date_column = $('mDateColumn').value; });
    $('mTable').addEventListener('change', () => {
        // Columns picked from the old table mean nothing on the new one.
        draft.table = $('mTable').value;
        draft.column = draft.date_column = '';
        draft.filters = [];
        draft.match = draft.match.map(p => ({...p, column: ''}));
        draft.fallback = draft.fallback.map(p => ({...p, column: ''}));
        showError('');
        loadColumns(draft.table).catch(err => showError('Error: ' + err.message));
    });
    $('mAddFilterBtn').addEventListener('click', () => {
        draft.filters.push({column: '', operator: 'equals', value: ''});
        renderFilters();
    });
    $('mAddMatchBtn').addEventListener('click', () => {
        draft.match.push({field: 'make', column: ''});
        renderPairs('match', $('mMatch'));
    });
    $('mAddFallbackBtn').addEventListener('click', () => {
        draft.fallback.push({field: 'make', column: ''});
        renderPairs('fallback', $('mFallback'));
    });

    $('mFilters').addEventListener('input', e => {
        const d = e.target.dataset;
        if (d.fval !== undefined) draft.filters[+d.fval].value = e.target.value;
    });
    $('mFilters').addEventListener('change', e => {
        const d = e.target.dataset;
        if (d.fcol !== undefined) draft.filters[+d.fcol].column = e.target.value;
        if (d.fop !== undefined) { draft.filters[+d.fop].operator = e.target.value; renderFilters(); }
    });
    $('mFilters').addEventListener('click', e => {
        const b = e.target.closest('[data-fdel]');
        if (b) { draft.filters.splice(+b.dataset.fdel, 1); renderFilters(); }
    });
    [$('mMatch'), $('mFallback')].forEach(el => {
        el.addEventListener('change', e => {
            const d = e.target.dataset;
            if (d.pfield !== undefined) draft[d.kind][+d.pfield].field = e.target.value;
            if (d.pcol !== undefined) draft[d.kind][+d.pcol].column = e.target.value;
        });
        el.addEventListener('click', e => {
            const b = e.target.closest('[data-pdel]');
            if (b) { draft[b.dataset.kind].splice(+b.dataset.pdel, 1); renderPairs(b.dataset.kind, el); }
        });
    });

    $('metricList').addEventListener('click', e => {
        const edit = e.target.closest('[data-medit]');
        if (edit) { openEditor((buyerCfg.metrics || []).find(m => m.id === edit.dataset.medit)); return; }
        const del = e.target.closest('[data-mdel]');
        if (!del) return;
        const m = (buyerCfg.metrics || []).find(x => x.id === del.dataset.mdel);
        if (!m || !confirm(`Delete the metric ${m.label}?`)) return;
        fetch(BASE + '/cars-bought/metrics/' + encodeURIComponent(m.id), {method: 'DELETE'}).then(r => r.json()).then(res => {
            if (res.status !== 'ok') { alert(res.message); return; }
            afterSave(res);
        });
    });

    $('mSaveBtn').addEventListener('click', () => {
        showError('');
        $('mSaveBtn').disabled = true;
        postJSON('/cars-bought/metrics', {metric: fromForm()}).then(res => {
            if (res.status !== 'ok') { showError(res.message); return; }
            closeEditor();
            afterSave(res);
        }).catch(err => showError('Error: ' + err.message)).finally(() => { $('mSaveBtn').disabled = false; });
    });

    $('mTestBtn').addEventListener('click', () => {
        showError('');
        $('mTestBtn').disabled = true;
        $('mTestResult').innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Reading Snowflake...';
        postJSON('/cars-bought/metrics/test', {metric: fromForm()}).then(res => {
            if (res.status !== 'ok') { $('mTestResult').innerHTML = ''; showError(res.message); return; }
            const sample = (res.sample || []).map(s => `<tr><td>${esc(s.key)}</td><td class="text-end">${s.value === null ? '' : Number(s.value).toLocaleString('en-GB', {maximumFractionDigits: 2})}</td><td class="text-end">${s.rows}</td></tr>`).join('');
            $('mTestResult').innerHTML = `<div>${res.rows} rows across ${res.groups} matched values. ${res.matched} of ${res.vehicles} bought cars would get a figure`
                + `${res.matched_fallback ? `, and ${res.matched_fallback} more from the fallback` : ''}.</div>`
                + (sample ? `<table class="table table-sm small mt-1 mb-0" style="width:auto;"><thead><tr><th>Value With Most Rows</th><th class="text-end">Figure</th><th class="text-end">Rows</th></tr></thead><tbody>${sample}</tbody></table>` : '');
        }).catch(err => { $('mTestResult').innerHTML = ''; showError('Error: ' + err.message); })
            .finally(() => { $('mTestBtn').disabled = false; });
    });

    // Called each time the settings popup opens.
    window.openVehicleMetrics = () => { closeEditor(); renderList(); };
})();
