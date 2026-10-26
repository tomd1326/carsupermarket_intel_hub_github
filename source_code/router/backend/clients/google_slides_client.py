"""
Google Slides export for the Weekly Business Review page.

Acts as the Google account connected in Settings > Google (see
utils/google_oauth.py) and creates the deck in that account's My Drive. Scope
is drive.file, so it can only reach decks it created itself. Plain REST
through requests.
"""
import logging
import uuid

import requests

from utils import google_oauth
from utils.google_oauth import GoogleNotConnected

logger = logging.getLogger(__name__)

SLIDES_URL = "https://slides.googleapis.com/v1/presentations"

# Default Slides page size is 10in x 5.625in (16:9), in EMU (914400 per inch).
PAGE_WIDTH_EMU = 9144000
PAGE_HEIGHT_EMU = 5143500


class GoogleSlidesError(Exception):
    """The deck could not be created. The message is safe to show on the page."""


class GoogleSlidesClient:
    def __init__(self):
        self._token = None

    def _call(self, method: str, url: str, **kwargs) -> dict:
        if self._token is None:
            try:
                self._token = google_oauth.access_token()
            except GoogleNotConnected as e:
                raise GoogleSlidesError(str(e)) from e
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message") or resp.text
            except ValueError:
                message = resp.text
            logger.error(f"Google API {method} {url} failed ({resp.status_code}): {message}")
            raise GoogleSlidesError(f"Google returned {resp.status_code}: {message}")
        return resp.json() if resp.content else {}

    def create_deck(self, title: str, slides: list) -> str:
        """
        Create a deck with one slide per item of slides ([{title, subtitle,
        image_url}]), each a title, an optional subtitle and one image, and
        return its edit link. Each image_url must be a link Google can download
        the image from (max 2 KB long), e.g. an S3 presigned URL.
        """
        for s in slides:
            if len(s["image_url"]) > 2000:
                raise GoogleSlidesError(
                    f"A chart image link is {len(s['image_url'])} characters; Google Slides only accepts up to 2 KB."
                )

        # Created in the root of the connected account's My Drive.
        deck = self._call("POST", SLIDES_URL, json={"title": title})
        deck_id = deck["presentationId"]

        # The default title slide is replaced by blank ones laid out by hand,
        # so the result does not depend on the theme's placeholder positions.
        requests_body = [{"deleteObject": {"objectId": deck["slides"][0]["objectId"]}}]
        for s in slides:
            requests_body += self._slide_requests(s)
        self._call("POST", f"{SLIDES_URL}/{deck_id}:batchUpdate", json={"requests": requests_body})
        return f"https://docs.google.com/presentation/d/{deck_id}/edit"

    @staticmethod
    def _slide_requests(s: dict) -> list:
        slide_id = f"wbr_{uuid.uuid4().hex[:12]}"

        def _box(object_id, x, y, w, h):
            return {"createShape": {
                "objectId": object_id, "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": slide_id,
                    "size": {"width": {"magnitude": w, "unit": "EMU"},
                             "height": {"magnitude": h, "unit": "EMU"}},
                    "transform": {"scaleX": 1, "scaleY": 1, "translateX": x,
                                  "translateY": y, "unit": "EMU"},
                },
            }}

        def _text(object_id, text, size, bold):
            return [{"insertText": {"objectId": object_id, "text": text}},
                    {"updateTextStyle": {
                        "objectId": object_id,
                        "style": {"fontSize": {"magnitude": size, "unit": "PT"}, "bold": bold,
                                  "fontFamily": "DM Sans"},
                        "fields": "fontSize,bold,fontFamily",
                    }}]

        margin = 365760  # 0.4in
        image_top = 1005840  # 1.1in
        width = PAGE_WIDTH_EMU - 2 * margin
        out = [{"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
        if s.get("title"):
            out += [_box(f"{slide_id}_t", margin, 228600, width, 457200)] + _text(f"{slide_id}_t", s["title"], 22, True)
        # Slides refuses to insert empty text, so a blank subtitle gets no box.
        if s.get("subtitle"):
            out += [_box(f"{slide_id}_s", margin, 640080, width, 320040)] + _text(f"{slide_id}_s", s["subtitle"], 11, False)
        out.append({"createImage": {
            "objectId": f"{slide_id}_i", "url": s["image_url"],
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": {"magnitude": width, "unit": "EMU"},
                         "height": {"magnitude": PAGE_HEIGHT_EMU - image_top - margin, "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": margin,
                              "translateY": image_top, "unit": "EMU"},
            },
        }})
        return out
