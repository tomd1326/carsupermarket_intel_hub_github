from __future__ import annotations

import base64
import logging
import os
import threading
import time
import requests
from urllib.parse import urlparse

from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

# AAD client-credentials tokens are valid ~60-90 min; cache per tenant+client so
# concurrent/repeated SharePointClient() instances in the same process don't each
# pay for a fresh token round-trip (the file/drive resolution below still runs
# per-file, but the auth handshake is the one part safe to share globally).
_TOKEN_CACHE: dict = {}

# Graph site/drive/item IDs are stable identifiers — a site keeps its ID for
# life, and a file keeps its item ID until it is deleted and recreated (editing
# or saving over it does not change the ID). Resolving them cost three
# sequential Graph round-trips on EVERY worksheet read, before a single cell of
# data was returned, which dominated the fetch time.
#
# These are cached process-wide with a long TTL. If a cached ID goes stale
# (file deleted and recreated, library restructured) Graph returns 404/410 and
# get_worksheet_values clears the entry and retries once from scratch, so a
# stale ID self-heals on the next call rather than needing a redeploy.
_ID_CACHE: dict = {}
_ID_CACHE_TTL_SECONDS = 12 * 60 * 60
_id_cache_lock = threading.Lock()


# Sheet values last read, with the file's last-saved time at that read, for
# get_worksheet_values_if_changed. Also saved to S3 when a key is given, so a
# fresh Lambda can reuse another's read.
_SHEET_CACHE: dict = {}
_sheet_cache_lock = threading.Lock()
_SHEET_S3_BUCKET = "aa-raw-sources-uk"


