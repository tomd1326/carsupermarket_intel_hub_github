"""Settings > Live Update (manual dbt card): start dbt ECS tasks from the
website, the same way the Rundeck "ECS run task template" job does, and show
their logs. Live Update (live_update.py) uses the same functions.

Matches the Rundeck script:
- cluster aramis_group_core_prod_cluster, capacity provider
  aramis_group_core_<cp>_workloads_prod_asg_cp, in eu-west-1. Every site
  (prod, dev, local) runs the prod task - see _env()
- the command is split like a shell would (shlex) and sent as a container
  override, with optional memory and SNOWFLAKE_WH_OVERRIDE
- 3 attempts to start: 10s wait after an AWS call error, 300s after a
  RESOURCE/AGENT placement failure
- log stream log_stream/<container>/<task id> in the task definition's log group

Differs from Rundeck on purpose: a step only counts as done when the
container exits with 0 (a missing exit code means it never ran properly), and
its log is checked - dbt finding nothing to run, or running a different
number of models than planned, fails the step because it means the model
list is out of date.

The ECS task runs on its own, so the 10-minute Lambda limit does not apply.
The page polls: each poll checks the task, makes any start retry that is due,
starts the next step of a multi-step run once the previous one has finished,
and returns new log lines. Retries and next steps only happen while the page
is open.

Jobs (name, project, task definition, command...) are edited on the page and
stored in dbt_job_definitions. A command can use {select}, {environment} and
{current_date}, filled in when the run starts.

Model relationships are stored in dbt_lineage, filled by Recheck Lineage: a
`dbt ls` run of the product job (which holds raw and transform as packages),
read back from its log. Until the first recheck, data/dbt_lineage.json (built
by scripts/build_dbt_lineage.py) is used.
"""
import datetime
import json
import logging
import os
import re
import shlex
import uuid
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException

from utils.db_utils import get_database_connection
from utils.ddl_once import once_per_database
from .core import get_current_user, ADMIN_USERNAME

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_ATTEMPTS = 3
CLI_ERROR_RETRY_WAIT = 10
PLACEMENT_RETRY_WAIT = 300

PROJECT_ORDER = ['raw', 'transform', 'product']
PACKAGES = {'uk_dbt_raw_data': 'raw', 'uk_dbt_transform_data': 'transform', 'uk_dbt_product_data': 'product'}

# Fills the job list the first time the page is opened. "dbt raw" is the
# Rundeck job; transform and product follow the same pattern - check their
# commands against Rundeck and edit them on the page if they differ.
_COMMAND = ('run --select {select} --target {environment} --threads 40 '
            '--vars "{current_date: {current_date}}"')
DEFAULT_JOBS = [
    {'name': f'dbt {p}', 'project': p, 'task_definition': f'carsupermarket_dbt_{p}_td',
     'container_name': 'dbt', 'capacity_provider': 'low_intensity', 'command': _COMMAND,
     'default_select': f'uk_dbt_{p}_data,tag:daily', 'memory_reservation': None,
     'snowflake_wh_override': ''}
    for p in PROJECT_ORDER
]

# Materializations that need --full-refresh when a column is added, removed
# or renamed (see uk_dbt.md "Running dbt").
FULL_REFRESH_KINDS = {'incremental', 'from_external_stage'}

OPEN_STATUSES = ('waiting', 'running')
FINISHED_STATUSES = ('done', 'failed', 'stopped', 'cancelled')

# What Recheck Lineage asks dbt ls for.
LS_KEYS = 'name package_name depends_on config schema alias tags'
OUT_OF_DATE = "The model list is out of date - press Recheck Lineage, then try again."

_LINEAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'data', 'dbt_lineage.json')

# dbt's colour codes (ESC[0m etc.), which show as "[0m" on the page.
_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _clean(line: str) -> str:
    return _ANSI.sub('', line or '')


_NOTHING = re.compile(r"Nothing to do|does not match any enabled nodes")
_DONE = re.compile(r"Done\. PASS=(\d+).*?ERROR=(\d+).*?TOTAL=(\d+)")
_ERROR = re.compile(r"(Compilation|Database|Runtime|Parsing|Dependency) Error|Error in model|"
                    r"invalid identifier|does not exist or not authorized|^\s*ERROR\b|\bERROR creating")


def _admin(user: str):
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Forbidden")


def _env() -> str:
    """Always prod. Tom's decision (2026-10-19): dev runs failed with
    TaskDefinition not found in us-east-2, so every caller (prod site, dev
    site, local) uses the prod task. Runs use --target prod and write to
    PROD_CARSUPERMARKET_DB."""
    return 'prod'


def _region(env: str) -> str:
    return 'eu-west-1' if env == 'prod' else 'us-east-2'


