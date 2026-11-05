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


_INCH = 914400
_MARGIN = int(0.3 * _INCH)
_CONTENT_WIDTH = PAGE_WIDTH_EMU - 2 * _MARGIN
_CONTENT_TOP = int(0.8 * _INCH)
_NOTE_TOP = int(5.2 * _INCH)
_PAGE_BOTTOM = int(5.45 * _INCH)
_CAPTION_HEIGHT = int(0.28 * _INCH)
_COLUMN_GAP = int(0.2 * _INCH)
_LABEL_WIDTH = int(2.3 * _INCH)
_TEXT_LABEL_WIDTH = int(2.4 * _INCH)
# Slides refuses a table column narrower than 32 points.
_MIN_COLUMN_WIDTH = 406400
_MAX_COLUMN_WIDTH = int(0.85 * _INCH)
_ROW_HEIGHT = int(0.2 * _INCH)
_HEADER_FILL = {"red": 0.886, "green": 0.91, "blue": 0.941}


class GoogleSlidesError(Exception):
    """The deck could not be created. The message is safe to show on the page."""


def _size_and_place(slide_id: str, x: int, y: int, w: int, h: int) -> dict:
    return {
        "pageObjectId": slide_id,
        "size": {"width": {"magnitude": w, "unit": "EMU"}, "height": {"magnitude": h, "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "EMU"},
    }


def _text_box(slide_id: str, object_id: str, text: str, x: int, y: int, w: int, h: int, size: float, bold: bool) -> list:
    """A text box. Slides refuses to insert empty text, so blank text gets no box."""
    if not text:
        return []
    return [{"createShape": {"objectId": object_id, "shapeType": "TEXT_BOX",
                             "elementProperties": _size_and_place(slide_id, x, y, w, h)}},
            {"insertText": {"objectId": object_id, "text": text}},
            {"updateTextStyle": {
                "objectId": object_id,
                "style": {"fontSize": {"magnitude": size, "unit": "PT"}, "bold": bold, "fontFamily": "DM Sans"},
                "fields": "fontSize,bold,fontFamily",
            }}]


def _table_requests(slide_id: str, table: dict) -> list:
    """A real Slides table, so the figures can be copied and edited in the deck."""
    head, rows = table["head"], table["rows"]
    table_id = f"{slide_id}_table"
    columns = len(head)
    words = bool(table.get("text"))
    font = 8 if words or columns <= 9 else 7
    label = _TEXT_LABEL_WIDTH if words else _LABEL_WIDTH
    others = columns - 1
    if others:
        share = (_CONTENT_WIDTH - label) // others
        other = max(share, _MIN_COLUMN_WIDTH) if words else min(max(share, _MIN_COLUMN_WIDTH), _MAX_COLUMN_WIDTH)
    else:
        other, label = 0, _CONTENT_WIDTH
    out = [{"createTable": {"objectId": table_id, "rows": len(rows) + 1, "columns": columns,
                            "elementProperties": _size_and_place(slide_id, _MARGIN, _CONTENT_TOP, _CONTENT_WIDTH,
                                                                 _ROW_HEIGHT * (len(rows) + 1))}},
           {"updateTableRowProperties": {"objectId": table_id, "rowIndices": list(range(len(rows) + 1)),
                                         "tableRowProperties": {"minRowHeight": {"magnitude": _ROW_HEIGHT, "unit": "EMU"}},
                                         "fields": "minRowHeight"}},
           {"updateTableColumnProperties": {"objectId": table_id, "columnIndices": [0],
                                            "tableColumnProperties": {"columnWidth": {"magnitude": label, "unit": "EMU"}},
                                            "fields": "columnWidth"}},
           {"updateTableCellProperties": {
               "objectId": table_id,
               "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": 1, "columnSpan": columns},
               "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {"color": {"rgbColor": _HEADER_FILL}}}},
               "fields": "tableCellBackgroundFill.solidFill.color",
           }}]
    if others:
        out.append({"updateTableColumnProperties": {"objectId": table_id, "columnIndices": list(range(1, columns)),
                                                    "tableColumnProperties": {"columnWidth": {"magnitude": other, "unit": "EMU"}},
                                                    "fields": "columnWidth"}})
    lines = [(head, True)] + [(row["cells"], bool(row.get("bold"))) for row in rows]
    for r, (cells, bold) in enumerate(lines):
        for c, text in enumerate(cells):
            # Slides refuses to insert or style empty text, so a blank cell is left alone.
            if not text:
                continue
            place = {"rowIndex": r, "columnIndex": c}
            out += [{"insertText": {"objectId": table_id, "cellLocation": place, "text": text}},
                    {"updateTextStyle": {
                        "objectId": table_id, "cellLocation": place, "textRange": {"type": "ALL"},
                        "style": {"fontSize": {"magnitude": font, "unit": "PT"}, "bold": bold, "fontFamily": "DM Sans"},
                        "fields": "fontSize,bold,fontFamily",
                    }}]
            if c and not words:
                out.append({"updateParagraphStyle": {
                    "objectId": table_id, "cellLocation": place, "textRange": {"type": "ALL"},
                    "style": {"alignment": "END"}, "fields": "alignment",
                }})
    return out


def _chart_lines_requests(slide_id: str, lines: list, bottom: int) -> list:
    """The charts' lines one under another, each chart with its name above it."""
    out = []
    line_height = (bottom - _CONTENT_TOP) // len(lines)
    chart_width = (_CONTENT_WIDTH - _COLUMN_GAP) // 2
    for l, line in enumerate(lines):
        y = _CONTENT_TOP + l * line_height
        for c, chart in enumerate(line):
            x = _MARGIN + c * (chart_width + _COLUMN_GAP)
            caption = " - ".join(t for t in (chart.get("title"), chart.get("subtitle")) if t)
            out += _text_box(slide_id, f"{slide_id}_c{l}_{c}", caption, x, y, chart_width, _CAPTION_HEIGHT, 9, True)
            out.append({"createImage": {
                "objectId": f"{slide_id}_i{l}_{c}", "url": chart["image_url"],
                "elementProperties": _size_and_place(slide_id, x, y + _CAPTION_HEIGHT, chart_width,
                                                     line_height - _CAPTION_HEIGHT),
            }})
    return out


def report_slide_requests(slide: dict, slide_id: str = "") -> list:
    """One Report slide: {title, subtitle, note, table: {head, rows, text} or lines:
    [[{title, subtitle, image_url}]]}, laid out by hand as _slide_requests does."""
    slide_id = slide_id or f"wbr_{uuid.uuid4().hex[:12]}"
    note = slide.get("note") or ""
    out = [{"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
    out += _text_box(slide_id, f"{slide_id}_t", slide.get("title") or "", _MARGIN, int(0.1 * _INCH), _CONTENT_WIDTH,
                     int(0.42 * _INCH), 20, True)
    out += _text_box(slide_id, f"{slide_id}_s", slide.get("subtitle") or "", _MARGIN, int(0.5 * _INCH), _CONTENT_WIDTH,
                     int(0.26 * _INCH), 10, False)
    if slide.get("table"):
        out += _table_requests(slide_id, slide["table"])
    elif slide.get("lines"):
        out += _chart_lines_requests(slide_id, slide["lines"], _NOTE_TOP if note else _PAGE_BOTTOM)
    out += _text_box(slide_id, f"{slide_id}_n", note, _MARGIN, _NOTE_TOP, _CONTENT_WIDTH, int(0.35 * _INCH), 8, False)
    return out


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

    def create_report_deck(self, title: str, subtitle: str) -> tuple:
        """A new deck holding only a title slide; returns (deck id, edit link). The
        Report's slides are added one call at a time (add_report_slide), as one
        request for the whole deck would run past the API Gateway time limit."""
        deck = self._call("POST", SLIDES_URL, json={"title": title})
        deck_id = deck["presentationId"]
        slide_id = f"wbr_{uuid.uuid4().hex[:12]}"
        requests_body = [{"deleteObject": {"objectId": deck["slides"][0]["objectId"]}},
                         {"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
        requests_body += _text_box(slide_id, f"{slide_id}_t", title, _MARGIN, int(2 * _INCH), _CONTENT_WIDTH, int(0.7 * _INCH), 32, True)
        requests_body += _text_box(slide_id, f"{slide_id}_s", subtitle, _MARGIN, int(2.7 * _INCH), _CONTENT_WIDTH, int(0.4 * _INCH), 14, False)
        self._call("POST", f"{SLIDES_URL}/{deck_id}:batchUpdate", json={"requests": requests_body})
        return deck_id, f"https://docs.google.com/presentation/d/{deck_id}/edit"

    def add_report_slide(self, deck_id: str, slide: dict) -> None:
        for line in slide.get("lines") or []:
            for chart in line:
                if len(chart["image_url"]) > 2000:
                    raise GoogleSlidesError(
                        f"A chart image link is {len(chart['image_url'])} characters; Google Slides only accepts up to 2 KB."
                    )
        self._call("POST", f"{SLIDES_URL}/{deck_id}:batchUpdate", json={"requests": report_slide_requests(slide)})

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
