"""Keycloak single sign-on (OpenID Connect, authorization code flow).

Dev uses the stage Keycloak, prod the production one, both in the 'cosmos'
realm. The client is confidential: its id and secret live in Secrets Manager
(<env>/carsupermarket/core/keycloak/intel_hub_frontend_credentials).

The OAuth client is built on first use, not at import, so the app still starts
(and the local bypass still works) when Secrets Manager cannot be reached.
"""
import logging
import os

from authlib.integrations.starlette_client import OAuth

from .secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

# Keycloak settings per environment. The redirect URIs are the ones devops
# registered on each client; Keycloak refuses any other.
_ENVIRONMENTS = {
    'prod': {
        'issuer': 'https://keycloak.aramisgroup.tech/realms/cosmos',
        'client_id': 'carsupermarket-intel-hub-prod',
        'secret_name': 'prod/carsupermarket/core/keycloak/intel_hub_frontend_credentials',
        'redirect_uri': 'https://uk-intel-hub.aramis-group.eu/login/oauth2/callback',
    },
    'dev': {
        'issuer': 'https://keycloak.stage.aramisgroup.tech/realms/cosmos',
        'client_id': 'carsupermarket-intel-hub-dev',
        'secret_name': 'dev/carsupermarket/core/keycloak/intel_hub_frontend_credentials',
        'redirect_uri': 'https://dev-uk-intel-hub.aramis-group.eu/login/oauth2/callback',
    },
}

_oauth: OAuth | None = None
_oauth_env: str | None = None


class KeycloakNotConfigured(Exception):
    """Sign-in cannot start. The message is safe to show on the page."""


def _secret_value(secret: dict, *names: str) -> str | None:
    for name in names:
        for key in (name, name.upper()):
            if secret.get(key):
                return secret[key]
    return None


def environment_config(env: str) -> dict:
    if env not in _ENVIRONMENTS:
        raise KeycloakNotConfigured(f"Single sign-on is not set up for the '{env}' environment.")
    config = dict(_ENVIRONMENTS[env])
    # Lets a deployment point at another address without a code change.
    config['redirect_uri'] = os.getenv('KEYCLOAK_REDIRECT_URI') or config['redirect_uri']
    return config


def get_client(env: str):
    """The Keycloak client for this environment, built once per container."""
    global _oauth, _oauth_env
    if _oauth is not None and _oauth_env == env:
        return _oauth.keycloak
    config = environment_config(env)
    try:
        secret = get_secret(config['secret_name'])
    except Exception as e:
        logger.error("Could not read the Keycloak client secret: %s", e)
        raise KeycloakNotConfigured("Single sign-on is not available right now. Please try again later.") from e
    client_secret = _secret_value(secret, 'client_secret', 'secret')
    if not client_secret:
        raise KeycloakNotConfigured(
            f"Single sign-on is not set up: no client secret in {config['secret_name']}.")
    client_id = _secret_value(secret, 'client_id') or config['client_id']
    oauth = OAuth()
    oauth.register(
        name='keycloak',
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{config['issuer']}/.well-known/openid-configuration",
        client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256'},
    )
    _oauth, _oauth_env = oauth, env
    return oauth.keycloak


def redirect_uri(env: str) -> str:
    return environment_config(env)['redirect_uri']
