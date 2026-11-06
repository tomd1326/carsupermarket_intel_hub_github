"""Report planning and execution boundaries, with no live database or API calls."""
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import self_analytics as sa
from utils import self_analytics_builder as b
from utils import ai_knowledge as ak


@pytest.fixture
def definition():
    entry = next(e for e in ak._reference_seeds() if e['id'] == 'seed_builder_sales_20261106')
    return b.schema(entry)


@pytest.fixture
def plan(definition):
    return {'prompt': 'Compare Buyer And Actual Margin', 'source_spec': {
        'id': definition['id'], 'revision': definition['revision'], 'date': 'invoice_posted_date',
        'from': '2026-05-01', 'to': '2026-10-31',
        'fields': ['invoice_posted_date', 'buyer_est_gpu_m', 'gpu_m', 'vrm', 'supplier_channel'],
        'answers': {'invoice_status': 'paid', 'invoice_credited': 'exclude', 'sales_type_bucket': 'rate'},
        'filters': [], 'interactive': ['supplier_channel']},
        'charts': [{'id': 'c1', 'title': 'Margin', 'type': 'bar', 'x': 'invoice_posted_date',
                    'grain': 'month', 'cohort': 'complete', 'format': 'currency', 'filters': [],
                    'measures': [{'column': 'buyer_est_gpu_m', 'agg': 'avg'}, {'column': 'gpu_m', 'agg': 'avg'}]}]}


def test_prepares_explicit_population_and_inclusive_dates(plan, definition):
    out = b.prepare(plan, definition)
    assert '"INVOICE_POSTED_DATE" < \'2026-11-01\'::DATE' in out['sql']
    assert '"INVOICE_STATUS" = \'STATUS_PAID\'' in out['sql']
    assert '"INVOICE_CREDITED" = FALSE' in out['sql']
    assert '"SALES_TYPE_BUCKET" = \'Rate/Cash\'' in out['sql']
    assert 'SELECT *' not in out['sql']
    assert out['source_spec']['request'] == plan['prompt']
    assert out['charts'][0]['cohort'] == 'complete'


@pytest.mark.parametrize('key', ['date', 'from', 'to', 'fields', 'revision'])
def test_missing_choices_never_get_defaults(plan, definition, key):
    plan['source_spec'].pop(key)
    with pytest.raises(ValueError):
        b.prepare(plan, definition)


def test_population_answer_is_required(plan, definition):
    plan['source_spec']['answers'].pop('invoice_credited')
    with pytest.raises(ValueError, match='Credited'):
        b.prepare(plan, definition)


def test_unapproved_or_disabled_source_is_not_available(monkeypatch):
    entries = [dict(e, status='proposed', enabled=True) for e in ak._reference_seeds()]
    monkeypatch.setattr(ak, 'list_entries', lambda: entries)
    with pytest.raises(ValueError, match='Approve'):
        b.source('seed_builder_sales_20261106')
    entries[-2].update(status='active', enabled=False)
    with pytest.raises(ValueError, match='Approve'):
        b.source('seed_builder_sales_20261106')


def test_unknown_fields_and_sql_expressions_rejected(plan, definition):
    plan['source_spec']['fields'].append('gpu_m); DROP TABLE x')
    with pytest.raises(ValueError, match='Exact Columns'):
        b.prepare(plan, definition)
    entry = {'id': 'bad', 'title': 'Bad', 'fields': {'report_schema': json.dumps({
        'table': 'PROD_CARSUPERMARKET_DB.X.Y JOIN OTHER', 'columns': []})}}
    with pytest.raises(ValueError, match='Source Definition'):
        b.schema(entry)


def test_literal_escaping_and_revision_change(plan, definition):
    plan['source_spec']['filters'] = [{'column': 'vrm', 'op': '=', 'value': "x\\'; DROP TABLE x --"}]
    sql = b.prepare(plan, definition)['sql']
    assert "x\\\\''; DROP TABLE x --" in sql
    assert sa.check_sql(sql) is None
    definition['revision'] = 'changed'
    with pytest.raises(ValueError, match='Changed'):
        b.prepare(plan, definition)


@pytest.mark.parametrize('kind', ['pie', 'doughnut', 'tile'])
def test_charts_cannot_silently_discard_measures(plan, definition, kind):
    plan['charts'][0]['type'] = kind
    with pytest.raises(ValueError, match='One Measure'):
        b.prepare(plan, definition)