def _cluster(env: str) -> str:
    return f"aramis_group_core_{env}_cluster"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS dbt_job_definitions ("
            " job_id SERIAL PRIMARY KEY,"
            " name VARCHAR(100) NOT NULL,"
            " project VARCHAR(20) NOT NULL DEFAULT '',"
            " default_select TEXT NOT NULL DEFAULT '',"
            " task_definition VARCHAR(200) NOT NULL,"
            " container_name VARCHAR(100) NOT NULL,"
            " capacity_provider VARCHAR(100) NOT NULL,"
            " command TEXT NOT NULL,"
            " memory_reservation INTEGER,"
            " snowflake_wh_override VARCHAR(100) NOT NULL DEFAULT '',"
            " sort_order INTEGER NOT NULL DEFAULT 0)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS dbt_runs ("
            " run_id VARCHAR(36) PRIMARY KEY,"
            " chain_id VARCHAR(36) NOT NULL,"
            " chain_pos INTEGER NOT NULL DEFAULT 0,"
            " chain_len INTEGER NOT NULL DEFAULT 1,"
            " job_name VARCHAR(100) NOT NULL,"
            " env VARCHAR(10) NOT NULL,"
            " spec JSONB NOT NULL,"
            " status VARCHAR(10) NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " retry_at TIMESTAMPTZ,"
            " task_arn VARCHAR(300),"
            " log_group VARCHAR(300),"
            " log_stream VARCHAR(300),"
            " exit_code VARCHAR(20),"
            " message TEXT,"
            " start_log TEXT[] NOT NULL DEFAULT '{}',"
            " created_by VARCHAR(100),"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " started_at TIMESTAMPTZ,"
            " finished_at TIMESTAMPTZ)"
        )
        # What the log showed: nothing_to_do, pass/error/total counts, error
        # lines, and checked = the finished step's checks have been made.
        cur.execute("ALTER TABLE dbt_runs ADD COLUMN IF NOT EXISTS checks JSONB NOT NULL DEFAULT '{}'")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS dbt_lineage ("
            " id INTEGER PRIMARY KEY,"
            " checked_at TIMESTAMPTZ NOT NULL,"
            " checked_by VARCHAR(100),"
            " models JSONB NOT NULL)"
        )
        cur.execute("SELECT COUNT(*) FROM dbt_job_definitions")
        if cur.fetchone()[0] == 0:
            for i, job in enumerate(DEFAULT_JOBS):
                cur.execute(
                    "INSERT INTO dbt_job_definitions (name, project, default_select, task_definition,"
                    " container_name, capacity_provider, command, memory_reservation, snowflake_wh_override,"
                    " sort_order) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (job['name'], job['project'], job['default_select'], job['task_definition'],
                     job['container_name'], job['capacity_provider'], job['command'],
                     job['memory_reservation'], job['snowflake_wh_override'], i * 10))
    conn.commit()


_JOB_COLS = ['job_id', 'name', 'project', 'default_select', 'task_definition', 'container_name',
             'capacity_provider', 'command', 'memory_reservation', 'snowflake_wh_override', 'sort_order']


def _load_jobs(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_JOB_COLS)} FROM dbt_job_definitions ORDER BY sort_order, name")
        return [dict(zip(_JOB_COLS, r)) for r in cur.fetchall()]


_RUN_COLS = ['run_id', 'chain_id', 'chain_pos', 'chain_len', 'job_name', 'env', 'status', 'attempts',
             'task_arn', 'exit_code', 'message', 'created_by', 'created_at', 'started_at', 'finished_at',
             'spec', 'log_group', 'log_stream', 'start_log', 'retry_at', 'checks']


def _load_run(conn, run_id: str):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return dict(zip(_RUN_COLS, row)) if row else None


def load_chain(conn, chain_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs WHERE chain_id = %s ORDER BY chain_pos",
                    (chain_id,))
        return [dict(zip(_RUN_COLS, r)) for r in cur.fetchall()]


def _set_run(conn, run_id: str, note: str = None, **sets) -> None:
    sql = f"UPDATE dbt_runs SET {', '.join(k + ' = %s' for k in sets)}"
    values = list(sets.values())
    if note:
        sql += ", start_log = start_log || %s::text[]"
        values.append([note])
    with conn.cursor() as cur:
        cur.execute(sql + " WHERE run_id = %s", values + [run_id])
    conn.commit()


def _links(run: dict) -> dict:
    """ECS console and CloudWatch links, built as the Rundeck script does."""
    if not run.get('task_arn'):
        return {}
    region = _region(run['env'])
    task_id = run['task_arn'].rsplit('/', 1)[-1]
    links = {'ecs': f"https://{region}.console.aws.amazon.com/ecs/v2/clusters/{_cluster(run['env'])}"
                    f"/tasks/{task_id}?region={region}"}
    if run.get('log_group') and run.get('log_stream'):
        enc = lambda s: s.replace('/', '%252F')
        links['logs'] = (f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
                         f"#logsV2:log-groups/log-group/{enc(run['log_group'])}/log-events/{enc(run['log_stream'])}")
    return links


def _hint(run: dict) -> str:
    """What to do about a failed step, from the error lines in its log."""
    if run['status'] != 'failed':
        return ''
    text = ' '.join((run.get('checks') or {}).get('errors') or []) + ' ' + (run.get('message') or '')
    if 'invalid identifier' in text:
        return ("A column is missing. If a column was added, removed or renamed in an incremental or "
                "external stage model, run that model with Full Refresh from Manual dbt.")
    if 'does not exist or not authorized' in text or 'was not found' in text or 'depends on a node' in text:
        return "A table or model could not be found. " + OUT_OF_DATE
    return ''