def _sheet_s3_load(s3_key: str) -> dict | None:
    try:
        import boto3, json
        obj = boto3.client("s3", region_name="eu-west-1").get_object(Bucket=_SHEET_S3_BUCKET, Key=s3_key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _sheet_s3_save(s3_key: str, entry: dict) -> None:
    try:
        import boto3, json
        boto3.client("s3", region_name="eu-west-1").put_object(
            Bucket=_SHEET_S3_BUCKET, Key=s3_key,
            Body=json.dumps(entry, default=str).encode(), ContentType="application/json",
        )
    except Exception as e:
        logger.warning("SharePoint sheet S3 save failed for %s: %s", s3_key, e)


def _id_cache_get(key):
    with _id_cache_lock:
        entry = _ID_CACHE.get(key)
        if entry and time.monotonic() < entry["expires"]:
            return entry["value"]
    return None


def _id_cache_put(key, value):
    with _id_cache_lock:
        _ID_CACHE[key] = {"value": value, "expires": time.monotonic() + _ID_CACHE_TTL_SECONDS}


def _id_cache_drop(key):
    with _id_cache_lock:
        _ID_CACHE.pop(key, None)


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
        cache_key = (self._tenant_id, self._client_id)
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and time.monotonic() < cached["expires"]:
            return {"Authorization": f"Bearer {cached['token']}", "Accept": "application/json"}

        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        resp = requests.post(url, data={
            "client_id": self._client_id,
            "client_secret": self._client_secret.strip(),
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        })
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        # expires_in is seconds from AAD (typically 3600); refresh a little early
        expires_in = int(body.get("expires_in", 3600))
        _TOKEN_CACHE[cache_key] = {"token": token, "expires": time.monotonic() + max(60, expires_in - 120)}
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _site_cache_key(self):
        return ("site", urlparse(self.SITE_URL).netloc, self.TEAM_SITE.lstrip("/"))

    def _get_site_id(self) -> str:
        key = self._site_cache_key()
        cached = _id_cache_get(key)
        if cached is not None:
            return cached
        hostname = urlparse(self.SITE_URL).netloc
        team_site = self.TEAM_SITE.lstrip("/")
        url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{team_site}"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        site_id = resp.json()["id"]
        _id_cache_put(key, site_id)
        return site_id

    def _get_drive_id(self, site_id: str) -> str:
        key = ("drive", site_id)
        cached = _id_cache_get(key)
        if cached is not None:
            return cached
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        drives = resp.json().get("value", [])
        drive_id = None
        for d in drives:
            if d.get("name") == "Documents":
                drive_id = d["id"]
                break
        if drive_id is None:
            drive_id = drives[0]["id"]
        _id_cache_put(key, drive_id)
        return drive_id

    def _get_item_id(self, drive_id: str, subfolder: str, filename: str) -> str:
        key = ("item", drive_id, subfolder, filename)
        cached = _id_cache_get(key)
        if cached is not None:
            return cached
        full_path = f"{subfolder.strip('/')}/{filename}".strip("/")
        encoded = requests.utils.quote(full_path)
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded}"
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        item_id = resp.json()["id"]
        _id_cache_put(key, item_id)
        return item_id

    def _drop_cached_ids(self, subfolder: str, filename: str) -> None:
        """Forget cached IDs for this file so the next call re-resolves from scratch."""
        site_key = self._site_cache_key()
        site_id = _id_cache_get(site_key)
        if site_id is not None:
            drive_id = _id_cache_get(("drive", site_id))
            if drive_id is not None:
                _id_cache_drop(("item", drive_id, subfolder, filename))
            _id_cache_drop(("drive", site_id))
        _id_cache_drop(site_key)

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

    def get_last_modified_by_url(self, sharepoint_url: str, timeout: float = 30) -> str:
        """When the file at sharepoint_url was last saved (ISO string). One small
        call that does not open the workbook, so a file can be checked for
        changes before paying for a slow read."""
        return self.get_file_info_by_url(sharepoint_url, timeout=timeout)["lastModifiedDateTime"]

    def get_file_info_by_url(self, sharepoint_url: str, timeout: float = 30) -> dict:
        """{name, size, lastModifiedDateTime} for the file at sharepoint_url, in
        one small call that does not open the workbook."""
        encoded = base64.urlsafe_b64encode(sharepoint_url.encode()).rstrip(b"=").decode()
        url = (f"https://graph.microsoft.com/v1.0/shares/u!{encoded}/driveItem"
               "?$select=name,size,lastModifiedDateTime")
        resp = requests.get(url, headers=self._headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _item_ids_by_url(self, sharepoint_url: str) -> tuple[str, str]:
        """(drive_id, item_id) for a link, remembered like the other IDs so a
        read does not pay for the shares lookup every time."""
        key = ("url", sharepoint_url)
        cached = _id_cache_get(key)
        if cached is not None:
            return cached
        ids = self._get_item_id_by_url(sharepoint_url)
        _id_cache_put(key, ids)
        return ids

    def _get_by_url(self, sharepoint_url: str, path: str, timeout: float) -> requests.Response:
        """GET drives/{drive}/items/{item}/<path> for the file at sharepoint_url.
        A 404/410 can mean the remembered IDs are out of date (file deleted and
        recreated), so they are dropped and looked up again once."""
        for attempt in (1, 2):
            drive_id, item_id = self._item_ids_by_url(sharepoint_url)
            resp = requests.get(f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/{path}",
                                headers=self._headers, timeout=timeout)
            if resp.status_code in (404, 410) and attempt == 1:
                _id_cache_drop(("url", sharepoint_url))
                continue
            resp.raise_for_status()
            return resp

    def get_file_content_by_url(self, sharepoint_url: str, timeout: float = 120) -> bytes:
        """The whole file at sharepoint_url. Downloading the file and reading
        it here (utils/workbook_values) takes 2 to 6s on the files the app
        uses, where asking Graph for the cells of one sheet took 5 to 18s:
        Graph has to open the workbook in Excel Online for that, and the
        download is a plain file read."""
        return self._get_by_url(sharepoint_url, "content", timeout).content

    @classmethod
    def folder_url(cls, subfolder: str) -> str:
        """The link that opens a folder of the Documents library in a browser."""
        return f"{cls.SITE_URL}{cls.TEAM_SITE}/Documents/{requests.utils.quote(subfolder.strip('/'))}"

    def list_folder(self, subfolder: str, timeout: float = 20) -> tuple[str, list[dict]]:
        """(drive_id, files directly inside subfolder) on this client's site.
        Subfolders are left out. Each file has id, name, size and
        lastModifiedDateTime."""
        drive_id = self._get_drive_id(self._get_site_id())
        encoded = requests.utils.quote(subfolder.strip("/"))
        url = (f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded}:/children"
               "?$select=id,name,size,file,lastModifiedDateTime&$top=200")
        files = []
        while url:
            resp = requests.get(url, headers=self._headers, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            files += [item for item in body.get("value", []) if "file" in item]
            url = body.get("@odata.nextLink")
        return drive_id, files

    def get_item_content(self, drive_id: str, item_id: str, timeout: float = 120) -> bytes:
        """The whole file with this ID."""
        resp = requests.get(f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content",
                            headers=self._headers, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    def _subfolder_id(self, drive_id: str, parent: str, name: str) -> str:
        """The ID of folder `name` inside `parent`, created if it is not there.
        A 409 on creating means another read has just made it."""
        base = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:"
        path = requests.utils.quote(f"{parent.strip('/')}/{name}")
        resp = requests.get(f"{base}/{path}", headers=self._headers, timeout=30)
        if resp.status_code == 404:
            resp = requests.post(f"{base}/{requests.utils.quote(parent.strip('/'))}:/children", headers=self._headers,
                                 json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
                                 timeout=30)
            if resp.status_code == 409:
                resp = requests.get(f"{base}/{path}", headers=self._headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["id"]

    def move_to_subfolder(self, drive_id: str, item_id: str, parent: str, name: str, filename: str) -> None:
        """Move a file into folder `name` inside `parent`. A file of the same
        name already there is kept: this one gets the end of its ID added. Needs
        write access to the site, which reading the app's files does not."""
        folder_id = self._subfolder_id(drive_id, parent, name)
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
        resp = requests.patch(url, headers=self._headers, json={"parentReference": {"id": folder_id}}, timeout=30)
        if resp.status_code == 409:
            stem, ext = os.path.splitext(filename)
            resp = requests.patch(url, headers=self._headers, timeout=30,
                                  json={"parentReference": {"id": folder_id}, "name": f"{stem} ({item_id[-6:]}){ext}"})
        resp.raise_for_status()

    def get_last_modified(self, subfolder: str, filename: str) -> str:
        """Same as get_last_modified_by_url, for a file found by folder and name
        on this client's site. Uses the cached site/drive/item IDs, so it is
        one small call once those are known."""
        for attempt in (1, 2):
            site_id = self._get_site_id()
            drive_id = self._get_drive_id(site_id)
            item_id = self._get_item_id(drive_id, subfolder, filename)
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}?$select=lastModifiedDateTime"
            resp = requests.get(url, headers=self._headers, timeout=30)
            if resp.status_code in (404, 410) and attempt == 1:
                # File deleted and recreated: its cached ID is gone. Re-resolve once.
                self._drop_cached_ids(subfolder, filename)
                continue
            resp.raise_for_status()
            return resp.json()["lastModifiedDateTime"]

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

    def get_worksheet_values_by_url(self, sharepoint_url: str, sheet_name: str,
                                    max_col: str = "Z", max_row: int = 2000) -> list[list]:
        """
        Fetch cell values from a named worksheet in the file at sharepoint_url,
        bounded to A1:{max_col}{max_row}. For sheets that are not a named Table.

        Returns:
            list[list]: Row-major 2D array. Row 0 is the header row.
        """
        drive_id, item_id = self._get_item_id_by_url(sharepoint_url)
        encoded_sheet = requests.utils.quote(sheet_name)
        address = requests.utils.quote(f"A1:{max_col}{max_row}")
        url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
            f"/workbook/worksheets/{encoded_sheet}/range(address='{address}')?$select=values"
        )
        resp = requests.get(url, headers=self._headers)
        resp.raise_for_status()
        values = resp.json().get("values", [])
        # Trim fully-blank trailing rows Graph pads the bounded range with
        while values and all(c in (None, "") for c in values[-1]):
            values.pop()
        logger.info("SharePoint sheet '%s': fetched %d rows", sheet_name, len(values))
        return values

    def get_worksheet_values(self, subfolder: str, filename: str, sheet_name: str,
                              max_col: str = "Z", max_row: int = 2000) -> list[list]:
        """
        Fetch cell values from a named worksheet, bounded to A1:{max_col}{max_row}.

        usedRange forces Graph to scan the sheet's entire used extent (all
        formatting included), which is disproportionately slow for large workbooks
        — tens of seconds for sheets with only ~1000 data rows. Requesting a fixed
        bounded range is much faster for Graph to compute and is generous enough
        for any sheet with a couple dozen columns and up to max_row data rows.

        Returns:
            list[list]: Row-major 2D array. Row 0 is the header row.

        Raises:
            requests.HTTPError: If any Graph API call fails.
        """
        # Site/drive/item IDs come from the process-wide cache when warm, so the
        # common path is a single Graph call (the range read) rather than four.
        # On a 404/410 the cached IDs may be stale (file recreated), so drop them
        # and retry once with a full re-resolve.
        for attempt in (1, 2):
            started = time.monotonic()
            site_id = self._get_site_id()
            drive_id = self._get_drive_id(site_id)
            item_id = self._get_item_id(drive_id, subfolder, filename)
            resolve_secs = time.monotonic() - started

            encoded_sheet = requests.utils.quote(sheet_name)
            address = requests.utils.quote(f"A1:{max_col}{max_row}")
            url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{encoded_sheet}/range(address='{address}')?$select=values"
            )
            read_started = time.monotonic()
            resp = requests.get(url, headers=self._headers)
            if resp.status_code in (404, 410) and attempt == 1:
                logger.warning(
                    "SharePoint '%s': %s on range read — dropping cached IDs and retrying",
                    filename, resp.status_code,
                )
                self._drop_cached_ids(subfolder, filename)
                continue
            resp.raise_for_status()
            read_secs = time.monotonic() - read_started
            values = resp.json().get("values", [])
            # Trim fully-blank trailing rows Graph pads the bounded range with
            while values and all(c in (None, "") for c in values[-1]):
                values.pop()
            logger.info(
                "SharePoint '%s' sheet '%s': %d rows in %.2fs (id resolve %.2fs, range read %.2fs)",
                filename, sheet_name, len(values), resolve_secs + read_secs, resolve_secs, read_secs,
            )
            return values

    def get_worksheet_values_if_changed(self, subfolder: str, filename: str, sheet_name: str,
                                        max_col: str = "Z", max_row: int = 2000,
                                        s3_key: str = None) -> list[list]:
        """
        get_worksheet_values, but the slow range read only happens when the file
        has been saved since the last read. First asks when the file was last
        saved (one small call that does not open the workbook); if that matches
        the last read, held in memory or in S3 at s3_key, those values are
        returned instead.

        If the last-saved check fails, falls back to a normal read.
        """
        key = (self.TEAM_SITE, subfolder, filename, sheet_name, max_col, max_row)
        try:
            modified = self.get_last_modified(subfolder, filename)
        except Exception as e:
            logger.warning("SharePoint '%s': last-saved check failed, reading anyway: %s", filename, e)
            modified = None
        if modified:
            with _sheet_cache_lock:
                held = _SHEET_CACHE.get(key)
            if not (held and held.get("modified") == modified) and s3_key:
                held = _sheet_s3_load(s3_key)
            if held and held.get("modified") == modified:
                with _sheet_cache_lock:
                    _SHEET_CACHE[key] = held
                logger.info("SharePoint '%s' sheet '%s': not saved since last read (%s), reusing it",
                            filename, sheet_name, modified)
                return held["values"]
        values = self.get_worksheet_values(subfolder, filename, sheet_name, max_col=max_col, max_row=max_row)
        if modified:
            # The last-saved time is taken before the read, so a save made during
            # the read shows as a change next time and is read then.
            entry = {"modified": modified, "values": values}
            with _sheet_cache_lock:
                _SHEET_CACHE[key] = entry
            if s3_key:
                _sheet_s3_save(s3_key, entry)
        return values