def test_ratio_contract_and_missing_filters(plan, definition):
    plan['charts'][0]['format'] = 'percent'
    plan['charts'][0]['measures'] = [{'column': 'gpu_m', 'denominator': 'buyer_est_gpu_m', 'agg': 'sum'}]
    out = b.prepare(plan, definition)
    assert out['charts'][0]['measures'][0]['denominator'] == 'buyer_est_gpu_m'
    plan['charts'][0]['measures'][0]['agg'] = 'avg'
    with pytest.raises(ValueError, match='Ratio'):
        b.prepare(plan, definition)
    plan['charts'][0]['measures'][0]['agg'] = 'sum'
    plan['charts'][0]['filters'] = [{'column': 'unknown', 'op': '=', 'value': '1'}]
    with pytest.raises(ValueError, match='Chart Filter'):
        b.prepare(plan, definition)


def test_truncated_results_are_never_charted(plan, definition, monkeypatch):
    monkeypatch.setattr(b, 'source', lambda _: definition)
    monkeypatch.setattr(sa, 'run_query', lambda *a: {'truncated': True, 'rows': [[1]]})
    with pytest.raises(ValueError, match='Partial Data'):
        b.run(plan['source_spec'], plan['charts'], dict(sa.SETTINGS_DEFAULTS, builder_enabled=True))
    with pytest.raises(ValueError, match='Switched Off'):
        b.run(plan['source_spec'], plan['charts'], sa.SETTINGS_DEFAULTS)


def test_saved_report_revalidates_and_preserves_choices(plan, definition, monkeypatch):
    monkeypatch.setattr(b, 'source', lambda _: definition)
    report = sa.clean_report(dict(plan, name='Buyer Report', sql='Ignored Browser SQL'))
    assert report['source_spec']['interactive'] == ['supplier_channel']
    assert report['sql'].startswith('SELECT ')
    exported = sa.export_report(report)
    assert exported['source_spec'] == report['source_spec']
    monkeypatch.setattr(sa, 'run_query', lambda *a: {'truncated': False, 'rows': [], 'row_count': 0})
    out = sa.run_report(dict(report, id=45), dict(sa.SETTINGS_DEFAULTS, builder_enabled=True), False, 'now')
    assert out['from_cache'] is False and out['row_count'] == 0
    definition['revision'] = 'changed'
    with pytest.raises(ValueError, match='Changed'):
        sa.run_report(dict(report, id=45), dict(sa.SETTINGS_DEFAULTS, builder_enabled=True), False, 'now')


def test_sp_dates_filters_and_text_identifiers(monkeypatch):
    definition = {'id': 'sp:spend', 'revision': 'r1', 'name': 'Spend', 'kind': 'sharepoint',
        'freshness': '2026-01-03', 'columns': [{'name': 'Date', 'type': 'date'},
            {'name': 'Spend', 'type': 'number'}, {'name': 'Code', 'type': 'text'}], 'questions': []}
    rows = [[46023, 100, '001'], ['2026-01-01', 10, '1'], ['2025-12-31', 50, '001']]
    monkeypatch.setattr(b, 'spreadsheet', lambda _: (definition, copy.deepcopy(rows)))
    spec = {'id': 'sp:spend', 'revision': 'r1', 'date': 'Date', 'from': '2026-01-01', 'to': '2026-01-31',
            'fields': ['Date', 'Spend', 'Code'], 'filters': [{'column': 'Code', 'op': '=', 'value': '001'}]}
    charts = [{'type': 'line', 'x': 'Date', 'grain': 'day', 'cohort': 'complete', 'format': 'currency',
               'measures': [{'column': 'Spend', 'agg': 'sum'}]}]
    out = b.run(spec, charts, dict(sa.SETTINGS_DEFAULTS, builder_enabled=True))
    assert out['row_count'] == 1 and out['rows'][0] == ['2026-01-01', 100, '001']
    assert b._matches('001', {'op': '=', 'value': '1'}) is False
    rows.append(['not a date', 1, '001'])
    with pytest.raises(ValueError, match='Unrecognised Date'):
        b.run(spec, charts, dict(sa.SETTINGS_DEFAULTS, builder_enabled=True))