def _run_summary(run: dict) -> dict:
    iso = lambda d: d.isoformat() if d else None
    checks = run.get('checks') or {}
    return {
        'run_id': run['run_id'], 'chain_id': run['chain_id'], 'job_name': run['job_name'], 'env': run['env'],
        'step': run['chain_pos'] + 1, 'steps': run['chain_len'],
        'status': run['status'], 'exit_code': run['exit_code'], 'message': run['message'] or '',
        'created_by': run['created_by'], 'created_at': iso(run['created_at']),
        'started_at': iso(run['started_at']), 'finished_at': iso(run['finished_at']),
        'command': shlex.join((run['spec'] or {}).get('command') or []),
        'start_log': list(run['start_log'] or []), 'links': _links(run),
        'checked': bool(checks.get('checked')), 'errors': checks.get('errors') or [],
        'total': checks.get('total'),
        'expected': (run['spec'] or {}).get('expected_models') or (run['spec'] or {}).get('expected_min'),
        'hint': _hint(run),
    }


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------

def load_lineage(conn=None) -> dict:
    """{'models': {...}, 'checked_at': datetime or None, 'source': 'recheck' | 'file'}."""
    def read(c):
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('dbt_lineage')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT checked_at, models FROM dbt_lineage WHERE id = 1")
            return cur.fetchone()
    if conn is None:
        with get_database_connection() as c:
            c.rollback()
            row = read(c)
            c.rollback()
    else:
        row = read(conn)
    if row:
        models = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        return {'models': models, 'checked_at': row[0], 'source': 'recheck'}
    try:
        with open(_LINEAGE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return {'models': data.get('models') or {}, 'checked_at': None, 'source': 'file',
                'file_built': data.get('generated_at') or {}}
    except FileNotFoundError:
        return {'models': {}, 'checked_at': None, 'source': 'none'}


def with_children(models: dict) -> dict:
    out = {name: dict(m, children=[]) for name, m in models.items()}
    for name, m in out.items():
        for p in m['parents']:
            if p in out:
                out[p]['children'].append(name)
    return out


def walk(models: dict, start: str, key: str) -> set:
    """Every model reached from `start` by following `key` (parents or children)."""
    seen, todo = set(), [start]
    while todo:
        for nxt in models.get(todo.pop(), {}).get(key, []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def steps_for(models: dict, chosen: set, jobs: list, target: str = None) -> list:
    """One step per project, in run order, each listing its chosen models."""
    steps = []
    for project in PROJECT_ORDER:
        names = sorted(n for n in chosen if models[n]['project'] == project)
        if not names:
            continue
        job = next((j for j in jobs if j['project'] == project), None)
        steps.append({
            'project': project,
            'job_id': job and job['job_id'],
            'job_name': job and job['name'],
            'select': ' '.join(names),
            'models': [{'name': n, 'materialized': models[n]['materialized'], 'table': models[n]['table'],
                        'schema': models[n]['schema'],
                        'needs_full_refresh_on_column_change': models[n]['materialized'] in FULL_REFRESH_KINDS,
                        'is_target': n == target}
                       for n in names],
        })
    return steps


def _ls_node(line: str):
    """One model from a `dbt ls --output json` log line, or None. Copes with
    dbt's JSON log format, where the line is inside data.msg."""
    start = line.find('{')
    if start < 0:
        return None
    try:
        obj = json.loads(line[start:])
    except ValueError:
        return None
    if isinstance(obj, dict) and 'name' not in obj and isinstance(obj.get('data'), dict):
        try:
            obj = json.loads(obj['data'].get('msg') or '')
        except ValueError:
            return None
    if not isinstance(obj, dict) or obj.get('package_name') not in PACKAGES or not obj.get('name'):
        return None
    return obj


def lineage_from_ls(lines: list) -> dict:
    models = {}
    for line in lines:
        node = _ls_node(line)
        if not node or node['name'] in models:
            continue
        config = node.get('config') or {}
        parents = (node.get('depends_on') or {}).get('nodes') or []
        models[node['name']] = {
            'project':      PACKAGES[node['package_name']],
            'materialized': config.get('materialized'),
            'schema':       node.get('schema'),
            'table':        node.get('alias') or config.get('alias') or node['name'],
            'tags':         node.get('tags') or config.get('tags') or [],
            'stage_url':    config.get('stage_url'),
            'parents':      sorted({p.split('.')[-1] for p in parents
                                    if p.startswith('model.') and p.split('.')[1] in PACKAGES}),
        }
    return dict(sorted(models.items()))


def _read_whole_log(run: dict) -> list:
    logs = boto3.client('logs', region_name=_region(run['env']))
    lines, token = [], None
    while True:
        kwargs = {'logGroupName': run['log_group'], 'logStreamName': run['log_stream'], 'startFromHead': True}
        if token:
            kwargs['nextToken'] = token
        resp = logs.get_log_events(**kwargs)
        events = resp.get('events') or []
        lines.extend(_clean(e['message']) for e in events)
        new_token = resp.get('nextForwardToken')
        if not events or new_token == token:
            return lines
        token = new_token


def _save_lineage(conn, run: dict) -> str:
    """After a Recheck Lineage run: read its log and store the models.
    Returns a problem, or '' when saved."""
    if not (run['log_group'] and run['log_stream']):
        return "The run has no log to read the models from"
    try:
        models = lineage_from_ls(_read_whole_log(run))
    except (ClientError, BotoCoreError) as e:
        return f"Could not read the log: {str(e)[:300]}"
    if not models:
        return "No models found in the dbt ls output"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dbt_lineage (id, checked_at, checked_by, models) VALUES (1, NOW(), %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET checked_at = EXCLUDED.checked_at, "
                    "checked_by = EXCLUDED.checked_by, models = EXCLUDED.models",
                    (run['created_by'], json.dumps(models)))
    conn.commit()
    counts = {p: sum(1 for m in models.values() if m['project'] == p) for p in PROJECT_ORDER}
    logger.info("dbt lineage rechecked by %s: %s", run['created_by'], counts)
    _set_run(conn, run['run_id'], f"Model list saved: {len(models)} models "
                                  f"({', '.join(f'{v} {k}' for k, v in counts.items())})")
    return ''


def _lineage_info(lineage: dict) -> dict:
    return {'source': lineage['source'], 'count': len(lineage['models']),
            'checked_at': lineage['checked_at'].isoformat() if lineage.get('checked_at') else None,
            'file_built': lineage.get('file_built') or {}}


# ---------------------------------------------------------------------------
# Jobs and lineage routes
# ---------------------------------------------------------------------------

@router.get("/api/dbt/config")
def dbt_config(user: str = Depends(get_current_user)):
    _admin(user)
    env = _env()
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        jobs = _load_jobs(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs ORDER BY created_at DESC, chain_pos DESC LIMIT 20")
            runs = [_run_summary(dict(zip(_RUN_COLS, r))) for r in cur.fetchall()]
            # A run still going, or a finished step whose next step has not
            # started yet (the page was closed in between) - the page picks it up again.
            cur.execute("SELECT run_id FROM dbt_runs r WHERE status IN ('waiting', 'running') "
                        "OR (status = 'done' AND NOT COALESCE((checks->>'checked')::boolean, FALSE) "
                        "    AND finished_at > NOW() - INTERVAL '6 hours') "
                        "OR (status = 'done' AND EXISTS (SELECT 1 FROM dbt_runs q WHERE q.chain_id = r.chain_id "
                        "    AND q.chain_pos = r.chain_pos + 1 AND q.status = 'queued')) "
                        "ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
        lineage = load_lineage(conn)
        conn.rollback()
    today = datetime.datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
    return {"status": "ok", "env": env, "cluster": _cluster(env), "region": _region(env),
            "today": today, "jobs": jobs, "runs": runs, "open_run_id": row[0] if row else None,
            "models": sorted(lineage['models']), "lineage": _lineage_info(lineage),
            # For the Project then Table lists in Plan From A Model.
            "model_info": {n: {'project': m.get('project'), 'table': m.get('table'), 'schema': m.get('schema'),
                               'materialized': m.get('materialized')} for n, m in lineage['models'].items()}}


def _validate_job(data: dict) -> dict:
    job = {k: str(data.get(k) or '').strip() for k in
           ('name', 'project', 'default_select', 'task_definition', 'container_name', 'capacity_provider',
            'command', 'snowflake_wh_override')}
    for key, label in (('name', 'Name'), ('task_definition', 'Task Definition'),
                       ('container_name', 'Container Name'), ('capacity_provider', 'Capacity Provider'),
                       ('command', 'Command')):
        if not job[key]:
            raise ValueError(f"{label} is needed")
    if job['project'] not in ('', *PROJECT_ORDER):
        raise ValueError("Project must be raw, transform, product or blank")
    if len(job['name']) > 100:
        raise ValueError("Name must be 100 characters or fewer")
    try:
        shlex.split(job['command'])
    except ValueError as e:
        raise ValueError(f"Command could not be read: {e}")
    mem = str(data.get('memory_reservation') or '').strip()
    job['memory_reservation'] = int(mem) if mem else None
    job['sort_order'] = int(data.get('sort_order') or 0)
    return job


@router.post("/api/dbt/jobs")
def dbt_save_job(data: dict, user: str = Depends(get_current_user)):
    """Add a job (no job_id) or update one."""
    _admin(user)
    try:
        job = _validate_job(data)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    cols = [c for c in _JOB_COLS if c != 'job_id']
    values = [job[c] for c in cols]
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            if data.get('job_id'):
                cur.execute(f"UPDATE dbt_job_definitions SET {', '.join(c + ' = %s' for c in cols)} "
                            "WHERE job_id = %s", values + [int(data['job_id'])])
            else:
                cur.execute(f"INSERT INTO dbt_job_definitions ({', '.join(cols)}) "
                            f"VALUES ({', '.join(['%s'] * len(cols))})", values)
        conn.commit()
    logger.info("dbt job %s saved by %s", job['name'], user)
    return {"status": "ok"}


@router.post("/api/dbt/jobs/delete")
def dbt_delete_job(data: dict, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dbt_job_definitions WHERE job_id = %s", (int(data.get('job_id') or 0),))
        conn.commit()
    return {"status": "ok"}


@router.get("/api/dbt/plan")
def dbt_plan(model: str, upstream: int = 0, downstream: int = 1, user: str = Depends(get_current_user)):
    """The steps needed to rebuild the models in `model` (comma separated):
    one per project, in run order, each listing that project's models from
    the chosen set."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        jobs = _load_jobs(conn)
        models = with_children(load_lineage(conn)['models'])
        conn.rollback()
    picked = list(dict.fromkeys(m.strip() for m in model.split(',') if m.strip()))
    missing = [m for m in picked if m not in models]
    if not picked or missing:
        return {"status": "error", "message": f"{', '.join(missing) or 'No table'} is not in the model list. "
                                              + OUT_OF_DATE}
    chosen = set(picked)
    for m in picked:
        if upstream:
            chosen |= walk(models, m, 'parents')
        if downstream:
            chosen |= walk(models, m, 'children')
    steps = steps_for(models, chosen, jobs)
    for s in steps:
        for m in s['models']:
            m['is_target'] = m['name'] in picked
    # The S3 folders the raw models read, i.e. which ingestion feeds this.
    feeds = sorted({models[n]['stage_url'] for n in chosen if models[n].get('stage_url')})
    parents = sorted({p for m in picked for p in models[m]['parents']} - set(picked))
    children = sorted({c for m in picked for c in models[m]['children']} - set(picked))
    return {"status": "ok", "model": ', '.join(picked), "models": picked, "steps": steps, "feeds": feeds,
            "parents": parents, "children": children}


@router.post("/api/dbt/lineage/recheck")
def dbt_recheck_lineage(user: str = Depends(get_current_user)):
    """Run `dbt ls` with the product job; the poll saves the models once it
    finishes. Reads the code ECS runs, so it matches what a run will do."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        busy = _busy_message(conn)
        if busy:
            return {"status": "error", "message": busy}
        job = next((j for j in _load_jobs(conn) if j['project'] == 'product'), None)
        if not job:
            return {"status": "error", "message": "Add a job for the product project first"}
        today = datetime.datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
        try:
            tokens = shlex.split(job['command'].replace('{select}', 'x').replace('{environment}', _env())
                                 .replace('{current_date}', today))
        except ValueError as e:
            return {"status": "error", "message": f"{job['name']}: command could not be read: {e}"}
        command, skip = [], 0
        for i, t in enumerate(tokens):
            if skip:
                skip -= 1
                continue
            if i == 0 and t in ('run', 'build'):
                command.append('ls')
            elif t in ('--select', '-s', '--threads', '--models', '-m'):
                skip = 1
            else:
                command.append(t)
        if not command or command[0] != 'ls':
            command.insert(0, 'ls')
        command += ['--resource-type', 'model', '--output', 'json', '--output-keys', LS_KEYS]
        try:
            run_id = start_chain(conn, [{'job_id': job['job_id'], 'command': command, 'kind': 'lineage',
                                         'label': 'Recheck Lineage'}], user)[1]
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        run = _load_run(conn, run_id)
    return {"status": "ok", "run": _run_summary(run)}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _try_start(conn, run: dict) -> None:
    """One run_task attempt. Leaves the run running, waiting (retry due
    later) or failed."""
    spec = run['spec']
    env = run['env']
    attempt = run['attempts'] + 1

    def fail(note):
        _set_run(conn, run['run_id'], note, status='failed', attempts=attempt, retry_at=None,
                 finished_at=_now(), message=note)
        _cancel_rest(conn, run)

    def wait(note, seconds):
        _set_run(conn, run['run_id'], note, status='waiting', attempts=attempt,
                 retry_at=_now() + datetime.timedelta(seconds=seconds))

    override = {'name': spec['container_name'], 'command': spec['command']}
    if spec.get('memory_reservation'):
        # ECS needs memory >= memoryReservation, so both are set the same.
        override['memoryReservation'] = override['memory'] = spec['memory_reservation']
    if spec.get('snowflake_wh_override'):
        override['environment'] = [{'name': 'SNOWFLAKE_WH_OVERRIDE', 'value': spec['snowflake_wh_override']}]

    ecs = boto3.client('ecs', region_name=_region(env))
    try:
        resp = ecs.run_task(
            cluster=_cluster(env),
            taskDefinition=spec['task_definition'],
            capacityProviderStrategy=[{
                'capacityProvider': f"aramis_group_core_{spec['capacity_provider']}_workloads_{env}_asg_cp",
                'weight': 1, 'base': 0}],
            count=1,
            overrides={'containerOverrides': [override]},
        )
    except (ClientError, BotoCoreError) as e:
        note = f"Attempt {attempt}/{MAX_ATTEMPTS}: AWS call failed - {str(e)[:300]}"
        code = e.response.get('Error', {}).get('Code', '') if isinstance(e, ClientError) else ''
        # A missing task definition, bad parameter or missing permission will
        # not fix itself in 10 seconds - stop now and say what was asked for.
        if code in ('ClientException', 'InvalidParameterException', 'AccessDeniedException'):
            fail(f"{note} (task definition {spec['task_definition']}, cluster {_cluster(env)}, "
                 f"region {_region(env)})")
            return
        if attempt < MAX_ATTEMPTS:
            wait(note + f" - retrying in {CLI_ERROR_RETRY_WAIT}s", CLI_ERROR_RETRY_WAIT)
        else:
            fail(note)
        return

    tasks = resp.get('tasks') or []
    if tasks:
        task_arn = tasks[0]['taskArn']
        log_group = None
        try:
            td = ecs.describe_task_definition(taskDefinition=spec['task_definition'])['taskDefinition']
            defs = td.get('containerDefinitions') or []
            cdef = next((c for c in defs if c.get('name') == spec['container_name']), defs[0] if defs else {})
            log_group = ((cdef.get('logConfiguration') or {}).get('options') or {}).get('awslogs-group')
        except (ClientError, BotoCoreError) as e:
            logger.warning("dbt run %s: could not read the log group: %s", run['run_id'], e)
        _set_run(conn, run['run_id'], f"Task started on attempt {attempt}: {task_arn}",
                 status='running', attempts=attempt, retry_at=None, task_arn=task_arn, started_at=_now(),
                 log_group=log_group,
                 log_stream=f"log_stream/{spec['container_name']}/{task_arn.rsplit('/', 1)[-1]}")
        return

    failures = resp.get('failures') or []
    reasons = '; '.join(f"{f.get('reason')} {f.get('detail') or ''}".strip() for f in failures) or 'no task returned'
    placement = any(r in (f.get('reason') or '') for f in failures for r in ('RESOURCE', 'AGENT'))
    note = f"Attempt {attempt}/{MAX_ATTEMPTS}: ECS could not place the task - {reasons}"
    if placement and attempt < MAX_ATTEMPTS:
        wait(note + f" - retrying in {PLACEMENT_RETRY_WAIT // 60} minutes (keep this page open)",
             PLACEMENT_RETRY_WAIT)
    else:
        fail(note)


def _cancel_rest(conn, run: dict, reason: str = None) -> None:
    """Later steps of a failed or stopped run will not start."""
    reason = reason or f"Not run - step {run['chain_pos'] + 1} did not finish"
    with conn.cursor() as cur:
        cur.execute("UPDATE dbt_runs SET status = 'cancelled', message = %s, finished_at = NOW() "
                    "WHERE chain_id = %s AND status = 'queued'", (reason, run['chain_id']))
    conn.commit()


def _start_next(conn, run: dict):
    """After a step finishes, start the next one. Returns its run id. The
    UPDATE ... WHERE status = 'queued' means only one poll can start it."""
    with conn.cursor() as cur:
        cur.execute("UPDATE dbt_runs SET status = 'waiting' WHERE chain_id = %s AND chain_pos = %s "
                    "AND status = 'queued' RETURNING run_id", (run['chain_id'], run['chain_pos'] + 1))
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    _try_start(conn, _load_run(conn, row[0]))
    return row[0]


def _busy_message(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT job_name FROM dbt_runs WHERE status IN ('waiting', 'running', 'queued') LIMIT 1")
        busy = cur.fetchone()
    conn.rollback()
    return f"{busy[0]} is still going - stop it first." if busy else ''


def _with_threads(command: list, threads: int) -> list:
    """The command with --threads set to `threads`, replacing any value it had."""
    out, skip = [], False
    for t in command:
        if skip:
            skip = False
            continue
        if t == '--threads':
            skip = True
            continue
        out.append(t)
    return out + ['--threads', str(threads)]


def _with_raw_upstream(select: str) -> str:
    """Put + in front of every __s model in a --select, so the stg tables that
    read S3 are always rebuilt with it. Without it a __s run only re-reads the
    stg table as it was last loaded, and new S3 data never reaches it."""
    return ' '.join('+' + t if re.match(r'^[\w.]+__s\+?$', t) else t for t in select.split())


def start_chain(conn, steps: list, user: str, current_date: str = '') -> tuple:
    """Add the steps as one chain and start the first. Each step is
    {job_id, select, full_refresh} or {job_id, command} (a ready token list),
    plus optional kind ('lineage'), label and expected_models (how many models
    dbt should run - a different count fails the step). Returns
    (chain_id, first run_id). ValueError with a message if it cannot start."""
    env = _env()
    _ensure_tables(conn)
    jobs = {j['job_id']: j for j in _load_jobs(conn)}
    planned = []
    for step in steps:
        job = jobs.get(int(step.get('job_id') or 0))
        if not job:
            raise ValueError("A step has no job - add one for that project first")
        if step.get('command'):
            command = list(step['command'])
        else:
            select = _with_raw_upstream(' '.join(str(step.get('select') or '').split()) or job['default_select'])
            command_text = (job['command'].replace('{select}', select).replace('{environment}', env)
                            .replace('{current_date}', current_date))
            try:
                command = shlex.split(command_text)
            except ValueError as e:
                raise ValueError(f"{job['name']}: command could not be read: {e}")
            if step.get('full_refresh'):
                command.append('--full-refresh')
        if step.get('threads'):
            command = _with_threads(command, int(step['threads']))
        spec = {k: job[k] for k in ('task_definition', 'container_name', 'capacity_provider',
                                     'memory_reservation', 'snowflake_wh_override')}
        spec['command'] = command
        spec['kind'] = step.get('kind') or 'run'
        if step.get('expected_models'):
            spec['expected_models'] = int(step['expected_models'])
        if step.get('expected_min'):
            spec['expected_min'] = int(step['expected_min'])
        planned.append((step.get('label') or job['name'], spec))
    if not planned:
        raise ValueError("Nothing to run")

    busy = _busy_message(conn)
    if busy:
        raise ValueError(busy)
    chain_id = str(uuid.uuid4())
    run_ids = []
    with conn.cursor() as cur:
        for pos, (name, spec) in enumerate(planned):
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            cur.execute("INSERT INTO dbt_runs (run_id, chain_id, chain_pos, chain_len, job_name, env, spec,"
                        " status, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (run_id, chain_id, pos, len(planned), name, env, json.dumps(spec),
                         'waiting' if pos == 0 else 'queued', user))
    conn.commit()
    for name, spec in planned:
        logger.info("dbt run started by %s: %s %s", user, name, shlex.join(spec['command']))
    _try_start(conn, _load_run(conn, run_ids[0]))
    return chain_id, run_ids[0]


@router.post("/api/dbt/run")
def dbt_start(data: dict, user: str = Depends(get_current_user)):
    """Start one or more steps, each {job_id, select, full_refresh}. Steps
    run one after another; a step that fails stops the rest."""
    _admin(user)
    current_date = str(data.get('current_date') or '').strip()
    if current_date:
        try:
            datetime.datetime.strptime(current_date, '%Y-%m-%d')
        except ValueError:
            return {"status": "error", "message": "Current Date must be YYYY-MM-DD"}
    with get_database_connection() as conn:
        conn.rollback()
        from .live_update import open_live_update
        busy = open_live_update(conn)
        if busy:
            return {"status": "error", "message": f"Live Update {busy} is going - wait for it or stop it first."}
        try:
            run_id = start_chain(conn, data.get('steps') or [], user, current_date)[1]
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        run = _load_run(conn, run_id)
    return {"status": "ok", "run": _run_summary(run)}


def _scan(conn, run: dict, lines: list) -> None:
    """Note what the new log lines show: dbt finding nothing to run, the
    PASS / ERROR / TOTAL summary, and error lines (first 15 kept)."""
    if not lines:
        return
    checks = dict(run.get('checks') or {})
    errors = list(checks.get('errors') or [])
    for line in lines:
        if _NOTHING.search(line):
            checks['nothing_to_do'] = True
        m = _DONE.search(line)
        if m:
            checks['pass'], checks['error'], checks['total'] = (int(g) for g in m.groups())
        text = line.strip()[:400]
        if _ERROR.search(line) and 'ERROR=0' not in line and text not in errors and len(errors) < 15:
            errors.append(text)
    checks['errors'] = errors
    if checks != (run.get('checks') or {}):
        _set_run(conn, run['run_id'], checks=json.dumps(checks))
        run['checks'] = checks


def _check_finished(conn, run: dict) -> str:
    """The checks on a step that exited with 0, once its whole log has been
    read. Returns a problem, or ''. Only one poll makes them (the claim on
    checks.checked)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE dbt_runs SET checks = checks || '{\"checked\": true}'::jsonb "
                    "WHERE run_id = %s AND NOT COALESCE((checks->>'checked')::boolean, FALSE) RETURNING run_id",
                    (run['run_id'],))
        claimed = cur.fetchone()
    conn.commit()
    if not claimed:
        return ''
    spec = run['spec'] or {}
    checks = run.get('checks') or {}
    if spec.get('kind') == 'lineage':
        return _save_lineage(conn, run)
    if checks.get('nothing_to_do'):
        return "dbt found nothing to run for this selection. " + OUT_OF_DATE
    expected = spec.get('expected_models')
    if expected and checks.get('total') is not None and checks['total'] != expected:
        return f"dbt ran {checks['total']} models but the plan has {expected}. " + OUT_OF_DATE
    # A + selection lets dbt find the models itself, so it may run more than
    # planned - but never fewer.
    least = spec.get('expected_min')
    if least and checks.get('total') is not None and checks['total'] < least:
        return f"dbt ran {checks['total']} models but the plan needs at least {least}. " + OUT_OF_DATE
    if (expected or least) and checks.get('total') is None:
        return "dbt's summary line (Done. PASS=...) was not found in the log, so the models run cannot be counted"
    return ''


def poll_run(conn, run_id: str, token: str = '') -> dict:
    """Move one run on and read its new log lines after `token`. Used by the
    page's poll and by Live Update. Safe for two pages polling at once."""
    lines, next_token, next_run_id = [], token or None, None
    run = _load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run['status'] == 'waiting' and (not run['retry_at'] or run['retry_at'] <= _now()):
        _try_start(conn, run)
        run = _load_run(conn, run_id)

    finished_now = False
    if run['task_arn'] and run['status'] == 'running':
        try:
            resp = boto3.client('ecs', region_name=_region(run['env'])).describe_tasks(
                cluster=_cluster(run['env']), tasks=[run['task_arn']])
            task = (resp.get('tasks') or [{}])[0]
            if task.get('lastStatus') == 'STOPPED':
                container = next((c for c in task.get('containers') or []
                                  if c.get('name') == run['spec']['container_name']), {})
                code = container.get('exitCode')
                # Only 0 counts: no exit code means the container never ran properly.
                ok = code == 0
                reason = task.get('stoppedReason') or container.get('reason') or ''
                if ok:
                    message = 'Finished'
                elif code is None:
                    message = "The container stopped without an exit code" + (f" - {reason}" if reason else '')
                else:
                    message = f"Failed with exit code {code}" + (f" - {reason}" if reason else '')
                with conn.cursor() as cur:
                    cur.execute("UPDATE dbt_runs SET status = %s, exit_code = %s, message = %s, finished_at = %s "
                                "WHERE run_id = %s AND status = 'running'",
                                ('done' if ok else 'failed', str(code), message, _now(), run_id))
                conn.commit()
                finished_now = True
        except (ClientError, BotoCoreError) as e:
            logger.warning("dbt run %s: describe_tasks failed: %s", run_id, e)

    if run['log_group'] and run['log_stream']:
        try:
            kwargs = {'logGroupName': run['log_group'], 'logStreamName': run['log_stream'],
                      'startFromHead': True}
            if token:
                kwargs['nextToken'] = token
            resp = boto3.client('logs', region_name=_region(run['env'])).get_log_events(**kwargs)
            lines = [_clean(e['message']) for e in resp.get('events') or []]
            next_token = resp.get('nextForwardToken') or token
        except ClientError as e:
            # The stream does not exist until the container writes its first line.
            if e.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
                logger.warning("dbt run %s: get_log_events failed: %s", run_id, e)

    run = _load_run(conn, run_id)
    # Each poller reads from its own token, so the same lines can be scanned
    # twice; only the counts and the first 15 error lines are kept, so that
    # does no harm.
    _scan(conn, run, lines)
    # Only check and move on once this poll has read the last of the log (an
    # empty page from CloudWatch), so no lines are lost when the page switches.
    if run['status'] == 'done' and not lines and not finished_now:
        problem = _check_finished(conn, run)
        run = _load_run(conn, run_id)
        if problem:
            _set_run(conn, run_id, problem, status='failed', message=problem)
            _cancel_rest(conn, run)
            run = _load_run(conn, run_id)
        elif (run['checks'] or {}).get('checked'):
            next_run_id = _start_next(conn, run)
            if not next_run_id:
                with conn.cursor() as cur:
                    cur.execute("SELECT run_id FROM dbt_runs WHERE chain_id = %s AND chain_pos = %s",
                                (run['chain_id'], run['chain_pos'] + 1))
                    row = cur.fetchone()
                next_run_id = row[0] if row else None
    elif run['status'] in ('failed', 'stopped'):
        _cancel_rest(conn, run)
    conn.rollback()
    return {"run": _run_summary(run), "lines": lines, "token": next_token, "next_run_id": next_run_id}


@router.get("/api/dbt/run/{run_id}")
def dbt_poll(run_id: str, token: str = '', user: str = Depends(get_current_user)):
    """Run state, log lines after `token` (CloudWatch's forward token), and
    next_run_id once the next step has been started."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        result = poll_run(conn, run_id, token)
    return {"status": "ok", **result}


def stop_chain(conn, chain_id: str, user: str) -> str:
    """Stop the step that is going and cancel the steps after it. Returns a message."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs WHERE chain_id = %s "
                    "AND status IN ('waiting', 'running')", (chain_id,))
        open_runs = [dict(zip(_RUN_COLS, r)) for r in cur.fetchall()]
    for r in open_runs:
        if r['task_arn']:
            try:
                boto3.client('ecs', region_name=_region(r['env'])).stop_task(
                    cluster=_cluster(r['env']), task=r['task_arn'],
                    reason=f"Stopped from the website by {user}")
            except (ClientError, BotoCoreError) as e:
                raise ValueError(f"Could not stop the task: {str(e)[:300]}")
        _set_run(conn, r['run_id'], status='stopped', message=f"Stopped by {user}", finished_at=_now())
    with conn.cursor() as cur:
        cur.execute("UPDATE dbt_runs SET status = 'cancelled', message = %s, finished_at = NOW() "
                    "WHERE chain_id = %s AND status = 'queued'", (f"Not run - stopped by {user}", chain_id))
    conn.commit()
    logger.info("dbt run %s stopped by %s", chain_id, user)
    return "Stopped" if open_runs else "Nothing was running"


@router.post("/api/dbt/run/{run_id}/stop")
def dbt_stop(run_id: str, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        run = _load_run(conn, run_id)
        if not run:
            return {"status": "ok", "message": "Nothing was running"}
        try:
            message = stop_chain(conn, run['chain_id'], user)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
    return {"status": "ok", "message": message}
