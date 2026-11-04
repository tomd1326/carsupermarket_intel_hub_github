/* All Cars Bought, admin only: import the old Cars Bought sheet from a CSV.
 * Choosing a file only reads it and says what would happen. Import saves as much
 * as the server can in one request and is asked again until nothing is left;
 * cars already saved are skipped, so a request that fails can just be run again.
 * Loaded after cars_bought_table.js. */
(function () {
    let file = null;        // {filename, data (base64)}
    let mapping = null;     // {CSV heading: field key}

    function showError(text) {
        $('importError').textContent = text || '';
        $('importError').style.display = text ? '' : 'none';
    }

    const send = save => postJSON('/cars-bought/import-csv', {...file, mapping, save});

    function renderPlan(res) {
        mapping = res.mapping;
        const options = key => `<option value="">Not Imported</option>` + res.options.map(o =>
            `<option value="${esc(o.key)}"${o.key === key ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
        $('importMapBody').innerHTML = res.headings.map((h, i) => `<tr>
            <td class="fw-semibold">${esc(h)}</td><td class="text-muted">${esc(res.first_row[i] || '')}</td>
            <td><select class="form-select form-select-sm" data-heading="${i}">${options(mapping[h])}</select></td></tr>`).join('');
        $('importMapBody').dataset.headings = JSON.stringify(res.headings);
        const lines = [`<strong>${res.cars} Cars In ${res.batches} Purchases Ready To Import</strong>, from ${res.rows} rows.`];
        if (res.already_here) lines.push(`${res.already_here} already here with the same date, left out.`);
        if (res.bought_on_another_date) lines.push(`${res.bought_on_another_date} have a VRM already here on another date, and will be added.`);
        if (res.new_suppliers.length) lines.push(`New suppliers that will be added: ${res.new_suppliers.map(esc).join(', ')}.`);
        if (res.missing.length) lines.push(`<span class="text-danger fw-bold">Choose a column for: ${res.missing.map(esc).join(', ')}.</span>`);
        $('importSummary').innerHTML = lines.join('<br>');
        $('importProblems').innerHTML = res.problems.length
            ? res.problems.map(p => `<div>${esc(p)}</div>`).join('') + (res.problems_total > res.problems.length ? `<div class="text-muted">And ${res.problems_total - res.problems.length} more</div>` : '')
            : '<span class="text-muted">None</span>';
        $('importDetail').style.display = '';
        $('importSaveBtn').style.display = res.cars ? '' : 'none';
        $('importSaveBtn').disabled = false;
        $('importSaveBtn').textContent = `Import ${res.cars} Cars`;
    }

    function check() {
        showError('');
        $('importSaveBtn').disabled = true;
        send(false).then(res => {
            if (res.status !== 'ok') { showError(res.message); return; }
            renderPlan(res);
        }).catch(err => showError('Error: ' + err.message));
    }

    function reset() {
        file = null;
        mapping = null;
        $('importFile').value = '';
        $('importSummary').textContent = '';
        $('importDetail').style.display = 'none';
        $('importSaveBtn').style.display = 'none';
        showError('');
    }

    async function save() {
        const total = +$('importSaveBtn').textContent.replace(/\D/g, '');
        if (!confirm(`Import ${total} cars? They go straight into the All Cars Bought table.`)) return;
        $('importSaveBtn').disabled = true;
        showError('');
        let saved = 0;
        try {
            for (;;) {
                const res = await send(true);
                if (res.status !== 'ok') throw new Error(res.message);
                saved += res.saved_cars;
                $('importSummary').textContent = `Saved ${saved} of ${total} cars...`;
                if (!res.batches_left) break;
                if (!res.saved_cars) throw new Error('Nothing could be saved');
            }
            $('importSummary').innerHTML = `<strong>${saved} cars imported.</strong>`;
            $('importSaveBtn').style.display = 'none';
            $('importDetail').style.display = 'none';
            loadAllVehicles();
        } catch (err) {
            showError(`Stopped after ${saved} cars: ${err.message}. Press Import again to carry on; saved cars are not added twice.`);
            $('importSaveBtn').disabled = false;
        }
    }

    $('importOpenBtn').addEventListener('click', () => {
        reset();
        bootstrap.Modal.getOrCreateInstance($('importModal')).show();
    });
    $('importFile').addEventListener('change', e => {
        const chosen = e.target.files[0];
        if (!chosen) return;
        if (chosen.size > 4 * 1024 * 1024) { showError(`${chosen.name} is larger than 4 MB. Split it into smaller files.`); return; }
        const reader = new FileReader();
        reader.onload = () => {
            file = {filename: chosen.name, data: String(reader.result).split(',')[1] || ''};
            mapping = null;
            check();
        };
        reader.readAsDataURL(chosen);
    });
    $('importMapBody').addEventListener('change', e => {
        const headings = JSON.parse($('importMapBody').dataset.headings);
        mapping[headings[+e.target.dataset.heading]] = e.target.value;
        check();
    });
    $('importSaveBtn').addEventListener('click', save);
})();
