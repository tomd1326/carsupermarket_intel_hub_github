"""Settings > Live Update: pull API endpoints, rebuild only the dbt models
between them and a target table, and check each step.

A recipe (live_update_recipes) is a name, the endpoints to pull, the target
model and how many hours back to pull. The models to run are worked out from
the model list (dbt_runs.load_lineage): everything downstream of the raw
models that read the endpoints' S3 folders and upstream of the target.

A run (live_update_runs) goes through stages, each moved on by the page's
poll (no poll, no progress - reopening the page picks it up again):
  api          the Nexus job (api_requests) is pulling the endpoints
  check_api    claimed once: check the pull, then start the dbt steps
  dbt          the dbt chain (dbt_runs) is running, one step per project
  check        claimed once: check the new data reached the raw tables and
               every table was rebuilt
  done
Each move to the next stage is an UPDATE ... WHERE stage = <old> RETURNING,
so two pages polling the same run cannot start anything twice.

Pages show a run with static/js/live_update.js. The Apex Missing Finance
report has a Live Update button that runs the 'apex_missing_finance' recipe.
"""
import datetime
import json
import logging
import re
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from utils.db_utils import get_database_connection
from utils.snowflake_client import SnowflakeClient
from .core import get_current_user, ADMIN_USERNAME
from . import api_requests, dbt_runs

logger = logging.getLogger(__name__)

router = APIRouter()

UK = ZoneInfo('Europe/London')
SNOWFLAKE_DB = 'PROD_CARSUPERMARKET_DB'   # dbt runs always write to PROD (dbt_runs._env)
LINEAGE_WARN_DAYS = 7
# A claimed check stage that has not moved on in this long crashed part way.
STUCK_MINUTES = 5

# Fills the recipe list the first time it is opened.
DEFAULT_RECIPES = [
    {'recipe_key': 'apex_missing_finance', 'name': 'Apex Missing Finance', 'source': 'nexus',
     'endpoints': ['invoices', 'invoices/invoice-finance-amounts'],
     'target_model': 'missing_finance_amounts__p', 'lookback_hours': 3},
]

FINISHED = ('done', 'failed', 'stopped')


def _admin(user: str):
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Forbidden")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(d):
    return d.isoformat() if d else None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS live_update_recipes ("
            " recipe_key VARCHAR(60) PRIMARY KEY,"
            " name VARCHAR(100) NOT NULL,"
            " source VARCHAR(30) NOT NULL DEFAULT 'nexus',"
            " endpoints TEXT[] NOT NULL,"
            " target_model VARCHAR(200) NOT NULL,"
            " lookback_hours INTEGER NOT NULL DEFAULT 3,"
            " sort_order INTEGER NOT NULL DEFAULT 0)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS live_update_runs ("
            " run_id VARCHAR(36) PRIMARY KEY,"
            " recipe_key VARCHAR(60) NOT NULL,"
            " name VARCHAR(100) NOT NULL,"
            " status VARCHAR(10) NOT NULL,"
            " stage VARCHAR(12) NOT NULL,"
            " message TEXT,"
            " since VARCHAR(30),"
            " plan JSONB NOT NULL,"
            " steps JSONB NOT NULL,"
            " api_job_id VARCHAR(36),"
            " dbt_chain_id VARCHAR(36),"
            " dbt_started_at TIMESTAMPTZ,"
            " stop_requested BOOLEAN NOT NULL DEFAULT FALSE,"
            " created_by VARCHAR(100),"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " stage_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " finished_at TIMESTAMPTZ)"
        )
        cur.execute("SELECT COUNT(*) FROM live_update_recipes")
        if cur.fetchone()[0] == 0:
            for i, r in enumerate(DEFAULT_RECIPES):
                cur.execute("INSERT INTO live_update_recipes (recipe_key, name, source, endpoints, target_model,"
                            " lookback_hours, sort_order) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (r['recipe_key'], r['name'], r['source'], r['endpoints'], r['target_model'],
                             r['lookback_hours'], i * 10))
    conn.commit()


