"""Settings > Run dbt: start dbt ECS tasks from the website, the same way the
Rundeck "ECS run task template" job does, and show their logs.

Matches the Rundeck script:
- cluster aramis_group_core_prod_cluster, capacity provider
  aramis_group_core_<cp>_workloads_prod_asg_cp, in eu-west-1. Every site
  (prod, dev, local) runs the prod task - see _env()
- the command is split like a shell would (shlex) and sent as a container
  override, with optional memory and SNOWFLAKE_WH_OVERRIDE
- 3 attempts to start: 10s wait after an AWS call error, 300s after a
  RESOURCE/AGENT placement failure
- log stream log_stream/<container>/<task id> in the task definition's log group

The ECS task runs on its own, so the 10-minute Lambda limit does not apply.
The page polls: each poll checks the task, makes any start retry that is due,
starts the next step of a multi-step run once the previous one has finished,
and returns new log lines. Retries and next steps only happen while the page
is open.

Jobs (name, project, task definition, command...) are edited on the page and
stored in dbt_job_definitions. A command can use {select}, {environment} and
{current_date}, filled in when the run starts.

Model relationships come from data/dbt_lineage.json, built from the dbt
manifests by scripts/build_dbt_lineage.py. Picking a model gives one step per
project (raw, then transform, then product) listing the models it needs.
"""
import datetime
import json
import logging
import os
import shlex
import uuid
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException

from utils.db_utils import get_database_connection
from .core import get_current_user, ADMIN_USERNAME

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_ATTEMPTS = 3
CLI_ERROR_RETRY_WAIT = 10
PLACEMENT_RETRY_WAIT = 300

PROJECT_ORDER = ['raw', 'transform', 'product']

# Fills the job list the first time the page is opened. "dbt raw" is the
# Rundeck job; transform and product follow the same pattern - check their
# commands against Rundeck and edit them on the page if they differ.
_COMMAND = ('run --select {select} --target {environment} --threads 20 '
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

_LINEAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'data', 'dbt_lineage.json')
_lineage = None


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


def _load_lineage() -> dict:
    global _lineage
    if _lineage is None:
        try:
            with open(_LINEAGE_PATH, encoding='utf-8') as f:
                _lineage = json.load(f)
        except FileNotFoundError:
            _lineage = {'generated_at': {}, 'models': {}}
    return _lineage


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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
             'spec', 'log_group', 'log_stream', 'start_log', 'retry_at']


def _load_run(conn, run_id: str):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return dict(zip(_RUN_COLS, row)) if row else None


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


def _run_summary(run: dict) -> dict:
    iso = lambda d: d.isoformat() if d else None
    return {
        'run_id': run['run_id'], 'job_name': run['job_name'], 'env': run['env'],
        'step': run['chain_pos'] + 1, 'steps': run['chain_len'],
        'status': run['status'], 'exit_code': run['exit_code'], 'message': run['message'] or '',
        'created_by': run['created_by'], 'created_at': iso(run['created_at']),
        'started_at': iso(run['started_at']), 'finished_at': iso(run['finished_at']),
        'command': shlex.join((run['spec'] or {}).get('command') or []),
        'start_log': list(run['start_log'] or []), 'links': _links(run),
    }


# ---------------------------------------------------------------------------
# Jobs and lineage
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
        conn.rollback()
    lineage = _load_lineage()
    today = datetime.datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
    # A run still going, or a finished step whose next step has not started
    # yet (the page was closed in between) - the page picks it up again.
    open_run_id = None
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM dbt_runs r WHERE status IN ('waiting', 'running') "
                        "OR (status = 'done' AND EXISTS (SELECT 1 FROM dbt_runs q WHERE q.chain_id = r.chain_id "
                        "    AND q.chain_pos = r.chain_pos + 1 AND q.status = 'queued')) "
                        "ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            open_run_id = row[0] if row else None
        conn.rollback()
    return {"status": "ok", "env": env, "cluster": _cluster(env), "region": _region(env),
            "today": today, "jobs": jobs, "runs": runs, "open_run_id": open_run_id,
            "models": sorted(lineage['models']), "lineage_built": lineage['generated_at']}


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


def _walk(models: dict, start: str, key: str) -> set:
    """Every model reached from `start` by following `key` (parents or children)."""
    seen, todo = set(), [start]
    while todo:
        for nxt in models.get(todo.pop(), {}).get(key, []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


@router.get("/api/dbt/plan")
def dbt_plan(model: str, upstream: int = 1, downstream: int = 0, user: str = Depends(get_current_user)):
    """The steps needed to rebuild `model`: one per project, in run order,
    each listing that project's models from the chosen set."""
    _admin(user)
    lineage = _load_lineage()
    models = {name: dict(m, children=[]) for name, m in lineage['models'].items()}
    if model not in models:
        return {"status": "error", "message": f"{model} is not in the model list"}
    for name, m in models.items():
        for p in m['parents']:
            if p in models:
                models[p]['children'].append(name)
    chosen = {model}
    if upstream:
        chosen |= _walk(models, model, 'parents')
    if downstream:
        chosen |= _walk(models, model, 'children')

    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        jobs = _load_jobs(conn)
        conn.rollback()
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
                        'is_target': n == model}
                       for n in names],
        })
    # The S3 folders the raw models read, i.e. which ingestion feeds this.
    feeds = sorted({models[n]['stage_url'] for n in chosen if models[n].get('stage_url')})
    return {"status": "ok", "model": model, "steps": steps, "feeds": feeds,
            "parents": models[model]['parents'], "children": sorted(models[model]['children'])}


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
    """After a step finishes, start the next one. Returns its run id."""
    with conn.cursor() as cur:
        cur.execute("UPDATE dbt_runs SET status = 'waiting' WHERE chain_id = %s AND chain_pos = %s "
                    "AND status = 'queued' RETURNING run_id", (run['chain_id'], run['chain_pos'] + 1))
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    _try_start(conn, _load_run(conn, row[0]))
    return row[0]


