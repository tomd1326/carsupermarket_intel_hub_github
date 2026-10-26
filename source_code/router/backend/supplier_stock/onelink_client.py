import io
import logging

import requests
import pandas as pd

logger = logging.getLogger(__name__)

AUTH_URL = "https://disposalnetwork.1link.co.uk/uk/micro/users/token"
STOCKLIST_URL = "https://disposalnetwork.1link.co.uk/uk/micro/vehicles/search/stocklist"

_HEADERS_BASE = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "referer": "https://disposalnetwork.1link.co.uk/uk/tb/app/login",
    "origin": "https://disposalnetwork.1link.co.uk",
}


class OneLinkFetchClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def fetch_dataframe(self) -> pd.DataFrame:
        token = self._get_token()
        csv_text = self._fetch_stocklist(token)
        df = pd.read_csv(io.StringIO(csv_text))
        logger.info(f"1link: fetched {len(df)} rows")
        return df

    def _get_token(self) -> str:
        logger.info("1link: attempting token fetch")
        resp = requests.get(
            AUTH_URL,
            auth=(self.username, self.password),
            headers={**_HEADERS_BASE, "accept": "application/json, text/plain, */*"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"1link auth returned {resp.status_code}: {resp.text[:200]}")
        # Response is plain text — the token itself
        token = resp.text.strip()
        if not token:
            raise RuntimeError("1link auth returned empty token")
        logger.info("1link: token received")
        return token

    def _fetch_stocklist(self, token: str) -> str:
        logger.info(f"1link: sending stocklist request to {STOCKLIST_URL}")
        resp = requests.get(
            STOCKLIST_URL,
            headers={
                **_HEADERS_BASE,
                "authorization": f"Bearer {token}",
                "accept": "application/json, text/plain, */*",
                "referer": "https://disposalnetwork.1link.co.uk/uk/tb/app",
            },
            timeout=60,
        )
        logger.info(f"1link: stocklist response status {resp.status_code}, payload size {len(resp.content)} bytes")
        if resp.status_code != 200:
            logger.error(f"1link: stocklist error body: {resp.text[:500]}")
            raise RuntimeError(f"1link stocklist returned {resp.status_code}: {resp.text[:200]}")
        content = resp.content
        if content.startswith(b'\xef\xbb\xbf'):
            content = content[3:]
        text = content.decode("utf-8")
        row_count = max(0, text.count('\n') - 1)
        logger.info(f"1link: CSV received, approximately {row_count} data rows")
        return text
