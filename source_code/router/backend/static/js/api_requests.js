// Manual API requests (routers/api_requests.py). Shared so any page can start
// a run and show its log, not only Settings > API Requests.
//
//   runApiRequest(BASE_URL, 'nexus', { routes: ['vehicles'], updated_since: '2026-10-19T08:00', ... },
//                 { onLog: lines => ..., onStart: jobId => ... })
//     -> resolves to the finished job { status, message, log }
//   watchApiRequest(BASE_URL, jobId, { onLog }) -> follow a run already going
//   stopApiRequest(BASE_URL, 'nexus') -> { status, message }

async function apiGetJson(url, opts, timeoutMs = 20000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    let resp;
    try {
        resp = await fetch(url, { ...(opts || {}), signal: ctrl.signal });
    } catch (e) {
        if (e.name === 'AbortError') throw new Error('no answer from the server after ' + timeoutMs / 1000 + 's');
        throw e;
    } finally {
        clearTimeout(t);
    }
    const text = await resp.text();
    try {
        return JSON.parse(text);
    } catch (e) {
        throw new Error('HTTP ' + resp.status + ': ' + (text.slice(0, 300) || '(empty response)'));
    }
}

function apiPostJson(url, payload) {
    return apiGetJson(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {}),
    });
}

async function watchApiRequest(baseUrl, jobId, handlers = {}) {
    // Poll every 2 seconds until the run finishes.
    while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const job = await apiGetJson(baseUrl + '/api/api-requests/job/' + jobId);
        if (handlers.onLog) handlers.onLog(job.log || []);
        if (['done', 'failed', 'stopped'].includes(job.status)) return job;
    }
}

async function runApiRequest(baseUrl, source, params, handlers = {}) {
    const start = await apiPostJson(`${baseUrl}/api/api-requests/${source}/start`, params);
    if (start.status !== 'started') throw new Error(start.message || 'could not start');
    if (handlers.onStart) handlers.onStart(start.job_id);
    return watchApiRequest(baseUrl, start.job_id, handlers);
}

function stopApiRequest(baseUrl, source) {
    return apiPostJson(`${baseUrl}/api/api-requests/${source}/stop`);
}
