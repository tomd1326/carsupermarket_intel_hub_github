from __future__ import annotations

import logging
import requests
from urllib.parse import urlparse

from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)


class SharePointClient:
    """
    Microsoft Graph API client for reading SharePoint Excel workbooks.

    Credentials are fetched from AWS Secrets Manager at construction time.
    The secret must contain: tenant_id, client_id, client_secret.

    Usage:
        client = SharePointClient()
        values = client.get_worksheet_values(
            subfolder="Department/Buying/B2B",
            filename="NEW DAILY BUYERS SHEET.xlsx",
            sheet_name="Today",
        )
        # values is a list of lists: values[0] = headers, values[1:] = data rows
    """

    SITE_URL = "https://carsmkt.sharepoint.com"
    TEAM_SITE = "/sites/BuyingDrive"
    SECRET_NAME = "prod/carsupermarket/sharepoint/credentials"

    def __init__(self):
        secret = get_secret(self.SECRET_NAME)
        self._tenant_id = secret["tenant_id"]
        self._client_id = secret["client_id"]
        self._client_secret = secret["client_secret"]
        self._headers = self._build_headers()

    def _build_headers(self) -> dict:
        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        resp = requests.post(url, data={
            "client_id": self._client_id,
            "client_secret": self._client_secret.strip(),
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        })
        resp.raise_for_status()
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get_site_id(self) -> str:
        hostname = urlparse(self.SITE_URL).netloc
        team_site = self.TEAM_SITE.lstrip("/")
        url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{team_site}"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.json()["id"]

    def _get_drive_id(self, site_id: str) -> str:
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        drives = resp.json().get("value", [])
        for d in drives:
            if d.get("name") == "Documents":
                return d["id"]
        return drives[0]["id"]

    def _get_item_id(self, drive_id: str, subfolder: str, filename: str) -> str:
        full_path = f"{subfolder.strip('/')}/{filename}".strip("/")
        encoded = requests.utils.quote(full_path)
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded}"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.json()["id"]

    def get_worksheet_values(self, subfolder: str, filename: str, sheet_name: str) -> list[list]:
        """
        Fetch all cell values from a named worksheet.

        Returns:
            list[list]: Row-major 2D array. Row 0 is the header row.

        Raises:
            requests.HTTPError: If any Graph API call fails.
        """
        site_id = self._get_site_id()
        drive_id = self._get_drive_id(site_id)
        item_id = self._get_item_id(drive_id, subfolder, filename)
        encoded_sheet = requests.utils.quote(sheet_name)
        url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
            f"/workbook/worksheets/{encoded_sheet}/usedRange?$select=values"
        )
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.json().get("values", [])
