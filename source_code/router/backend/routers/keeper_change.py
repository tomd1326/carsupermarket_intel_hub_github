"""Keeper Change Check — last keeper change dates for a list of VRMs.

Replaces a hand-run script (autotrader_keeper_changes_v1.1.py) that read a CSV
of VRMs, called the Autotrader /vehicles API with history=true, and wrote a CSV.

Two different existing pages are followed on purpose:

  - How the work is dispatched comes from reports.py. A background thread is NOT
    safe here: the app runs under Mangum on Lambda, and once the request returns
    Lambda may freeze the environment mid-run. dev/prod therefore re-invoke the
    same Lambda asynchronously against the internal worker route below.
  - The XLSX build, the S3 history listing and the download route come from
    mot_check.py, which does those parts well.
"""
import datetime
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import requests
from botocore.exceptions import ClientError
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

from utils.s3_tools import download_file
from utils.lambda_http import LambdaRequestsManager

from .core import (BASE_URL, templates, s3_bucketname, s3_bucket_region, _track,
                   require_page, require_page_api, _uk_now, worker_secret, check_worker_secret)

logger = logging.getLogger(__name__)

router = APIRouter()

_ENV = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
_IS_LOCAL = os.getenv('LOCAL_ENV') == '1'
_S3_PREFIX = f"applications/pricing/uk-vehicle-list-evaluator/{_ENV}/keeper-change/"

# A single Lambda invocation cannot process a large list: the function's timeout
# is 600s (terraform aws_lambda_timeout_1), and at the Autotrader rate limit of
# 60/sec even 5,000 vehicles is ~85 seconds at BEST — much slower if Autotrader
# pushes back, which would lose the whole run to a timeout.
#
# So a run is split into chunks. Each invocation processes one chunk, appends
# its rows, then invokes itself for the next; only the final chunk builds the
# XLSX. No invocation goes near the timeout however long the list is.
#
# _MAX_VRMS is 5,000 by choice: bigger files are split by hand, which keeps each
# run short enough to watch and to re-run cheaply if something looks wrong.
# _CHUNK_SIZE is deliberately HALF that, so a full 5,000 run is two chunks
# rather than one. The chaining is then used by every large run instead of being
# a rarely-taken path that only wakes up at the limit, and a slow patch costs
# half as much work.
#
# _CHUNK_DEADLINE_SECONDS is the belt and braces: a chunk still going at that
# point stops early and hands its remainder to the next invocation, so nothing
# times out even if throughput collapses.
_MAX_VRMS = 5000
_CHUNK_SIZE = 2500
_CHUNK_DEADLINE_SECONDS = 360
_WORKERS = 30
_RETRY_PAUSE_SECONDS = 5
_STALE_RUNNING_MINUTES = 20

# How many rows the page is sent. At the 5,000 cap a full run fits comfortably
# inside the 6MB Lambda response limit once gzipped, so this only bites if the
# cap is raised later. The XLSX always holds every row regardless.
_PREVIEW_ROWS = 5000

COLUMNS = ["VRM", "Make", "Model", "Last Keeper Change", "Previous Keeper Change",
           "Previous Owners", "Status"]


def _s3_client():
    # aa-raw-sources-uk is always in eu-west-1 regardless of Lambda region
    return boto3.client('s3', region_name=s3_bucket_region)


def _status_key(run_id: str) -> str:
    return f"{_S3_PREFIX}status_{run_id}.json"


def _input_key(run_id: str) -> str:
    """The run's full VRM list. Held in S3 rather than passed through the Lambda
    payload — 20,000 registrations is far too much to put in an invocation
    event, and every chunk needs to read its own slice anyway."""
    return f"{_S3_PREFIX}input_{run_id}.json"


def _rows_key(run_id: str) -> str:
    """Rows accumulated so far, rewritten at the end of each chunk."""
    return f"{_S3_PREFIX}rows_{run_id}.json"


def _put_json(key: str, value) -> None:
    _s3_client().put_object(
        Bucket=s3_bucketname, Key=key,
        Body=json.dumps(value).encode('utf-8'), ContentType='application/json',
    )


