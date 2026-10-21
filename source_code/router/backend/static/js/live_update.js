// Live Update (routers/live_update.py): start a recipe and show every step
// while it runs. Shared by Settings > Live Update and the report pages.
// Needs api_requests.js (apiGetJson / apiPostJson) loaded first.
//
//   liveUpdateStart(BASE_URL, 'apex_missing_finance', box, { reload: true, onDone: run => fetchData(true) })
//   liveUpdateFollow(BASE_URL, runId, box, { onDone })   -> show a run already going
//
// onDone runs once, when the run has finished without failing, only if the
// run has a "Reload The Report Data" step. It must resolve to true when the
// reload worked, or false / { ok: false, message } when it did not.

const LU_STATUS = {
    waiting: ['Waiting', 'text-muted'], running: ['Running', 'text-primary'], done: ['Done', 'text-success'],
    failed: ['Failed', 'text-danger'], warning: ['Done With Warning', 'lu-warn'], stopped: ['Stopped', 'text-danger'],
    skipped: ['Not Run', 'text-muted'],
};

function luEsc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function luElapsed(step) {
    if (!step.started_at) return '';
    const end = step.finished_at ? new Date(step.finished_at) : new Date();
    const s = Math.max(0, Math.round((end - new Date(step.started_at)) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function luStyles() {
    if (document.getElementById('lu-styles')) return;
    const style = document.createElement('style');
    style.id = 'lu-styles';
    style.textContent = `
        .lu-steps td, .lu-steps th { vertical-align: top; font-size: 0.85rem; }
        .lu-steps .lu-detail { font-size: 0.75rem; color: #6c757d; }
        .lu-steps .lu-result { white-space: pre-wrap; font-size: 0.8rem; }
        .lu-steps tr.lu-running td { background: #eef5ff; }
        .lu-warn { color: #b45309; }
        .lu-log { white-space: pre-wrap; max-height: 360px; overflow-y: auto; font-size: 0.75rem; }`;
    document.head.appendChild(style);
}

function luBuild(box) {
    luStyles();
    box.innerHTML = `
        <div class="d-flex align-items-center gap-2 mb-2">
            <strong class="lu-title"></strong>
            <span class="lu-status"></span>
            <button type="button" class="btn btn-sm btn-outline-danger ms-auto lu-stop" style="display:none">
                <i class="fas fa-stop"></i> Stop</button>
        </div>
        <div class="lu-message mb-2"></div>
        <table class="table table-sm lu-steps mb-2">
            <thead><tr><th>#</th><th>Step</th><th>Status</th><th>Time</th><th>Result</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="lu-links small mb-2"></div>
        <div class="small fw-semibold">API Log</div>
        <pre class="lu-log lu-api-log border rounded p-2 bg-light mb-2"></pre>
        <div class="small fw-semibold">dbt Log</div>
        <pre class="lu-log lu-dbt-log border rounded p-2 bg-light mb-0"></pre>`;
}

function luRender(box, run, dbt) {
    const [label, cls] = LU_STATUS[run.status === 'running' ? 'running' : run.status] || [run.status, ''];
    box.querySelector('.lu-title').textContent = run.name;
    box.querySelector('.lu-status').innerHTML = `<span class="${cls}">${luEsc(label)}</span>` +
        (run.since ? ` <span class="text-muted small">- Apex changes since ${luEsc(run.since.replace('T', ' '))}</span>` : '');
    const msg = box.querySelector('.lu-message');
    msg.className = 'lu-message mb-2 ' + (run.status === 'failed' || run.status === 'stopped' ? 'text-danger' :
                                         run.status === 'done' ? 'text-success' : '');
    msg.textContent = run.message || '';
    if (run.lineage_old && run.status === 'running') {
        msg.innerHTML += `<div class="lu-warn small">The model list has not been rechecked recently - if the dbt SQL
            changed, a step may fail. Use Recheck Lineage on Settings > Live Update.</div>`;
    }
    box.querySelector('.lu-steps tbody').innerHTML = run.steps.map((s, i) => {
        const [sl, sc] = LU_STATUS[s.status] || [s.status, ''];
        return `<tr class="${s.status === 'running' ? 'lu-running' : ''}">
            <td>${i + 1}</td>
            <td><div>${luEsc(s.label)}</div><div class="lu-detail">${luEsc(s.detail || '')}</div></td>
            <td class="text-nowrap ${sc}">${luEsc(sl)}</td>
            <td class="text-nowrap">${luElapsed(s)}</td>
            <td class="lu-result">${luEsc(s.result || '')}</td></tr>`;
    }).join('');
    box.querySelector('.lu-stop').style.display = run.status === 'running' ? '' : 'none';
    if (dbt && dbt.run) {
        const l = dbt.run.links || {};
        box.querySelector('.lu-links').innerHTML = [l.ecs ? `<a href="${l.ecs}" target="_blank">ECS Task</a>` : '',
            l.logs ? `<a href="${l.logs}" target="_blank">CloudWatch Log</a>` : ''].filter(Boolean).join(' &middot; ');
    }
}

function luAppend(pre, lines) {
    if (!lines.length) return;
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += (pre.textContent ? '\n' : '') + lines.join('\n');
    if (atEnd) pre.scrollTop = pre.scrollHeight;
}

async function liveUpdateStart(baseUrl, recipeKey, box, opts = {}) {
    const d = await apiPostJson(baseUrl + '/api/live-update/start', {
        recipe_key: recipeKey, lookback_hours: opts.lookback || null, reload: !!opts.reload,
    });
    if (d.status !== 'ok') throw new Error(d.message || d.detail || 'could not start');
    return liveUpdateFollow(baseUrl, d.run_id, box, opts);
}

async function liveUpdateFollow(baseUrl, runId, box, opts = {}) {
    if (box.dataset.luRun === runId) return;   // already showing this run
    box.dataset.luRun = runId;
    luBuild(box);
    const apiPre = box.querySelector('.lu-api-log');
    const dbtPre = box.querySelector('.lu-dbt-log');
    box.querySelector('.lu-stop').onclick = async () => {
        if (!confirm('Stop this Live Update? Steps after the current one will not run.')) return;
        const s = await apiPostJson(`${baseUrl}/api/live-update/run/${runId}/stop`);
        box.querySelector('.lu-message').textContent = s.message || '';
    };
    let dbtRunId = '', token = '', headedRun = '', shownStart = 0, run = null;
    try {
        while (box.dataset.luRun === runId) {
            const q = new URLSearchParams({ dbt_run_id: dbtRunId, token });
            const d = await apiGetJson(`${baseUrl}/api/live-update/run/${runId}?${q}`, undefined, 60000);
            if (d.status !== 'ok') throw new Error(d.message || d.detail || 'could not read the run');
            run = d.run;
            if (d.api_log.length) {
                const atEnd = apiPre.scrollTop + apiPre.clientHeight >= apiPre.scrollHeight - 20;
                apiPre.textContent = d.api_log.join('\n');
                if (atEnd) apiPre.scrollTop = apiPre.scrollHeight;
            }
            if (d.dbt && d.dbt.run) {
                const r = d.dbt.run;
                if (r.run_id !== headedRun) {
                    luAppend(dbtPre, [`===== Step ${r.step} of ${r.steps}: ${r.job_name} =====`, r.command]);
                    headedRun = r.run_id; shownStart = 0;
                    if (dbtRunId !== r.run_id) { dbtRunId = r.run_id; token = ''; }
                }
                luAppend(dbtPre, r.start_log.slice(shownStart));
                shownStart = r.start_log.length;
                luAppend(dbtPre, d.dbt.lines || []);
                token = d.dbt.token || token;
                if (d.dbt.next_run_id) { dbtRunId = d.dbt.next_run_id; token = ''; }
            }
            luRender(box, run, d.dbt);
            if (run.status !== 'running') break;
            await new Promise(res => setTimeout(res, 3000));
        }
    } catch (e) {
        box.querySelector('.lu-message').textContent = 'Lost touch with the run: ' + e.message +
            ' - reopen this to pick it up again.';
        box.dataset.luRun = '';
        return run;
    }
    box.dataset.luRun = '';
    const reload = run && run.steps.find(s => s.key === 'reload');
    if (run && run.status === 'done' && reload && reload.status === 'waiting' && opts.onDone) {
        reload.status = 'running'; reload.started_at = new Date().toISOString();
        luRender(box, run, null);
        let ok = false, message = '';
        try {
            const res = await opts.onDone(run);
            ok = res === true || (res && res.ok === true);
            message = (res && res.message) || (ok ? '' : 'The data did not load - press Refresh Data');
        } catch (e) {
            message = e.message;
        }
        reload.status = ok ? 'done' : 'failed';
        reload.result = message;
        reload.finished_at = new Date().toISOString();
        luRender(box, run, null);
        apiPostJson(`${baseUrl}/api/live-update/run/${runId}/reload`, { ok, message }).catch(() => {});
    }
    if (opts.onFinished) opts.onFinished(run);
    return run;
}
