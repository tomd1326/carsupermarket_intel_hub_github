import logging
import os
import time
import requests

_TOKEN_URL = "https://login.microsoftonline.com/a455b827-244f-4c97-b5b4-ce5d13b4d00c/oauth2/v2.0/token"
_SCOPE = "https://tapi.dvsa.gov.uk/.default"
_BASE_URL = "https://history.mot.api.gov.uk/v1"

logger = logging.getLogger(__name__)

# Sentinel returned when the API confirms the vehicle is not found (404)
NOT_FOUND = object()


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

    def get_vehicle_by_vrm(self, vrm: str, debug: bool = False):
        """
        Returns:
          - parsed JSON dict/list on success
          - NOT_FOUND sentinel on confirmed 404
          - raises on other errors (with retry on 429 / non-JSON response)
        """
        _RETRIES = 3
        _RETRY_DELAYS = [2, 5, 10]

        for attempt in range(_RETRIES):
            token = self._get_access_token()
            url = f"{_BASE_URL}/trade/vehicles/registration/{vrm}"
            t0 = time.time()
            r = requests.get(
                url,
                headers={"accept": "application/json", "Authorization": f"Bearer {token}", "x-api-key": self.api_key},
                timeout=30,
            )
            elapsed = round(time.time() - t0, 2)

            if debug:
                content_type = r.headers.get("Content-Type", "")
                body_preview = r.text[:500].replace("\n", " ")
                logger.info(
                    "[DVSA] %s | attempt=%d | status=%d | %.2fs | content-type=%s | body=%s",
                    vrm, attempt + 1, r.status_code, elapsed, content_type, body_preview,
                )

            if r.status_code in (401, 403):
                raise PermissionError(f"DVSA auth failed ({r.status_code}): {r.text}")

            if r.status_code == 404:
                return NOT_FOUND

            if r.status_code == 429:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                logger.warning("[DVSA] %s rate-limited (429), retrying in %ds", vrm, delay)
                time.sleep(delay)
                continue

            if r.status_code >= 500:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                logger.warning("[DVSA] %s server error %d, retrying in %ds", vrm, r.status_code, delay)
                time.sleep(delay)
                continue

            r.raise_for_status()

            # Guard against HTML error pages returned with a 200 status
            content_type = r.headers.get("Content-Type", "")
            if "json" not in content_type:
                body_preview = r.text[:200]
                logger.warning("[DVSA] %s non-JSON response (attempt %d): %s", vrm, attempt + 1, body_preview)
                if attempt < _RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise RuntimeError(f"DVSA returned non-JSON for {vrm}: {body_preview}")

            return r.json()

        raise RuntimeError(f"DVSA request failed for {vrm} after {_RETRIES} attempts")