def _get_json(key: str):
    try:
        obj = _s3_client().get_object(Bucket=s3_bucketname, Key=key)
        return json.loads(obj['Body'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        raise


def _claim_chunk(run_id: str, slice_start: int) -> bool:
    """Claim the slice starting at slice_start, returning False if another
    invocation already has it.

    AWS delivers an async invocation at least once, so the same chunk can arrive
    twice. Both copies would read the same accumulated rows and both append,
    duplicating every vehicle in the slice. The claim marker is written with
    IfNoneMatch, which S3 rejects if the object already exists, so exactly one
    invocation proceeds.
    """
    key = f"{_S3_PREFIX}claim_{run_id}_{slice_start}.json"
    try:
        _s3_client().put_object(
            Bucket=s3_bucketname, Key=key,
            Body=json.dumps({'claimed_at': _now_iso()}).encode('utf-8'),
            ContentType='application/json',
            IfNoneMatch='*',
        )
        return True
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code')
        if code in ('PreconditionFailed', 'ConditionalRequestConflict'):
            return False
        # An older S3 endpoint may not support IfNoneMatch. Losing the guard is
        # better than failing the run — a duplicate delivery is rare and the
        # row-count check above already catches the common case.
        logger.warning('Chunk claim for %s@%d could not be written (%s) - continuing',
                       run_id, slice_start, code)
        return True


def _cleanup_run_files(run_id: str) -> None:
    """Drop the input list, accumulated rows and chunk claims once the XLSX
    exists. The status record stays — it holds the finished rows the page
    renders."""
    s3 = _s3_client()
    keys = [_input_key(run_id), _rows_key(run_id)]
    try:
        resp = s3.list_objects_v2(Bucket=s3_bucketname, Prefix=f"{_S3_PREFIX}claim_{run_id}_")
        keys.extend(o['Key'] for o in resp.get('Contents', []))
    except Exception as e:
        logger.warning('Could not list chunk claims for %s: %s', run_id, e)
    for key in keys:
        try:
            s3.delete_object(Bucket=s3_bucketname, Key=key)
        except Exception as e:
            logger.warning('Could not remove %s: %s', key, e)


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


# ── worker credential ─────────────────────────────────────────────────────────
# Shared with the SharePoint download worker; see core.worker_secret.
_worker_secret = worker_secret
_check_worker_secret = check_worker_secret


# ── run state ─────────────────────────────────────────────────────────────────

def _status_counts(rows: list) -> dict:
    """Tally by Status over EVERY row. Computed here, not in the browser, because
    the page is only sent a preview of a large run and would otherwise report
    counts for the first _PREVIEW_ROWS alone."""
    idx = COLUMNS.index("Status")
    counts = {'OK': 0, 'NO_HISTORY': 0, 'NOT_FOUND': 0, 'ERROR': 0}
    for row in rows:
        key = row[idx]
        if key in counts:
            counts[key] += 1
    return counts


def _write_state(run_id: str, status: str, *, done: int = 0, total: int = 0,
                 user: str = None, filename: str = None, error: str = None,
                 columns: list = None, rows: list = None,
                 started_at: str = None, eta_s: float = None,
                 counts: dict = None) -> dict:
    """Persist a run's state to S3. This is the only progress store — there is
    no in-memory copy, because the poller and the worker are different Lambda
    invocations and an in-memory dict would make a container report whatever run
    it happened to handle last."""
    pct = round(done / total * 100, 1) if total else 0
    body = {
        'run_id': run_id, 'status': status, 'user': user,
        'pct': 100 if status in ('done', 'error') else pct,
        'done': done, 'total': total, 'eta_s': eta_s,
        'complete': status in ('done', 'error'),
        'filename': filename, 'error': error,
        'columns': columns, 'rows': rows, 'counts': counts,
        'started_at': started_at, 'updated_at': _now_iso(),
    }
    _s3_client().put_object(
        Bucket=s3_bucketname, Key=_status_key(run_id),
        Body=json.dumps(body).encode('utf-8'), ContentType='application/json',
    )
    return body


def _read_state(run_id: str) -> dict | None:
    try:
        obj = _s3_client().get_object(Bucket=s3_bucketname, Key=_status_key(run_id))
        return json.loads(obj['Body'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        raise


def _mark_failed_if_stale(state: dict) -> dict:
    """Flip an orphaned 'running' run to 'error'. Without this the page polls a
    dead run forever with the Run button disabled and nothing explaining why."""
    if state.get('status') != 'running':
        return state
    try:
        updated_at = datetime.datetime.fromisoformat(state['updated_at'])
    except (KeyError, TypeError, ValueError):
        return state
    age = datetime.datetime.utcnow() - updated_at
    if age < datetime.timedelta(minutes=_STALE_RUNNING_MINUTES):
        return state
    logger.warning('Keeper change run %s stale (no updates for %s) - marking failed',
                   state.get('run_id'), age)
    return _write_state(
        state['run_id'], 'error', done=state.get('done') or 0, total=state.get('total') or 0,
        user=state.get('user'), started_at=state.get('started_at'),
        error=f'Run stalled - no progress for over {_STALE_RUNNING_MINUTES} minutes '
              '(likely a server restart or timeout)',
    )


# ── page ──────────────────────────────────────────────────────────────────────

@router.get("/keeper-change-check", response_class=HTMLResponse)
def keeper_change_page(request: Request, user: str = Depends(require_page('keeper_change'))):
    _track(request, 'page_view', '/keeper-change-check')
    return templates.TemplateResponse("keeper_change_check.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL, "max_vrms": _MAX_VRMS,
    })


@router.post("/keeper-change-check/process")
def keeper_change_process(request: Request, payload: dict,
                          user: str = Depends(require_page_api('keeper_change'))):
    vrms = _clean_vrms(payload.get("vrms") or [])
    if not vrms:
        raise HTTPException(status_code=400, detail="No VRMs provided")
    if len(vrms) > _MAX_VRMS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many vehicles ({len(vrms):,}). The limit is {_MAX_VRMS:,} per run - "
                   f"split the file into batches of up to {_MAX_VRMS:,} and run them "
                   "one after another.",
        )

    run_id = uuid.uuid4().hex
    started_at = _now_iso()
    total = len(vrms)
    _track(request, 'keeper_change', '/keeper-change-check', {'vrm_count': total})

    # The list goes to S3, not into the dispatch payload — each chunk reads its
    # own slice from here.
    _put_json(_input_key(run_id), vrms)
    _put_json(_rows_key(run_id), [])

    # Persisted BEFORE dispatch, so a dispatch that never lands still leaves the
    # page something to poll rather than a 404 it cannot interpret.
    _write_state(run_id, 'running', done=0, total=total, user=user, started_at=started_at)

    aws_context = request.scope.get("aws.context")
    try:
        _dispatch_chunk(run_id, 0, user, started_at, aws_context)
    except Exception as e:
        logger.exception('Keeper change dispatch failed for run %s', run_id)
        _write_state(run_id, 'error', total=total, user=user, started_at=started_at,
                     error=f'Could not start the run: {e}')
        raise HTTPException(status_code=500, detail="Could not start the run")

    return {"run_id": run_id, "total": total}


def _dispatch_chunk(run_id: str, chunk: int, user: str, started_at: str,
                    aws_context=None) -> None:
    """Start one chunk, in its own Lambda invocation on dev/prod.

    The payload carries only the run id and which chunk to do — never the VRMs,
    which live in S3. A chunk hands off to the next by calling this again with
    its own aws_context, so the run walks itself to the end without any single
    invocation approaching the 600s timeout.
    """
    if _IS_LOCAL or aws_context is None:
        # Not on Lambda (local uvicorn/docker — aws.context is only set by
        # Mangum). simulate_http would run the worker inline and block the
        # caller, so a thread keeps it responsive, matching dev/prod. No worker
        # credential is fetched: nothing crosses a route, so there is nothing to
        # authenticate, and the page runs locally without the secret existing.
        threading.Thread(
            target=_run_chunk, daemon=True,
            kwargs={'run_id': run_id, 'chunk': chunk, 'user': user,
                    'started_at': started_at, 'aws_context': None},
        ).start()
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/keeper-change-check/run",
            method="POST",
            data={'run_id': run_id, 'chunk': chunk, 'user': user,
                  'started_at': started_at, 'secret': _worker_secret()},
        )


@router.post("/keeper-change-check/run")
def keeper_change_run(data: dict, request: Request):
    """Internal worker route, invoked by our own Lambda (dev/prod only — local
    dev uses the thread in _dispatch_chunk). Guarded by a shared secret rather
    than a session, since the caller is the Lambda itself."""
    _check_worker_secret(data.get('secret'))
    _run_chunk(run_id=data['run_id'], chunk=int(data.get('chunk') or 0),
               user=data.get('user'), started_at=data.get('started_at'),
               aws_context=request.scope.get("aws.context"))
    return {"ok": True}


@router.get("/keeper-change-check/progress/{run_id}")
def keeper_change_progress(run_id: str, user: str = Depends(require_page_api('keeper_change'))):
    state = _read_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    state = _mark_failed_if_stale(state)

    # A 20,000 row payload is several MB and would both strain the 6MB Lambda
    # response limit and lock the browser up rendering it. The preview is enough
    # to see the run worked; the XLSX has every row.
    rows = state.get('rows')
    if rows and len(rows) > _PREVIEW_ROWS:
        state = dict(state)
        state['rows'] = rows[:_PREVIEW_ROWS]
        state['rows_truncated'] = True
        state['rows_total'] = len(rows)
    return state


# ── the work ──────────────────────────────────────────────────────────────────

def _clean_vrms(raw: list) -> list:
    """Normalise and de-duplicate, keeping the submitted order. Applied
    server-side as well as in the browser — the page is not the only way to
    reach this route."""
    seen = set()
    out = []
    for v in raw:
        vrm = str(v or '').strip().upper().replace(' ', '')
        if not vrm or vrm == '0' or vrm in seen:
            continue
        seen.add(vrm)
        out.append(vrm)
    return out


def _run_chunk(run_id: str, chunk: int, user: str = None, started_at: str = None,
               aws_context=None) -> None:
    """Process one chunk of a run, then hand off to the next.

    Each call does at most _CHUNK_SIZE vehicles, appends its rows to the run's
    accumulated set, and invokes the next chunk. Only the last one builds the
    XLSX. That way a 20,000 vehicle run is a chain of short invocations, none of
    which can hit the 600s Lambda timeout.

    A repeated delivery must not redo work or overwrite a finished run, so the
    persisted state and the row count are both checked first.
    """
    state = _read_state(run_id)
    if state is None:
        logger.warning('Keeper change: no state for run %s - refusing to start', run_id)
        return
    if state.get('status') in ('done', 'error'):
        logger.info('Keeper change: run %s already %s - ignoring repeat delivery',
                    run_id, state.get('status'))
        return

    vrms = _get_json(_input_key(run_id))
    if vrms is None:
        logger.error('Keeper change: input list missing for run %s', run_id)
        _write_state(run_id, 'error', total=state.get('total') or 0, user=user,
                     started_at=started_at, error='The run input could not be read')
        return

    total = len(vrms)
    rows = _get_json(_rows_key(run_id)) or []

    # The accumulated row count IS the position in the list - each row is one
    # vehicle, in order. Using it rather than chunk * _CHUNK_SIZE keeps the run
    # correct when a chunk stops early at the deadline and leaves a partial
    # slice behind. The chunk number is only carried for logging and for the
    # repeat-delivery check below.
    slice_start = len(rows)
    slice_vrms = vrms[slice_start:slice_start + _CHUNK_SIZE]
    if not slice_vrms:
        # Everything is already fetched. Either a duplicate delivery arriving
        # after the run finished, or the final chunk's dispatch racing its own
        # completion — nothing left to do either way.
        logger.info('Keeper change: nothing left for chunk %d of run %s', chunk, run_id)
        return

    if not _claim_chunk(run_id, slice_start):
        logger.info('Keeper change: slice %d of run %s already claimed - ignoring repeat',
                    slice_start, run_id)
        return

    from clients.autotrader_client import AutoTraderClient

    start = time.time()
    results = [None] * len(slice_vrms)
    retryable = {}
    done_in_chunk = 0
    last_write = [0.0]
    write_lock = threading.Lock()
    stopped_early = False

    try:
        client = AutoTraderClient()
        # A backoff left over from an earlier chunk should not slow this one
        # before it has even seen a 429.
        client.reset_history_backoff()
        session = requests.Session()
        headers = {'Authorization': f'Bearer {client.get_access_token(session)}'}

        def fetch(idx_vrm):
            idx, vrm = idx_vrm
            cached = client._history_cached(vrm)
            if cached is not None:
                return idx, vrm, cached, 200
            payload, status = client.send_vehicles_history_request(session, headers, vrm)
            if status == 200 and payload is not None:
                client._history_store(vrm, payload)
            return idx, vrm, payload, status

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {pool.submit(fetch, (i, v)): i for i, v in enumerate(slice_vrms)}
            for fut in as_completed(futures):
                if fut.cancelled():
                    # Cancelled at the deadline below. Its vehicle simply has no
                    # row yet, and the next chunk picks it up.
                    continue
                idx, vrm, payload, status = fut.result()
                results[idx] = _build_row(vrm, payload, status)
                # A 404 is a real answer about the vehicle - never retry it.
                # Anything else that failed gets one retry below.
                if status not in (200, 404):
                    retryable[idx] = vrm
                done_in_chunk += 1

                done_overall = len(rows) + done_in_chunk
                elapsed = time.time() - start
                rate = done_in_chunk / elapsed if elapsed > 0 else 0
                eta_s = round((total - done_overall) / rate, 1) if rate > 0 else None

                now = time.time()
                if now - last_write[0] >= 2.0:
                    with write_lock:
                        last_write[0] = now
                        _write_state(run_id, 'running', done=done_overall, total=total,
                                     user=user, started_at=started_at, eta_s=eta_s)

                # Stop early if this chunk is taking too long - whatever is left
                # goes to the next invocation rather than risking the timeout.
                if elapsed > _CHUNK_DEADLINE_SECONDS and not stopped_early:
                    stopped_early = True
                    logger.warning('Keeper change: chunk %d of run %s hit the deadline at '
                                   '%d/%d - handing the rest on', chunk, run_id,
                                   done_in_chunk, len(slice_vrms))
                    for f in futures:
                        f.cancel()

        if retryable and not stopped_early:
            logger.warning('[AT history] Retrying %d VRMs after %ds',
                           len(retryable), _RETRY_PAUSE_SECONDS)
            time.sleep(_RETRY_PAUSE_SECONDS)
            for idx, vrm in retryable.items():
                payload, status = client.send_vehicles_history_request(session, headers, vrm)
                if status == 200 and payload is not None:
                    client._history_store(vrm, payload)
                results[idx] = _build_row(vrm, payload, status)

        # Only the unbroken run of results from the start of the slice can be
        # kept. The next chunk resumes at len(rows), which assumes row N is
        # vehicle N — so a gap in the middle (a vehicle cancelled at the
        # deadline while later ones finished) would silently shift every
        # following row onto the wrong vehicle. Anything after the first gap is
        # dropped and simply fetched again by the next chunk.
        kept = 0
        for r in results:
            if r is None:
                break
            kept += 1
        if kept < len(results):
            logger.info('Keeper change: chunk %d of run %s keeping %d of %d '
                        '(rest handed to the next chunk)',
                        chunk, run_id, kept, len(results))
        rows.extend(results[:kept])
        _put_json(_rows_key(run_id), rows)

        if len(rows) >= total:
            filename = _build_and_upload(rows, user)
            _write_state(run_id, 'done', done=total, total=total, user=user,
                         started_at=started_at, filename=filename,
                         columns=COLUMNS, rows=rows, eta_s=0,
                         counts=_status_counts(rows))
            _cleanup_run_files(run_id)
            logger.info('Keeper change run %s complete: %d vehicles', run_id, total)
        else:
            _write_state(run_id, 'running', done=len(rows), total=total,
                         user=user, started_at=started_at)
            logger.info('Keeper change run %s: %d/%d done, dispatching chunk %d',
                        run_id, len(rows), total, chunk + 1)
            _dispatch_chunk(run_id, chunk + 1, user, started_at, aws_context)

    except Exception as e:
        logger.exception('Keeper change run %s failed in chunk %d', run_id, chunk)
        _write_state(run_id, 'error', done=len(rows), total=total, user=user,
                     started_at=started_at, error=str(e))


def _build_row(vrm: str, payload, status) -> list:
    """One result row, in COLUMNS order.

    Status values: OK (history returned), NO_HISTORY (the call succeeded but the
    vehicle has no recorded keeper changes), NOT_FOUND (Autotrader does not know
    this VRM), ERROR (the call failed even after the retry).
    """
    from clients.autotrader_client import _unwrap_vehicles_result, keeper_change_dates

    if status == 404:
        return [vrm, "", "", "", "", "", "NOT_FOUND"]
    if status != 200 or payload is None:
        return [vrm, "", "", "", "", "", "ERROR"]

    result = _unwrap_vehicles_result(payload) or {}
    vehicle = result.get('vehicle') or {}
    history = result.get('history') or {}
    dates = keeper_change_dates(payload)

    previous_owners = history.get('previousOwners')
    return [
        vrm,
        vehicle.get('make') or "",
        vehicle.get('model') or "",
        _to_ddmmyyyy(dates[0]) if len(dates) > 0 else "",
        _to_ddmmyyyy(dates[1]) if len(dates) > 1 else "",
        "" if previous_owners is None else str(previous_owners),
        "OK" if dates else "NO_HISTORY",
    ]


def _to_ddmmyyyy(iso_date: str) -> str:
    """Autotrader sends ISO dates; the page and the sheet both show UK format."""
    if not iso_date:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(iso_date[:len(fmt) + 2].rstrip('Z'), fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(iso_date.replace('Z', '+00:00')).strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def _build_and_upload(rows: list, user: str) -> str:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from utils.file_handler import DEFAULT_FONT_NAME, set_default_font

    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"keeper_change_{ts_str}.xlsx"
    s3_key = _S3_PREFIX + filename

    wb = openpyxl.Workbook()
    set_default_font(wb)
    ws = wb.active
    ws.title = "Keeper Change Check"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(name=DEFAULT_FONT_NAME, bold=True)
    ok_fill = PatternFill("solid", fgColor="C6EFCE")
    bad_fill = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    status_col = COLUMNS.index("Status") + 1

    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for row in rows:
        ws.append(row)
        for col_idx, cell in enumerate(ws[ws.max_row], start=1):
            cell.border = border
            if col_idx == status_col:
                if cell.value == "OK":
                    cell.fill = ok_fill
                elif cell.value in ("NOT_FOUND", "ERROR"):
                    cell.fill = bad_fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 40)

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        wb.save(tmp_path)
        metadata = {"vrm_count": str(len(rows)), "processed_by": user or ""}
        with open(tmp_path, 'rb') as f:
            _s3_client().upload_fileobj(f, s3_bucketname, s3_key, ExtraArgs={"Metadata": metadata})
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return filename


# ── history and download ──────────────────────────────────────────────────────

@router.get("/keeper-change-check/history")
def keeper_change_history(user: str = Depends(require_page_api('keeper_change'))):
    """Previous runs, newest first. Download-only — the results themselves are
    not re-rendered, so only the date, who ran it and how many vehicles."""
    try:
        s3 = _s3_client()
        resp = s3.list_objects_v2(Bucket=s3_bucketname, Prefix=_S3_PREFIX)
        objects = [o for o in resp.get('Contents', []) if o['Key'].endswith('.xlsx')]
        objects.sort(key=lambda o: o['LastModified'], reverse=True)
        objects = objects[:20]

        def _metadata(key):
            try:
                m = s3.head_object(Bucket=s3_bucketname, Key=key).get('Metadata', {})
                return {
                    "vrm_count": int(m['vrm_count']) if m.get('vrm_count') else None,
                    "processed_by": m.get('processed_by') or None,
                }
            except Exception:
                return {"vrm_count": None, "processed_by": None}

        with ThreadPoolExecutor(max_workers=10) as pool:
            metas = list(pool.map(lambda o: _metadata(o['Key']), objects))

        return [
            {
                "filename": o['Key'].replace(_S3_PREFIX, ''),
                "ts": o['LastModified'].strftime("%d/%m/%Y %H:%M"),
                "vrm_count": meta["vrm_count"],
                "processed_by": meta["processed_by"],
            }
            for o, meta in zip(objects, metas)
        ]
    except Exception as e:
        logger.warning("Keeper change history listing failed: %s", e)
        return []


@router.get("/keeper-change-check/download/{filename}")
def keeper_change_download(filename: str, user: str = Depends(require_page_api('keeper_change'))):
    if '/' in filename or '\\' in filename or not filename.endswith('.xlsx'):
        raise HTTPException(status_code=400, detail="Invalid filename")
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, _S3_PREFIX + filename)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="File not found in S3")
    # Both the media type and the explicit header are needed — without them
    # Chrome saves the file with no extension.
    return FileResponse(
        tmp_path, filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