@router.post("/api/dbt/run")
def dbt_start(data: dict, user: str = Depends(get_current_user)):
    """Start one or more steps, each {job_id, select, full_refresh}. Steps
    run one after another; a step that fails stops the rest."""
    _admin(user)
    env = _env()
    current_date = str(data.get('current_date') or '').strip()
    if current_date:
        try:
            datetime.datetime.strptime(current_date, '%Y-%m-%d')
        except ValueError:
            return {"status": "error", "message": "Current Date must be YYYY-MM-DD"}
    steps = data.get('steps') or []
    if not steps:
        return {"status": "error", "message": "Nothing to run"}
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        jobs = {j['job_id']: j for j in _load_jobs(conn)}
        planned = []
        for step in steps:
            job = jobs.get(int(step.get('job_id') or 0))
            if not job:
                return {"status": "error", "message": "A step has no job - add one for that project first"}
            select = ' '.join(str(step.get('select') or '').split()) or job['default_select']
            command_text = (job['command'].replace('{select}', select).replace('{environment}', env)
                            .replace('{current_date}', current_date))
            try:
                command = shlex.split(command_text)
            except ValueError as e:
                return {"status": "error", "message": f"{job['name']}: command could not be read: {e}"}
            if step.get('full_refresh'):
                command.append('--full-refresh')
            spec = {k: job[k] for k in ('task_definition', 'container_name', 'capacity_provider',
                                         'memory_reservation', 'snowflake_wh_override')}
            spec['command'] = command
            planned.append((job['name'], spec))

        with conn.cursor() as cur:
            cur.execute("SELECT job_name FROM dbt_runs WHERE status IN ('waiting', 'running', 'queued') LIMIT 1")
            busy = cur.fetchone()
            if busy:
                conn.rollback()
                return {"status": "error", "message": f"{busy[0]} is still going - stop it first."}
            chain_id = str(uuid.uuid4())
            run_ids = []
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
        run = _load_run(conn, run_ids[0])
    return {"status": "ok", "run": _run_summary(run)}


@router.get("/api/dbt/run/{run_id}")
def dbt_poll(run_id: str, token: str = '', user: str = Depends(get_current_user)):
    """Run state, log lines after `token` (CloudWatch's forward token), and
    next_run_id once the next step has been started."""
    _admin(user)
    lines, next_token, next_run_id = [], token or None, None
    with get_database_connection() as conn:
        conn.rollback()
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
                    # As the Rundeck script: exit code 0 or none means it worked.
                    ok = code in (0, None)
                    reason = task.get('stoppedReason') or container.get('reason') or ''
                    message = 'Finished' if ok else f"Failed with exit code {code}" + (f" - {reason}" if reason else '')
                    _set_run(conn, run_id, status='done' if ok else 'failed', exit_code=str(code),
                             message=message, finished_at=_now())
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
                lines = [e['message'] for e in resp.get('events') or []]
                next_token = resp.get('nextForwardToken') or token
            except ClientError as e:
                # The stream does not exist until the container writes its first line.
                if e.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
                    logger.warning("dbt run %s: get_log_events failed: %s", run_id, e)

        run = _load_run(conn, run_id)
        # Only move on once this poll has read the last of the log (an empty
        # page from CloudWatch), so no lines are lost when the page switches.
        if run['status'] == 'done' and not lines and not finished_now:
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
    return {"status": "ok", "run": _run_summary(run), "lines": lines, "token": next_token,
            "next_run_id": next_run_id}


@router.post("/api/dbt/run/{run_id}/stop")
def dbt_stop(run_id: str, user: str = Depends(get_current_user)):
    """Stop the step that is going and cancel the steps after it."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        run = _load_run(conn, run_id)
        if not run:
            return {"status": "ok", "message": "Nothing was running"}
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_RUN_COLS)} FROM dbt_runs WHERE chain_id = %s "
                        "AND status IN ('waiting', 'running')", (run['chain_id'],))
            open_runs = [dict(zip(_RUN_COLS, r)) for r in cur.fetchall()]
        for r in open_runs:
            if r['task_arn']:
                try:
                    boto3.client('ecs', region_name=_region(r['env'])).stop_task(
                        cluster=_cluster(r['env']), task=r['task_arn'],
                        reason=f"Stopped from the website by {user}")
                except (ClientError, BotoCoreError) as e:
                    return {"status": "error", "message": f"Could not stop the task: {str(e)[:300]}"}
            _set_run(conn, r['run_id'], status='stopped', message=f"Stopped by {user}", finished_at=_now())
        _cancel_rest(conn, run, f"Not run - stopped by {user}")
    logger.info("dbt run %s stopped by %s", run['chain_id'], user)
    return {"status": "ok", "message": "Stopped" if open_runs else "Nothing was running"}