_RECIPE_COLS = ['recipe_key', 'name', 'source', 'endpoints', 'target_model', 'lookback_hours', 'sort_order']
_RUN_COLS = ['run_id', 'recipe_key', 'name', 'status', 'stage', 'message', 'since', 'plan', 'steps',
             'api_job_id', 'dbt_chain_id', 'dbt_started_at', 'stop_requested', 'created_by', 'created_at',
             'stage_at', 'finished_at']


def _load_recipes(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RECIPE_COLS)} FROM live_update_recipes ORDER BY sort_order, name")
        return [dict(zip(_RECIPE_COLS, r), endpoints=list(r[3] or [])) for r in cur.fetchall()]


def _load_run(conn, run_id: str):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM live_update_runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return dict(zip(_RUN_COLS, row)) if row else None


def open_live_update(conn) -> str:
    """The name of a Live Update still going, or ''. The manual API and dbt
    tools call this so they do not start in the middle of one."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('live_update_runs')")
        if cur.fetchone()[0] is None:
            conn.rollback()
            return ''
        cur.execute("SELECT name FROM live_update_runs WHERE status = 'running' ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
    conn.rollback()
    return row[0] if row else ''


def _open_run_id(conn, recipe_key: str = None):
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM live_update_runs WHERE status = 'running'"
                    + (" AND recipe_key = %s" if recipe_key else "") + " ORDER BY created_at DESC LIMIT 1",
                    (recipe_key,) if recipe_key else ())
        row = cur.fetchone()
    return row[0] if row else None


def _save_steps(conn, run: dict, stage: str = None) -> None:
    """Write the step list, only while the run is still at the stage it was
    read at, so a slower poll cannot write over a newer one."""
    with conn.cursor() as cur:
        cur.execute("UPDATE live_update_runs SET steps = %s WHERE run_id = %s AND stage = %s AND status = 'running'",
                    (json.dumps(run['steps']), run['run_id'], stage or run['stage']))
    conn.commit()


def _claim(conn, run: dict, from_stage: str, to_stage: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE live_update_runs SET stage = %s, stage_at = NOW() "
                    "WHERE run_id = %s AND stage = %s AND status = 'running' RETURNING run_id",
                    (to_stage, run['run_id'], from_stage))
        ok = cur.fetchone() is not None
    conn.commit()
    if ok:
        run['stage'] = to_stage
    return ok


def _set_step(run: dict, key: str, status: str, result: str = None) -> None:
    step = next((s for s in run['steps'] if s['key'] == key), None)
    if not step:
        return
    now = _iso(_now())
    if status == 'running' and not step.get('started_at'):
        step['started_at'] = now
    if status in ('done', 'failed', 'warning', 'stopped', 'skipped') and step['status'] not in (
            'done', 'failed', 'warning', 'stopped', 'skipped'):
        step['started_at'] = step.get('started_at') or now
        step['finished_at'] = now
    step['status'] = status
    if result is not None:
        step['result'] = result


def _finish(conn, run: dict, status: str, message: str) -> None:
    """End the run. Steps that never got going show as Not Run."""
    for s in run['steps']:
        if s['status'] in ('waiting', 'running') and s['key'] != 'reload':
            _set_step(run, s['key'], 'skipped' if s['status'] == 'waiting' else status,
                      s.get('result') or ('' if s['status'] == 'waiting' else message))
    if status != 'done':
        _set_step(run, 'reload', 'skipped')
    with conn.cursor() as cur:
        cur.execute("UPDATE live_update_runs SET status = %s, stage = 'done', message = %s, steps = %s, "
                    "finished_at = NOW(), stage_at = NOW() WHERE run_id = %s AND status = 'running'",
                    (status, message[:2000], json.dumps(run['steps']), run['run_id']))
    conn.commit()
    run.update(status=status, stage='done', message=message)
    logger.info("Live Update %s (%s) finished: %s - %s", run['run_id'], run['name'], status, message)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _same_folder(stage_url: str, path: str) -> bool:
    a, b = (stage_url or '').strip('/'), path.strip('/')
    return bool(a) and (a == b or a.startswith(b + '/'))


def build_plan(conn, recipe: dict) -> dict:
    """The endpoints and dbt steps a recipe runs. `errors` lists anything that
    stops it running (e.g. an endpoint no model reads)."""
    lineage = dbt_runs.load_lineage(conn)
    models = dbt_runs.with_children(lineage['models'])
    cfg = api_requests._source(recipe['source'])
    target = recipe['target_model']
    errors, endpoints, chosen = [], [], set()
    if target not in models:
        errors.append(f"{target} is not in the model list. " + dbt_runs.OUT_OF_DATE)
    else:
        upstream = dbt_runs.walk(models, target, 'parents') | {target}
        for route in recipe['endpoints']:
            path = cfg['stage_path'](route)
            starts = sorted(n for n, m in models.items() if _same_folder(m.get('stage_url'), path))
            ep = {'route': route, 'folder': path, 'models': starts, 'check_table': None}
            if not starts:
                errors.append(f"No model reads {path} (endpoint {route}). " + dbt_runs.OUT_OF_DATE)
            else:
                down = set(starts)
                for s in starts:
                    down |= dbt_runs.walk(models, s, 'children')
                path_models = down & upstream
                if not path_models:
                    errors.append(f"{route} does not feed {target}: none of the models reading it lead there")
                chosen |= path_models
                # The table to check the pulled rows reached: the raw model
                # furthest along that is a stored table (the __s one).
                raw = sorted((n for n in path_models if models[n]['project'] == 'raw'
                              and models[n]['materialized'] not in ('view', 'ephemeral')),
                             key=lambda n: (not n.endswith('__s'), n))
                if raw:
                    m = models[raw[0]]
                    ep['check_table'] = {'model': raw[0], 'schema': m['schema'], 'table': m['table']}
            endpoints.append(ep)
    jobs = dbt_runs._load_jobs(conn)
    steps = dbt_runs.steps_for(models, chosen, jobs, target) if chosen else []
    for s in steps:
        if not s['job_id']:
            errors.append(f"No dbt job for the {s['project']} project - add one in Manual dbt")
    checked = lineage.get('checked_at')
    old = (lineage['source'] != 'recheck'
           or (checked and _now() - checked > datetime.timedelta(days=LINEAGE_WARN_DAYS)))
    return {'recipe_key': recipe['recipe_key'], 'target': target, 'endpoints': endpoints, 'steps': steps,
            'errors': errors, 'lineage': dbt_runs._lineage_info(lineage), 'lineage_old': bool(old)}


def _initial_steps(plan: dict, since: str, reload: bool) -> list:
    steps = [{'key': f"api:{e['route']}", 'label': f"Pull {e['route']}",
              'detail': f"Nexus Point, updated since {since.replace('T', ' ')}", 'status': 'waiting'}
             for e in plan['endpoints']]
    steps.append({'key': 'check_api', 'label': 'Check The Pull',
                  'detail': 'Every endpoint worked and at least one returned rows', 'status': 'waiting'})
    for i, s in enumerate(plan['steps']):
        steps.append({'key': f"dbt:{i}", 'label': f"dbt {s['project'].title()} ({len(s['models'])} Models)",
                      'detail': ', '.join(m['name'] for m in s['models']), 'status': 'waiting', 'chain_pos': i})
    steps.append({'key': 'check_data', 'label': 'Check The New Data Reached Snowflake',
                  'detail': 'Newest updated_at in each raw table is at least the newest pulled',
                  'status': 'waiting'})
    steps.append({'key': 'check_tables', 'label': 'Check The Tables Were Rebuilt',
                  'detail': 'Every table in the run was changed after dbt started', 'status': 'waiting'})
    if reload:
        steps.append({'key': 'reload', 'label': 'Reload The Report Data', 'detail': '', 'status': 'waiting'})
    return steps


# ---------------------------------------------------------------------------
# Moving a run on
# ---------------------------------------------------------------------------

def _api_stage(conn, run: dict) -> dict:
    job = api_requests.load_job(conn, run['api_job_id']) if run['api_job_id'] else None
    if not job:
        _finish(conn, run, 'failed', 'The API job was not found')
        return {}
    results = job['results']
    for i, route in enumerate(job['routes']):
        key = f"api:{route}"
        res = results.get(route)
        if res is not None:
            if res.get('error'):
                _set_step(run, key, 'failed', res['error'])
            else:
                _set_step(run, key, 'done', f"{res.get('rows', 0)} rows saved" if res.get('rows')
                          else 'No rows changed in the window - nothing saved')
        elif job['status'] in ('queued', 'running') and i == job['claimed_index']:
            last = next((l for l in reversed(job['log']) if route in l or 'page' in l), '')
            _set_step(run, key, 'running', last.strip()[-200:])
    if job['status'] in ('queued', 'running'):
        _save_steps(conn, run, 'api')
        return job
    if not _claim(conn, run, 'api', 'check_api'):
        return job
    _after_api(conn, run, job)
    return job


def _after_api(conn, run: dict, job: dict) -> None:
    """Claimed once: check the pull, then start the dbt steps."""
    try:
        results = job['results']
        routes = job['routes']
        if job['status'] == 'stopped' or run['stop_requested']:
            _finish(conn, run, 'stopped', 'Stopped during the API pull')
            return
        failed = [r for r in routes if (results.get(r) or {}).get('error') or r not in results]
        if failed:
            _set_step(run, 'check_api', 'failed', f"Failed: {', '.join(failed)}")
            _finish(conn, run, 'failed', f"The pull failed for {', '.join(failed)} - see the log. Nothing was rebuilt.")
            return
        total = sum(int((results.get(r) or {}).get('rows') or 0) for r in routes)
        if not total:
            msg = (f"No rows changed in Apex since {(run['since'] or '').replace('T', ' ')}, so there is nothing "
                   "to rebuild. Check the correction was saved in Apex, or use a longer look back.")
            _set_step(run, 'check_api', 'failed', msg)
            _finish(conn, run, 'failed', msg)
            return
        _set_step(run, 'check_api', 'done', f"{total} rows across {len(routes)} endpoint(s)")
        plan = run['plan']
        steps = [{'job_id': s['job_id'], 'select': s['select'], 'expected_models': len(s['models']),
                  'label': f"Live Update {s['project']}"} for s in plan['steps']]
        today = datetime.datetime.now(UK).strftime('%Y-%m-%d')
        try:
            chain_id, _ = dbt_runs.start_chain(conn, steps, run['created_by'], today)
        except ValueError as e:
            _finish(conn, run, 'failed', f"dbt could not start: {e}")
            return
        _set_step(run, 'dbt:0', 'running')
        with conn.cursor() as cur:
            cur.execute("UPDATE live_update_runs SET stage = 'dbt', stage_at = NOW(), dbt_chain_id = %s, "
                        "dbt_started_at = NOW(), steps = %s WHERE run_id = %s",
                        (chain_id, json.dumps(run['steps']), run['run_id']))
        conn.commit()
        run.update(stage='dbt', dbt_chain_id=chain_id)
    except Exception as e:
        logger.exception("Live Update %s: after the pull", run['run_id'])
        _finish(conn, run, 'failed', f"Crashed after the pull: {str(e)[:300]}")


_DBT_STATUS = {'queued': 'waiting', 'waiting': 'running', 'running': 'running', 'failed': 'failed',
               'stopped': 'stopped', 'cancelled': 'skipped'}


def _dbt_stage(conn, run: dict, dbt_run_id: str, token: str) -> dict:
    chain = dbt_runs.load_chain(conn, run['dbt_chain_id'])
    if not chain:
        _finish(conn, run, 'failed', 'The dbt run was not found')
        return {}
    ids = {r['run_id'] for r in chain}
    if dbt_run_id not in ids:
        current = next((r for r in chain if r['status'] in ('waiting', 'running')), None) \
            or next((r for r in chain if r['status'] == 'done' and not (r['checks'] or {}).get('checked')), None) \
            or chain[-1]
        dbt_run_id, token = current['run_id'], ''
    result = dbt_runs.poll_run(conn, dbt_run_id, token)
    result['run_id'] = dbt_run_id

    chain = dbt_runs.load_chain(conn, run['dbt_chain_id'])
    for r in chain:
        checks = r['checks'] or {}
        if r['status'] == 'done':
            status = 'done' if checks.get('checked') else 'running'
            text = (f"{checks['total']} models built" if checks.get('total') is not None else 'Finished') \
                if status == 'done' else 'Reading the end of the log'
        else:
            status = _DBT_STATUS.get(r['status'], r['status'])
            text = r['message'] or ''
            if r['status'] == 'failed' and checks.get('errors'):
                text += '\n' + '\n'.join(checks['errors'][:5])
        _set_step(run, f"dbt:{r['chain_pos']}", status, text)

    open_steps = [r for r in chain if r['status'] in ('queued', 'waiting', 'running')
                  or (r['status'] == 'done' and not (r['checks'] or {}).get('checked'))]
    if open_steps:
        _save_steps(conn, run, 'dbt')
        return result
    bad = next((r for r in chain if r['status'] in ('failed', 'stopped', 'cancelled')), None)
    if bad:
        if _claim(conn, run, 'dbt', 'check'):
            if bad['status'] == 'stopped' or run['stop_requested']:
                _finish(conn, run, 'stopped', 'Stopped during the dbt run')
            else:
                hint = dbt_runs._hint(bad)
                _finish(conn, run, 'failed', f"dbt step {bad['chain_pos'] + 1} ({bad['job_name']}) failed: "
                                             f"{bad['message'] or ''}" + (f" {hint}" if hint else ''))
        return result
    if _claim(conn, run, 'dbt', 'check'):
        _final_checks(conn, run)
    return result


def _aware(d):
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def _final_checks(conn, run: dict) -> None:
    """Claimed once, after every dbt step is done."""
    try:
        plan = run['plan']
        job = api_requests.load_job(conn, run['api_job_id']) or {'results': {}}
        started = _aware(run['dbt_started_at']) or _aware(run['created_at'])
        _set_step(run, 'check_data', 'running')
        with SnowflakeClient() as sf:
            # 1. The rows pulled reached the raw tables.
            lines, status = [], 'done'
            for ep in plan['endpoints']:
                res = job['results'].get(ep['route']) or {}
                ct = ep.get('check_table')
                if not res.get('rows'):
                    lines.append(f"{ep['route']}: no rows pulled, nothing to check")
                    continue
                if not ct or not res.get('max_updated_at'):
                    lines.append(f"{ep['route']}: no raw table or updated_at to check against")
                    status = 'warning' if status == 'done' else status
                    continue
                name = f"{ct['schema']}.{ct['table']}".upper()
                try:
                    rows, _ = sf.execute(f"SELECT MAX(UPDATED_AT), MAX(UPDATED_AT) >= TRY_TO_TIMESTAMP(%s) "
                                         f"FROM {SNOWFLAKE_DB}.{name}", [res['max_updated_at']])
                    newest, ok = rows[0]
                except Exception as e:
                    lines.append(f"{ep['route']}: could not check {name} - {str(e)[:200]}")
                    status = 'warning' if status == 'done' else status
                    continue
                if ok:
                    lines.append(f"{name} has the pulled rows (newest updated_at {newest})")
                elif ok is None:
                    lines.append(f"{name}: could not compare {newest} with the pulled {res['max_updated_at']}")
                    status = 'warning' if status == 'done' else status
                else:
                    lines.append(f"{name} newest updated_at is {newest}, older than the pulled "
                                 f"{res['max_updated_at']} - the new rows did not load")
                    status = 'failed'
            _set_step(run, 'check_data', status, '\n'.join(lines))

            # 2. Every stored table was changed after dbt started.
            _set_step(run, 'check_tables', 'running')
            wanted = {}
            for s in plan['steps']:
                for m in s['models']:
                    if m['materialized'] not in ('view', 'ephemeral'):
                        wanted[(m['schema'].upper(), m['table'].upper())] = m
            found = {}
            if wanted:
                schemas = sorted({k[0] for k in wanted})
                tables = sorted({k[1] for k in wanted})
                rows, _ = sf.execute(
                    f"SELECT TABLE_SCHEMA, TABLE_NAME, LAST_ALTERED FROM {SNOWFLAKE_DB}.INFORMATION_SCHEMA.TABLES "
                    f"WHERE TABLE_SCHEMA IN ({', '.join(['%s'] * len(schemas))}) "
                    f"AND TABLE_NAME IN ({', '.join(['%s'] * len(tables))})", schemas + tables)
                found = {(r[0], r[1]): _aware(r[2]) for r in rows}
            not_found, stale, soft = [], [], []
            for key, m in sorted(wanted.items()):
                altered = found.get(key)
                if altered is None:
                    not_found.append('.'.join(key))
                elif altered < started:
                    # An incremental merge with nothing to change may leave the
                    # time alone, so those are a warning, not a failure.
                    (soft if m['materialized'] in dbt_runs.FULL_REFRESH_KINDS else stale).append('.'.join(key))
            t_status = 'failed' if (not_found or stale) else ('warning' if soft else 'done')
            parts = [f"{len(wanted) - len(not_found) - len(stale) - len(soft)} of {len(wanted)} tables rebuilt"]
            if not_found:
                parts.append(f"Not found: {', '.join(not_found)}. " + dbt_runs.OUT_OF_DATE)
            if stale:
                parts.append(f"Not rebuilt: {', '.join(stale)}")
            if soft:
                parts.append(f"Not changed (incremental, may have had nothing to merge): {', '.join(soft)}")
            _set_step(run, 'check_tables', t_status, '\n'.join(parts))
        failed = [s['label'] for s in run['steps'] if s['key'] in ('check_data', 'check_tables')
                  and s['status'] == 'failed']
        if failed:
            _finish(conn, run, 'failed', f"{' and '.join(failed)} failed - see the steps")
        else:
            _finish(conn, run, 'done', 'Pulled, rebuilt and checked')
    except Exception as e:
        logger.exception("Live Update %s: final checks", run['run_id'])
        _finish(conn, run, 'failed', f"The checks could not run: {str(e)[:300]}")


def _state(run: dict, job_log: list = None, dbt: dict = None) -> dict:
    return {"status": "ok", "run": {
        'run_id': run['run_id'], 'recipe_key': run['recipe_key'], 'name': run['name'], 'status': run['status'],
        'stage': run['stage'], 'message': run['message'] or '', 'since': run['since'], 'steps': run['steps'],
        'created_by': run['created_by'], 'created_at': _iso(run['created_at']),
        'finished_at': _iso(run.get('finished_at')), 'lineage_old': (run['plan'] or {}).get('lineage_old')},
        "api_log": job_log or [], "dbt": dbt}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/live-update/config")
def lu_config(recipe: str = '', user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        api_requests._ensure_tables(conn)
        dbt_runs._ensure_tables(conn)
        recipes = _load_recipes(conn)
        routes = api_requests._load_config(conn, 'nexus')['routes']
        lineage = dbt_runs.load_lineage(conn)
        open_id = _open_run_id(conn, recipe or None)
        with conn.cursor() as cur:
            cur.execute("SELECT run_id, name, status, message, created_by, created_at, finished_at "
                        "FROM live_update_runs ORDER BY created_at DESC LIMIT 15")
            recent = [{'run_id': r[0], 'name': r[1], 'status': r[2], 'message': r[3] or '', 'created_by': r[4],
                       'created_at': _iso(r[5]), 'finished_at': _iso(r[6])} for r in cur.fetchall()]
        conn.rollback()
    return {"status": "ok", "recipes": recipes, "endpoints": routes, "models": sorted(lineage['models']),
            "lineage": dbt_runs._lineage_info(lineage), "open_run_id": open_id, "recent": recent,
            "lineage_warn_days": LINEAGE_WARN_DAYS}


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:60]


@router.post("/api/live-update/recipes")
def lu_save_recipe(data: dict, user: str = Depends(get_current_user)):
    """Add a recipe (no recipe_key) or update one."""
    _admin(user)
    name = str(data.get('name') or '').strip()
    target = str(data.get('target_model') or '').strip()
    endpoints = [api_requests._clean_route(e) for e in data.get('endpoints') or []]
    endpoints = list(dict.fromkeys(e for e in endpoints if e))
    try:
        lookback = int(data.get('lookback_hours') or 3)
    except ValueError:
        return {"status": "error", "message": "Look Back must be a whole number of hours"}
    if not name or len(name) > 100:
        return {"status": "error", "message": "Enter a name of at most 100 characters"}
    if not endpoints:
        return {"status": "error", "message": "Tick at least one endpoint"}
    if not 1 <= lookback <= 720:
        return {"status": "error", "message": "Look Back must be between 1 and 720 hours"}
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        if target not in dbt_runs.load_lineage(conn)['models']:
            return {"status": "error", "message": f"{target or 'The target'} is not in the model list"}
        known = set(api_requests._load_config(conn, 'nexus')['routes'])
        unknown = [e for e in endpoints if e not in known]
        if unknown:
            return {"status": "error", "message": f"Not in the endpoint list: {', '.join(unknown)}"}
        key = str(data.get('recipe_key') or '').strip()
        with conn.cursor() as cur:
            if key:
                cur.execute("UPDATE live_update_recipes SET name = %s, endpoints = %s, target_model = %s, "
                            "lookback_hours = %s WHERE recipe_key = %s", (name, endpoints, target, lookback, key))
            else:
                key = _slug(name) or str(uuid.uuid4())[:8]
                cur.execute("INSERT INTO live_update_recipes (recipe_key, name, source, endpoints, target_model,"
                            " lookback_hours, sort_order) SELECT %s, %s, 'nexus', %s, %s, %s, "
                            " COALESCE(MAX(sort_order), 0) + 10 FROM live_update_recipes ON CONFLICT DO NOTHING",
                            (key, name, endpoints, target, lookback))
                if cur.rowcount != 1:
                    conn.rollback()
                    return {"status": "error", "message": f"A recipe called {name} already exists"}
        conn.commit()
    logger.info("Live Update recipe %s saved by %s", key, user)
    return {"status": "ok", "recipe_key": key}


@router.post("/api/live-update/recipes/delete")
def lu_delete_recipe(data: dict, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM live_update_recipes WHERE recipe_key = %s", (str(data.get('recipe_key') or ''),))
        conn.commit()
    return {"status": "ok"}


def _recipe(conn, key: str) -> dict:
    recipe = next((r for r in _load_recipes(conn) if r['recipe_key'] == key), None)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@router.get("/api/live-update/plan")
def lu_plan(recipe: str, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        dbt_runs._ensure_tables(conn)
        r = _recipe(conn, recipe)
        plan = build_plan(conn, r)
        conn.rollback()
    return {"status": "ok", "recipe": r, "plan": plan}


@router.post("/api/live-update/start")
def lu_start(data: dict, user: str = Depends(get_current_user)):
    """Start a recipe: data {recipe_key, lookback_hours (optional), reload}."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        api_requests._ensure_tables(conn)
        dbt_runs._ensure_tables(conn)
        recipe = _recipe(conn, str(data.get('recipe_key') or ''))
        busy = open_live_update(conn)
        if busy:
            return {"status": "error", "message": f"Live Update {busy} is already going."}
        dbt_busy = dbt_runs._busy_message(conn)
        if dbt_busy:
            return {"status": "error", "message": f"A dbt run is going: {dbt_busy}"}
        plan = build_plan(conn, recipe)
        if plan['errors']:
            return {"status": "error", "message": ' '.join(plan['errors'])}
        try:
            lookback = min(720, max(1, int(data.get('lookback_hours') or recipe['lookback_hours'])))
        except ValueError:
            return {"status": "error", "message": "Look Back must be a whole number of hours"}
        since = (datetime.datetime.now(UK) - datetime.timedelta(hours=lookback)).strftime('%Y-%m-%dT%H:%M')
        source = recipe['source']
        cfg = api_requests._source(source)
        config = api_requests._load_config(conn, source)
        try:
            params = cfg['params']({'updated_since': since, 'lookback_hours': lookback, 'folder': 'v0'}, config)
            job_id = api_requests.create_job(conn, source, recipe['endpoints'], params, user)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        run_id = str(uuid.uuid4())
        steps = _initial_steps(plan, since, bool(data.get('reload')))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO live_update_runs (run_id, recipe_key, name, status, stage, since, plan, steps,"
                        " api_job_id, created_by) VALUES (%s, %s, %s, 'running', 'api', %s, %s, %s, %s, %s)",
                        (run_id, recipe['recipe_key'], recipe['name'], since, json.dumps(plan),
                         json.dumps(steps), job_id, user))
        conn.commit()
    logger.info("Live Update %s (%s) started by %s, updated since %s", run_id, recipe['name'], user, since)
    api_requests._dispatch(job_id, 0)
    return {"status": "ok", "run_id": run_id}


