"""Cell values from a downloaded .xlsx, in the shape the Graph cell reads
used to return: a list of rows, each a list of cells, row 0 the first row of
the range (or a table's header row).

Reading the file with calamine takes about 0.1s where asking Graph for the
cells took 5 to 18s on the same files, and the download itself is 2 to 6s.

Cells come back as Graph gave them, so the pages need no change: "" for a
blank cell, int for a whole number, float otherwise, str, bool. Dates are the
one difference - Graph gave the Excel serial number, here they are ISO text
(2026-10-30, or 2026-10-30 09:15:00 with a time); every page's date parser
accepts both.
"""
import datetime
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from python_calamine import CalamineWorkbook

_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PKG_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _cell(v):
    if isinstance(v, datetime.datetime):
        return v.date().isoformat() if v.time() == datetime.time(0) else v.isoformat(sep=" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (datetime.time, datetime.timedelta)):
        return str(v)
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return int(v)
    if isinstance(v, str):
        # Excel keeps a line break inside a cell as \r\n; Graph gave \n.
        return v.replace("\r\n", "\n")
    return v


def _col_index(letters: str) -> int:
    """1-based column number for a column letter: A=1, Z=26, AA=27."""
    n = 0
    for ch in letters.strip().upper():
        n = n * 26 + ord(ch) - 64
    return n


def _trim(rows: list) -> list:
    while rows and all(c in (None, "") for c in rows[-1]):
        rows.pop()
    return rows


def _block(rows: list, first_row: int, last_row: int, first_col: int, last_col: int) -> list:
    """Rows first_row..last_row (1-based, inclusive) cut to columns
    first_col..last_col, every row padded to that width, trailing blank rows
    removed."""
    width = last_col - first_col + 1
    out = []
    for row in rows[first_row - 1:last_row]:
        cells = [_cell(c) for c in row[first_col - 1:last_col]]
        out.append(cells + [""] * (width - len(cells)))
    return _trim(out)


def _workbook(data: bytes) -> CalamineWorkbook:
    return CalamineWorkbook.from_filelike(io.BytesIO(data))


def _sheet_rows(wb: CalamineWorkbook, sheet_name: str) -> list:
    if sheet_name not in wb.sheet_names:
        raise ValueError(f"Sheet '{sheet_name}' not found. Sheets: {wb.sheet_names}")
    # skip_empty_area=False keeps empty leading rows and columns, so row 0 is
    # row 1 of the sheet and column 0 is column A, as with a Graph range read.
    return wb.get_sheet_by_name(sheet_name).to_python(skip_empty_area=False)


def sheet_values(data: bytes, sheet_name: str, max_col: str, max_row: int) -> list:
    """Cells A1:{max_col}{max_row} of a sheet, trailing blank rows removed."""
    rows = _sheet_rows(_workbook(data), sheet_name)
    return _block(rows, 1, int(max_row), 1, _col_index(max_col))


def _tables(data: bytes) -> dict:
    """{table name: (sheet name, ref, totals rows)} from the file's own table
    definitions: xl/tables/*.xml, each linked from its sheet's rels, the sheet
    named in xl/workbook.xml."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())

        def rels(part: str) -> dict:
            rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            if rel_part not in names:
                return {}
            out = {}
            for rel in ET.fromstring(z.read(rel_part)):
                target = rel.get("Target") or ""
                if target.startswith("/"):
                    target = target.lstrip("/")
                else:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
                out[rel.get("Id")] = target
            return out

        sheet_parts = {}
        wb_rels = rels("xl/workbook.xml")
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{_MAIN_NS}sheet"):
            part = wb_rels.get(sheet.get(f"{_REL_NS}id"))
            if part:
                sheet_parts[part] = sheet.get("name")

        tables = {}
        for part, sheet_name in sheet_parts.items():
            for target in rels(part).values():
                if not re.match(r"xl/tables/.*\.xml$", target) or target not in names:
                    continue
                root = ET.fromstring(z.read(target))
                name = root.get("displayName") or root.get("name")
                tables[name] = (sheet_name, root.get("ref"), int(root.get("totalsRowCount") or 0))
        return tables


def table_names(data: bytes) -> list:
    return list(_tables(data))


def table_values(data: bytes, table_name: str) -> list:
    """A named table as rows: row 0 its header row, then its data rows."""
    tables = _tables(data)
    if table_name not in tables:
        raise ValueError(f"Table '{table_name}' not found. Tables: {list(tables)}")
    sheet_name, ref, totals_rows = tables[table_name]
    m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", ref or "")
    if not m:
        raise ValueError(f"Table '{table_name}' has an unexpected range: {ref}")
    first_col, first_row, last_col, last_row = _col_index(m.group(1)), int(m.group(2)), _col_index(m.group(3)), int(m.group(4))
    rows = _sheet_rows(_workbook(data), sheet_name)
    return _block(rows, first_row, last_row - totals_rows, first_col, last_col)


def sheet_names(data: bytes) -> list:
    return list(_workbook(data).sheet_names)
