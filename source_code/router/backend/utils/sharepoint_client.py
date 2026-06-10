from __future__ import annotations

import base64
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

        # To read a named Table from a different site:
        client = SharePointClient(team_site="/teams/marketing")
        rows = client.get_table_rows(
            subfolder="Reports",
            filename="Daily Spend and Enquiries.xlsx",
            table_name="Table1",
        )
        # rows is a list of dicts keyed by column header
    """

    SITE_URL = "https://carsmkt.sharepoint.com"
    TEAM_SITE = "/sites/BuyingDrive"
    SECRET_NAME = "prod/carsupermarket/sharepoint/credentials"

    def __init__(self, team_site: str = None):
        if team_site is not None:
            self.TEAM_SITE = team_site
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

    def _get_item_id_by_url(self, sharepoint_url: str) -> tuple[str, str]:
        """
        Resolve a SharePoint file URL to (drive_id, item_id) via the Graph shares endpoint.
        This bypasses site/drive traversal and works regardless of the document library name.
        """
        # Graph requires the URL base64-encoded with a specific prefix
        encoded = base64.urlsafe_b64encode(sharepoint_url.encode()).rstrip(b"=").decode()
        share_token = f"u!{encoded}"
        url = f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        item = resp.json()
        # parentReference.driveId is the drive that owns this item
        drive_id = item["parentReference"]["driveId"]
        item_id = item["id"]
        return drive_id, item_id

    def get_table_rows(self, table_name: str, sharepoint_url: str = None,
                       subfolder: str = None, filename: str = None) -> list[dict]:
        """
        Fetch all rows from a named Excel Table as a list of dicts keyed by column header.

        Preferred: pass sharepoint_url (the URL from the browser/Share Link) to resolve
        the file directly without drive traversal.

        Fallback: pass subfolder + filename to resolve via the site's Documents drive.

        Returns:
            list[dict]: One dict per data row; keys are the table column headers.
        """
        if sharepoint_url:
            drive_id, item_id = self._get_item_id_by_url(sharepoint_url)
        else:
            site_id = self._get_site_id()
            drive_id = self._get_drive_id(site_id)
            item_id = self._get_item_id(drive_id, subfolder, filename)

        encoded_table = requests.utils.quote(table_name)
        base = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"

        # Fetch column headers
        resp = requests.get(f"{base}/workbook/tables/{encoded_table}/columns?$select=name", headers=self._headers)
        resp.raise_for_status()
        columns = [c["name"] for c in resp.json().get("value", [])]

        # Fetch rows (paginated — Graph returns max 5000 rows per call)
        # Each item in body["value"] is a row object with a "values" property: [[v1, v2, ...]]
        rows_url = f"{base}/workbook/tables/{encoded_table}/rows"
        all_rows = []
        while rows_url:
            resp = requests.get(rows_url, headers=self._headers)
            resp.raise_for_status()
            body = resp.json()
            for row_obj in body.get("value", []):
                cell_values = row_obj.get("values", [[]])[0]
                all_rows.append(dict(zip(columns, cell_values)))
            rows_url = body.get("@odata.nextLink")

        logger.info("SharePoint table '%s': fetched %d rows", table_name, len(all_rows))
        return all_rows

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