@router.get("/api/live-update/run/{run_id}")
def lu_poll(run_id: str, dbt_run_id: str = '', token: str = '', user: str = Depends(get_current_user)):
    """Move the run on and return every step, the API log, and (in the dbt
    stage) the new log lines of the dbt step `dbt_run_id` after `token`."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        run = _load_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        job, dbt = None, None
        if run['status'] == 'running':
            if run['stage'] == 'api':
                job = _api_stage(conn, run)
            elif run['stage'] == 'dbt':
                dbt = _dbt_stage(conn, run, dbt_run_id, token)
            elif run['stage'] in ('check_api', 'check') and \
                    _now() - _aware(run['stage_at']) > datetime.timedelta(minutes=STUCK_MINUTES):
                _finish(conn, run, 'failed', f"Stopped moving during {run['stage']} - start it again")
        if job is None and run['api_job_id']:
            job = api_requests.load_job(conn, run['api_job_id'])
        run = _load_run(conn, run_id)
        conn.rollback()
    return _state(run, (job or {}).get('log'), dbt)


@router.post("/api/live-update/run/{run_id}/reload")
def lu_reload_done(run_id: str, data: dict, user: str = Depends(get_current_user)):
    """The report page says whether reloading its data worked."""
    _admin(user)
    ok = bool(data.get('ok'))
    with get_database_connection() as conn:
        conn.rollback()
        run = _load_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        _set_step(run, 'reload', 'done' if ok else 'failed', '' if ok else str(data.get('message') or '')[:500])
        with conn.cursor() as cur:
            cur.execute("UPDATE live_update_runs SET steps = %s WHERE run_id = %s",
                        (json.dumps(run['steps']), run_id))
        conn.commit()
    return {"status": "ok"}


@router.post("/api/live-update/run/{run_id}/stop")
def lu_stop(run_id: str, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        run = _load_run(conn, run_id)
        if not run or run['status'] != 'running':
            return {"status": "ok", "message": "Nothing was running"}
        with conn.cursor() as cur:
            cur.execute("UPDATE live_update_runs SET stop_requested = TRUE WHERE run_id = %s", (run_id,))
        conn.commit()
        if run['stage'] == 'api' and run['api_job_id']:
            with conn.cursor() as cur:
                cur.execute("UPDATE api_request_jobs SET stop_requested = TRUE "
                            "WHERE job_id = %s AND status IN ('queued', 'running')", (run['api_job_id'],))
            conn.commit()
            return {"status": "ok", "message": "Stopping - the endpoint being pulled will not be saved"}
        if run['stage'] == 'dbt' and run['dbt_chain_id']:
            try:
                return {"status": "ok", "message": dbt_runs.stop_chain(conn, run['dbt_chain_id'], user)}
            except ValueError as e:
                return {"status": "error", "message": str(e)}
    return {"status": "ok", "message": "Stopping"}
