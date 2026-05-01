import os
import time
import requests

_TOKEN_URL = "https://login.microsoftonline.com/a455b827-244f-4c97-b5b4-ce5d13b4d00c/oauth2/v2.0/token"
_SCOPE = "https://tapi.dvsa.gov.uk/.default"
_BASE_URL = "https://history.mot.api.gov.uk/v1"


class DVSAClient:
    def __init__(self):
        # TODO: move to AWS Secrets Manager once added
        self.client_id = os.getenv("DVSA_CLIENT_ID", "")
        self.client_secret = os.getenv("DVSA_CLIENT_SECRET", "")
        self.api_key = os.getenv("DVSA_API_KEY", "")
        self._access_token = None
        self._token_expiry = 0.0

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token
        r = requests.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret, "scope": _SCOPE},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"DVSA token request failed: {r.status_code} {r.text}")
        j = r.json()
        self._access_token = j["access_token"]
        self._token_expiry = now + int(j.get("expires_in", 3600))
        return self._access_token

    def get_vehicle_by_vrm(self, vrm: str):
        token = self._get_access_token()
        r = requests.get(
            f"{_BASE_URL}/trade/vehicles/registration/{vrm}",
            headers={"accept": "application/json", "Authorization": f"Bearer {token}", "x-api-key": self.api_key},
            timeout=30,
        )
        if r.status_code in (401, 403):
            raise PermissionError(f"DVSA auth failed ({r.status_code}): {r.text}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
