# source_code/router/backend/utils/file_handler.py
from __future__ import annotations

import os
from typing import Optional

import pandas as pd


def _read_csv(path: str) -> pd.DataFrame:
    """
    Robust CSV reader:
    - Try UTF-8 with BOM first
    - Fallback to cp1252 for typical Windows exports
    """
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="cp1252", errors="replace")


def read_dataframe_file(path: str) -> pd.DataFrame:
    """
    Load .csv, .xlsx, or .xls into a DataFrame.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return _read_csv(path)
    if ext in [".xlsx", ".xls"]:
        # engine auto-selected by pandas; xls uses xlrd if available
        return pd.read_excel(path)
    raise ValueError(f"Unsupported input extension: {ext}")


def write_dataframe_file(df: pd.DataFrame, output_path: str) -> None:
    """
    Save DataFrame as:
    - .xlsx/.xls: XLSX via xlsxwriter, formatted with Aptos Narrow and no borders
    - .csv: UTF-8 with BOM
    """
    ext = os.path.splitext(output_path)[1].lower()

    if ext in [".xlsx", ".xls"]:
        with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
            sheet_name = "Sheet1"
            df.to_excel(writer, index=False, sheet_name=sheet_name)

            wb = writer.book
            ws = writer.sheets[sheet_name]

            # Formats: Aptos Narrow, no borders
            header_fmt = wb.add_format({
                "font_name": "Aptos Narrow",
                "bold": True,
                "border": 0,
            })
            base_fmt = wb.add_format({
                "font_name": "Aptos Narrow",
                "border": 0,
            })

            # Re-write headers with header format
            for colx, colname in enumerate(df.columns):
                ws.write(0, colx, colname, header_fmt)

            # Apply base format to all data cells and set column default format
            nrows, ncols = df.shape
            if ncols > 0:
                ws.set_column(0, ncols - 1, None, base_fmt)
            if nrows > 0 and ncols > 0:
                # Conditional format trick to apply a cell format to a range without conditions
                ws.conditional_format(
                    1, 0, nrows, ncols - 1,
                    {"type": "no_errors", "format": base_fmt}
                )

            # Optional: basic column autosize using displayed string lengths
            try:
                for col_idx, col in enumerate(df.columns):
                    # header width
                    max_len = len(str(col)) if col is not None else 0
                    # sample up to first 1000 rows for speed
                    series = df[col].astype(str).head(1000)
                    if not series.empty:
                        max_len = max(max_len, series.map(len).max())
                    # buffer so Aptos Narrow is not cramped
                    ws.set_column(col_idx, col_idx, min(max_len + 2, 60), base_fmt)
            except Exception:
                # Sizing is best-effort; ignore failures
                pass

        return

    if ext == ".csv":
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        return

    raise ValueError(f"Unsupported output extension: {ext}")