def test_seed_import_is_additive_and_marker_prevents_resurrection():
    class Cursor:
        def __init__(self, marker): self.marker, self.calls = marker, []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args): self.calls.append((sql, args))
        def fetchone(self): return ('true',) if self.marker else None
    class Conn:
        def __init__(self, marker): self.cur, self.commits = Cursor(marker), 0
        def cursor(self): return self.cur
        def commit(self): self.commits += 1
    existing = Conn(True)
    ak._import_report_knowledge(existing)
    assert len(existing.cur.calls) == 1 and existing.commits == 0
    fresh = Conn(False)
    ak._import_report_knowledge(fresh)
    inserts = [sql for sql, args in fresh.cur.calls if 'INSERT INTO ai_knowledge' in sql]
    assert len(inserts) == 21 and all("'proposed'" in sql and 'DO NOTHING' in sql for sql in inserts)
    assert fresh.commits == 1


def test_source_definitions_are_checked_when_knowledge_is_saved():
    entry = next(e for e in ak._reference_seeds() if e['id'] == 'seed_builder_sales_20261106')
    assert ak.validate_entry(entry) is None
    bad = copy.deepcopy(entry)
    bad['fields']['report_schema'] = '{bad json'
    assert 'Source Definition' in ak.validate_entry(bad)


@pytest.mark.parametrize('writer,admin', [(True, True), (True, False), (False, False)])
def test_page_controls_render_for_each_role(writer, admin):
    from jinja2 import DictLoader, Environment
    from html.parser import HTMLParser
    path = Path(__file__).resolve().parents[1] / 'templates/self_analytics.html'
    env = Environment(loader=DictLoader({'page': path.read_text(encoding='utf-8'),
        '_navigation.html': '{% macro page_header(title,user) %}<header>{{title}}</header>{% endmacro %}'}))
    html = env.get_template('page').render(can_write_sql=writer, show_admin=admin,
        BASE_URL='', user='test', page_name=lambda _: 'Self-Analytics', static_url=lambda s: '/static/' + s)
    class IDs(HTMLParser):
        def __init__(self): super().__init__(); self.ids = []
        def handle_starttag(self, tag, attrs):
            self.ids.extend(v for k, v in attrs if k == 'id')
    parser = IDs(); parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert {'sa-guided', 'sa-live-filters', 'sa-drill-modal', 'sa-e-cohort', 'sa-run'} <= set(parser.ids)
    assert ('sa-s-builder' in parser.ids) == admin
    assert ('sa-sql' in parser.ids) == writer


@pytest.fixture
def router_module(monkeypatch):
    core = types.ModuleType('routers.core')
    for name in ('BASE_URL', 'VALID_USERS', 'templates', 'user_details'):
        setattr(core, name, {})
    core.is_admin = lambda user: user == 'admin'
    core.user_has_permission = lambda user, permission: user in ('admin', 'writer')
    core.require_page = core.require_page_api = lambda _: (lambda: 'writer')
    core._track = lambda *args: None
    import datetime
    core._uk_now = datetime.datetime.now
    monkeypatch.setitem(sys.modules, 'routers.core', core)
    spec = importlib.util.spec_from_file_location('routers._builder_route_test', Path(__file__).resolve().parents[1] / 'routers/self_analytics.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sa, 'get_settings', lambda: dict(sa.SETTINGS_DEFAULTS, builder_enabled=True))
    return mod


def test_route_permission_and_review_integrity(router_module, plan, definition, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(b, 'source', lambda _: definition)
    with pytest.raises(HTTPException) as exc:
        router_module.builder_preview(plan, user='viewer')
    assert exc.value.status_code == 403
    preview = router_module.builder_preview(plan, user='writer')
    calls = []
    monkeypatch.setattr(b, 'run', lambda *args: calls.append(args) or {'rows': [], 'row_count': 0})
    payload = dict(plan, confirmed=True, review_hash=preview['review_hash'])
    payload['charts'] = copy.deepcopy(plan['charts'])
    payload['charts'][0]['grain'] = 'week'
    assert router_module.builder_run(payload, None, user='writer')['status'] == 'error'
    assert not calls
    payload['charts'] = plan['charts']
    assert router_module.builder_run(payload, None, user='writer')['status'] == 'ok'
    assert len(calls) == 1
