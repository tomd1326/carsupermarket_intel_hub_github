"""Keycloak sign-in: email matching, the callback, and the local-only bypass."""
import asyncio
from unittest import mock

import pytest

from routers import auth


def _request(query=None):
    req = mock.MagicMock()
    req.query_params = query or {}
    req.session = {}
    return req


def _client(userinfo):
    client = mock.MagicMock()
    client.authorize_access_token = mock.AsyncMock(return_value={'userinfo': userinfo})
    return client


def _callback(userinfo, usernames_for_email):
    """Run the callback with Keycloak answering userinfo and the users table
    holding usernames_for_email. Returns (response, request)."""
    req = _request()
    with mock.patch.object(auth.keycloak_auth, 'get_client', return_value=_client(userinfo)), \
            mock.patch.object(auth, '_reload_users_from_aurora'), \
            mock.patch.object(auth, 'get_database_connection'), \
            mock.patch.object(auth, 'find_usernames_by_email', return_value=usernames_for_email), \
            mock.patch.object(auth, 'record_login'), \
            mock.patch.object(auth, 'VALID_USERS', {'ann', 'bob'}), \
            mock.patch.object(auth, '_login_page', side_effect=lambda r, e=None, c=200: ('page', e, c)):
        return asyncio.run(auth.login_callback(req)), req


def test_matching_email_starts_the_session():
    resp, req = _callback({'email': 'Ann@x.com', 'email_verified': True}, ['ann'])
    assert resp.status_code == 303 and resp.headers['location'].endswith('/home')
    assert req.session['user'] == 'ann'
    assert 'pwd_hash' not in req.session


def test_unknown_email_is_refused():
    resp, req = _callback({'email': 'who@x.com'}, [])
    assert resp[0] == 'page' and resp[2] == 403
    assert 'user' not in req.session


def test_email_shared_by_two_users_is_refused():
    resp, req = _callback({'email': 'ann@x.com'}, ['ann', 'bob'])
    assert resp[0] == 'page' and resp[2] == 403
    assert 'user' not in req.session


def test_a_user_missing_from_the_users_table_is_ignored():
    resp, req = _callback({'email': 'zed@x.com'}, ['zed'])   # zed not in VALID_USERS
    assert resp[2] == 403 and 'user' not in req.session


@pytest.mark.parametrize('claims', [{}, {'email': ''}, {'email': 'a@x.com', 'email_verified': False}])
def test_missing_or_unverified_email_is_refused(claims):
    resp, req = _callback(claims, ['ann'])
    assert resp[2] == 403 and 'user' not in req.session


def test_failed_token_exchange_is_refused():
    client = mock.MagicMock()
    client.authorize_access_token = mock.AsyncMock(side_effect=RuntimeError('bad state'))
    with mock.patch.object(auth.keycloak_auth, 'get_client', return_value=client), \
            mock.patch.object(auth, '_login_page', side_effect=lambda r, e=None, c=200: ('page', e, c)):
        resp = asyncio.run(auth.login_callback(_request()))
    assert resp[2] == 401


def test_keycloak_error_parameter_is_refused():
    with mock.patch.object(auth, '_login_page', side_effect=lambda r, e=None, c=200: ('page', e, c)):
        resp = asyncio.run(auth.login_callback(_request({'error': 'access_denied'})))
    assert resp[2] == 401


def test_bypass_needs_local_env_and_no_lambda(monkeypatch):
    monkeypatch.setenv('LOCAL_LOGIN_AS', 'ann')
    monkeypatch.delenv('LOCAL_ENV', raising=False)
    assert auth._local_bypass_user() is None
    monkeypatch.setenv('LOCAL_ENV', '1')
    monkeypatch.delenv('AWS_LAMBDA_FUNCTION_NAME', raising=False)
    assert auth._local_bypass_user() == 'ann'
    monkeypatch.setenv('AWS_LAMBDA_FUNCTION_NAME', 'router')
    assert auth._local_bypass_user() is None


def test_each_environment_has_its_own_client_and_redirect():
    from utils import keycloak_auth
    dev, prod = keycloak_auth.environment_config('dev'), keycloak_auth.environment_config('prod')
    assert 'stage' in dev['issuer'] and 'stage' not in prod['issuer']
    assert dev['redirect_uri'] == 'https://dev-uk-intel-hub.aramis-group.eu/login/oauth2/callback'
    assert prod['redirect_uri'] == 'https://uk-intel-hub.aramis-group.eu/login/oauth2/callback'
    assert dev['secret_name'].startswith('dev/') and prod['secret_name'].startswith('prod/')
    with pytest.raises(keycloak_auth.KeycloakNotConfigured):
        keycloak_auth.environment_config('local')
