"""Reports page and the generic report task endpoints.

Adding a new report only requires:
  1. A module under reports/ exposing run_report(progress_cb) -> file bytes
  2. An entry in REPORTS below (label, module, output extension/content-type)

A report that needs a date range sets 'date_range': True on its REPORTS entry.
That shows the From/To pickers on the page and changes the module's signature to
run_report(progress_cb, params), where params carries 'date_from'/'date_to' as
YYYY-MM-DD strings. The report applies its own default for anything missing.
Reports without the flag keep the plain run_report(progress_cb) signature.

Flow (mirrors the /elaborate-task background pattern):
  POST /api/reports/{report_type}/start  -> creates task_id, writes initial
       status JSON + appends to the S3 task index, fires an async
       self-invocation of /api/reports/run
  POST /api/reports/run    -> internal worker (no session auth, like
       /elaborate-task); runs the report module, updating the status JSON
       (and index entry) per stage, then writes the output file to S3
  GET  /api/reports/status/{task_id}    -> polled by the page for the
       currently-running task
  GET  /api/reports/tasks               -> the full task history (all report
       types, newest first)
  GET  /api/reports/download/{task_id}  -> streams the output file

Task history is kept in a single S3 JSON index file (reports/index.json)
rather than Aurora — this app's `tasks` table belongs to the Supplier Stock
Evaluator flow and isn't shared with report tasks. Low write frequency (one
admin, occasional report runs) makes a get-then-put index update sufficiently
safe without needing S3 conditional writes.
"""
import datetime
import importlib
import json
import logging
import os
import re
import tempfile
import threading
import uuid

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse

from utils.lambda_http import LambdaRequestsManager
from .core import BASE_URL, templates, _track, get_current_user, s3_bucketname, s3_bucket_region, _S3_ENV

_is_local = os.getenv('LOCAL_ENV') == '1'

logger = logging.getLogger(__name__)

router = APIRouter()

_S3_PREFIX = f'applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/reports'
_INDEX_KEY = f'{_S3_PREFIX}/index.json'
_MAX_INDEX_ENTRIES = 200  # keep the scrollable list bounded

_XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

# Report registry — add new reports here.
REPORTS = {
    'vehicles-autoedit': {
        'label': 'Vehicles - AutoEdit',
        'module': 'reports.vehicles_autoedit',
        'extension': 'xlsx',
        'content_type': _XLSX_CONTENT_TYPE,
    },
    'crm-selling-enquiries': {
        'label': 'CRM - Selling - Enquiries',
        'module': 'reports.crm_selling_enquiries',
        'extension': 'xlsx',
        'content_type': _XLSX_CONTENT_TYPE,
        # Shows the date range pickers on the page and passes the chosen dates
        # to run_report as params. Both default to yesterday.
        'date_range': True,
    },
}


def _s3_client():
    # aa-raw-sources-uk is always in eu-west-1 regardless of Lambda region
    return boto3.client('s3', region_name=s3_bucket_region)


def _status_key(task_id: str) -> str:
    return f'{_S3_PREFIX}/{task_id}.json'


def _output_key(task_id: str, extension: str) -> str:
    return f'{_S3_PREFIX}/{task_id}.{extension}'


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _slugify(text: str) -> str:
    """Lowercase, alphanumerics joined by hyphens, capped — safe for filenames."""
    if not text:
        return ''
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:40].rstrip('-')


def _clean_date(value) -> str:
    """Keep a YYYY-MM-DD string, or drop it. The report module applies its own
    default (yesterday) for anything missing, so a bad value is not fatal."""
    if not isinstance(value, str):
        return ''
    value = value.strip()[:10]
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return ''
    return value


def _report_params(report_type: str, payload: dict) -> dict:
    """The run parameters a report accepts, taken off the start request."""
    if not REPORTS.get(report_type, {}).get('date_range'):
        return {}
    return {
        'date_from': _clean_date(payload.get('date_from')),
        'date_to': _clean_date(payload.get('date_to')),
    }


def _date_range_slug(params: dict) -> str:
    """Date range for the output filename: one day is just that day."""
    params = params or {}
    date_from, date_to = params.get('date_from'), params.get('date_to')
    if not date_from and not date_to:
        return ''
    if date_from == date_to or not date_to:
        return date_from or ''
    if not date_from:
        return date_to
    return f'{date_from}-to-{date_to}'


