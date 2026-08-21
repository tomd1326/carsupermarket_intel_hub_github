"""Which connection pool each DB accessor uses, per environment.

The risk this covers is a pool being shared where the two ends point at
different Aurora clusters — in particular analytics reads, which must always
reach PROD even when the app is running against dev.

Run from source_code/router/backend:
    poetry run pytest tests/test_db_pools.py
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import db_utils


PROD_SECRET = 'prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
DEV_SECRET = 'dev/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'

_SECRETS = {
    PROD_SECRET: {'endpoint': 'prod.cluster.eu-west-1.rds.amazonaws.com',
                  'username': 'prod_user', 'password': 'prod_pw', 'port': 5432},
    DEV_SECRET: {'endpoint': 'dev.cluster.us-east-2.rds.amazonaws.com',
                 'username': 'dev_user', 'password': 'dev_pw', 'port': 5432},
}


@pytest.fixture(autouse=True)
def isolated_pools():
    """Reset the module-level pools and patch out Secrets Manager and psycopg2."""
    db_utils._db_pool = None
    db_utils._vehicle_db_pool = None
    db_utils._prod_pricer_pool = None

    def fake_pool(**kwargs):
        # Stand-in for ThreadedConnectionPool: records the target it was built for.
        return mock.Mock(name=f"pool:{kwargs.get('host')}", host=kwargs.get('host'))

    with mock.patch.object(db_utils, 'get_secret', side_effect=lambda name: _SECRETS[name]), \
         mock.patch.object(db_utils.psycopg2.pool, 'ThreadedConnectionPool', side_effect=fake_pool):
        yield

    db_utils._db_pool = None
    db_utils._vehicle_db_pool = None
    db_utils._prod_pricer_pool = None


def _env(**overrides):
    """Replace the environment with only the vars the pool selection reads."""
    keep = {k: v for k, v in overrides.items() if v is not None}
    return mock.patch.dict(os.environ, keep, clear=True)


def test_deployed_dev_merges_main_and_vehicle_pools():
    with _env(AWS_REGION='us-east-2'):
        assert db_utils._get_vehicle_pool() is db_utils._get_pool()
        assert db_utils._get_pool().host == _SECRETS[DEV_SECRET]['endpoint']


def test_prod_merges_main_and_vehicle_pools():
    with _env(AWS_REGION='eu-west-1'):
        assert db_utils._get_vehicle_pool() is db_utils._get_pool()
        assert db_utils._get_pool().host == _SECRETS[PROD_SECRET]['endpoint']


def test_local_vehicle_override_keeps_separate_pool():
    with _env(LOCAL_ENV='1', VEHICLE_DB_HOST='localhost'):
        vehicle = db_utils._get_vehicle_pool()
        assert vehicle is not db_utils._get_pool()
        assert vehicle.host == 'localhost'


def test_prod_kpi_uses_merged_pool():
    with _env(AWS_REGION='eu-west-1'):
        assert db_utils._get_prod_pricer_pool() is db_utils._get_pool()


def test_dev_kpi_keeps_separate_prod_pricer_pool():
    with _env(AWS_REGION='us-east-2'):
        kpi = db_utils._get_prod_pricer_pool()
        assert kpi is not db_utils._get_pool()
        # Analytics must still read prod even though the app is on dev.
        assert kpi.host == _SECRETS[PROD_SECRET]['endpoint']
        assert db_utils._get_pool().host == _SECRETS[DEV_SECRET]['endpoint']