def _write_status(task_id: str, report_type: str, status: str, pct: int, stage: str,
                  started_at: str, user: str = None, notes: str = None,
                  filename: str = None, error: str = None, params: dict = None):
    body = {
        'task_id': task_id, 'report_type': report_type, 'user': user, 'notes': notes,
        'status': status, 'pct': pct, 'stage': stage,
        'started_at': started_at, 'updated_at': _now_iso(),
        'filename': filename, 'error': error, 'params': params or {},
    }
    if status in ('done', 'error'):
        body['completed_at'] = body['updated_at']
    _s3_client().put_object(
        Bucket=s3_bucketname, Key=_status_key(task_id),
        Body=json.dumps(body).encode('utf-8'), ContentType='application/json',
    )
    _upsert_index_entry(body)
    return body


def _read_index() -> list:
    try:
        obj = _s3_client().get_object(Bucket=s3_bucketname, Key=_INDEX_KEY)
        return json.loads(obj['Body'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return []
        raise


def _write_index(entries: list):
    _s3_client().put_object(
        Bucket=s3_bucketname, Key=_INDEX_KEY,
        Body=json.dumps(entries).encode('utf-8'), ContentType='application/json',
    )


def _upsert_index_entry(entry: dict):
    """Get-then-put update of the index. Single-admin, low-frequency writes —
    a lost update here just means a stale row in the list until the next
    status write for that task, not data loss."""
    entries = _read_index()
    entries = [e for e in entries if e['task_id'] != entry['task_id']]
    entries.append(entry)
    entries.sort(key=lambda e: e.get('started_at') or '', reverse=True)
    entries = entries[:_MAX_INDEX_ENTRIES]
    _write_index(entries)


def _report_config(report_type: str) -> dict:
    config = REPORTS.get(report_type)
    if not config:
        raise HTTPException(status_code=404, detail=f"Unknown report type: {report_type}")
    return config


# A running task writes a status update at every progress stage; the longest
# silent stretch is a single big Snowflake query or API delta fetch, well
# under this. Anything quieter for longer was orphaned (local server restart,
# Lambda timeout/crash) and would otherwise sit as "Running" forever.
_STALE_RUNNING_MINUTES = 10


def _mark_failed_if_stale(entry: dict) -> dict:
    """Flip an orphaned 'running' task to 'error' and persist the change."""
    if entry.get('status') != 'running':
        return entry
    try:
        updated_at = datetime.datetime.fromisoformat(entry['updated_at'])
    except (KeyError, TypeError, ValueError):
        return entry
    age = datetime.datetime.utcnow() - updated_at
    if age < datetime.timedelta(minutes=_STALE_RUNNING_MINUTES):
        return entry
    logger.warning('Report task %s stale (no updates for %s) — marking failed', entry.get('task_id'), age)
    return _write_status(
        entry['task_id'], entry.get('report_type'), 'error', entry.get('pct') or 0, 'Failed',
        entry.get('started_at'), user=entry.get('user'), notes=entry.get('notes'),
        error=f'Task stalled — no progress updates for over {_STALE_RUNNING_MINUTES} minutes '
              '(likely a server restart or timeout)',
    )


@router.get("/reports")
def reports_root():
    """Old single Reports entry point — home now links straight to
    /reports/apex and /reports/tom."""
    return RedirectResponse(url=BASE_URL + "/reports/tom", status_code=303)


@router.get("/reports/tom", response_class=HTMLResponse)
def tom_reports(request: Request, user: str = Depends(get_current_user)):
    """Tom Reports landing page — currently the Orders & Deliveries report."""
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/reports/tom')
    return templates.TemplateResponse("reports_landing.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
    })


@router.get("/reports/apex", response_class=HTMLResponse)
def reports_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/reports/apex')
    report_options = [
        {'type': k, 'label': v['label'], 'date_range': bool(v.get('date_range'))}
        for k, v in REPORTS.items()
    ]
    # Both date pickers default to yesterday — the most recent complete day.
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    return templates.TemplateResponse("reports.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "report_options": report_options, "default_date": yesterday,
    })


@router.get("/api/reports/tasks")
def list_report_tasks(user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    entries = [_mark_failed_if_stale(e) for e in _read_index()]
    for e in entries:
        e['report_label'] = REPORTS.get(e.get('report_type'), {}).get('label', e.get('report_type'))
    return {'tasks': entries}


def _run_report_worker(task_id: str, report_type: str, started_at: str, user: str = None,
                       notes: str = None, params: dict = None):
    """The actual report build + upload. Runs either inside the async worker
    Lambda invocation (dev/prod) or a background thread (local dev), so
    either way the caller of start_report gets an immediate response and the
    frontend sees live polling progress rather than a blocked request."""
    config = _report_config(report_type)
    try:
        module = importlib.import_module(config['module'])

        def progress(pct, stage):
            _write_status(task_id, report_type, 'running', pct, stage, started_at,
                          user=user, notes=notes, params=params)

        # Only pass params to reports that take them, so existing report
        # modules keep their zero-argument run_report signature.
        if config.get('date_range'):
            file_bytes = module.run_report(progress_cb=progress, params=params or {})
        else:
            file_bytes = module.run_report(progress_cb=progress)

        _write_status(task_id, report_type, 'running', 97, 'Uploading report', started_at,
                      user=user, notes=notes, params=params)
        _s3_client().put_object(
            Bucket=s3_bucketname, Key=_output_key(task_id, config['extension']),
            Body=file_bytes, ContentType=config['content_type'],
        )
        timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        notes_slug = _slugify(notes)
        date_slug = _date_range_slug(params)
        name_parts = ([report_type] + ([date_slug] if date_slug else [])
                      + ([notes_slug] if notes_slug else []) + [timestamp])
        filename = f"{'-'.join(name_parts)}.{config['extension']}"
        _write_status(task_id, report_type, 'done', 100, 'Complete', started_at,
                      user=user, notes=notes, filename=filename, params=params)
    except Exception as e:
        logger.exception('Report task %s (%s) failed', task_id, report_type)
        _write_status(task_id, report_type, 'error', 100, 'Failed', started_at,
                      user=user, notes=notes, error=str(e), params=params)


@router.post("/api/reports/{report_type}/start")
def start_report(report_type: str, request: Request, payload: dict = None,
                 user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    _report_config(report_type)  # 404s if unknown
    payload = payload or {}
    notes = (payload.get('notes') or '').strip()[:100] or None
    params = _report_params(report_type, payload)

    # Single-flight per report type: a report with no parameters would produce
    # identical output twice while doubling the Snowflake/API load (and racing
    # the delta cache), so the caller is attached to the in-flight run instead.
    # A report with a date range is only a duplicate when the range matches -
    # two different ranges are genuinely different reports.
    for entry in _read_index():
        if entry.get('report_type') == report_type and entry.get('status') == 'running':
            if (entry.get('params') or {}) != params:
                continue
            entry = _mark_failed_if_stale(entry)
            if entry.get('status') == 'running':
                logger.info('Report %s already running as task %s — attaching caller',
                            report_type, entry['task_id'])
                return {'task_id': entry['task_id'], 'existing': True}

    task_id = uuid.uuid4().hex
    started_at = _now_iso()
    _write_status(task_id, report_type, 'running', 0, 'Queued', started_at,
                  user=user, notes=notes, params=params)
    _track(request, 'run_report', '/reports', {'report': report_type, 'task_id': task_id,
                                               'notes': notes, **params})

    aws_context = request.scope.get("aws.context")
    if _is_local or aws_context is None:
        # Not on Lambda (uvicorn/docker local run — aws.context is only set
        # by Mangum). LambdaRequestsManager.simulate_http would run the
        # worker inline and block until it finishes — a background thread
        # keeps this route non-blocking so polling shows live progress,
        # matching the dev/prod async-Lambda behaviour.
        threading.Thread(
            target=_run_report_worker,
            args=(task_id, report_type, started_at, user, notes, params),
            daemon=True,
        ).start()
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/api/reports/run",
            method="POST",
            data={'task_id': task_id, 'report_type': report_type, 'started_at': started_at,
                  'user': user, 'notes': notes, 'params': params},
        )
    return {'task_id': task_id}


@router.post("/api/reports/run")
async def run_report_task(data: dict):
    """Internal worker route, invoked via async Lambda self-invocation
    (dev/prod only — local dev uses the background thread in start_report)."""
    _run_report_worker(data['task_id'], data['report_type'], data['started_at'],
                       data.get('user'), data.get('notes'), data.get('params'))
    return {'ok': True}


@router.get("/api/reports/status/{task_id}")
def report_status(task_id: str, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        obj = _s3_client().get_object(Bucket=s3_bucketname, Key=_status_key(task_id))
        return _mark_failed_if_stale(json.loads(obj['Body'].read()))
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")


@router.get("/api/reports/download/{task_id}")
def download_report(task_id: str, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        obj = _s3_client().get_object(Bucket=s3_bucketname, Key=_status_key(task_id))
        status = json.loads(obj['Body'].read())
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")
    if status.get('status') != 'done':
        raise HTTPException(status_code=409, detail="Report not ready")

    config = _report_config(status['report_type'])
    fd, tmp_path = tempfile.mkstemp(suffix='.' + config['extension'])
    os.close(fd)
    try:
        _s3_client().download_file(s3_bucketname, _output_key(task_id, config['extension']), tmp_path)
    except Exception:
        raise HTTPException(status_code=404, detail="Report file not found on S3")

    filename = status.get('filename') or f"{status['report_type']}-{task_id}.{config['extension']}"
    return FileResponse(
        tmp_path,
        media_type=config['content_type'],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
