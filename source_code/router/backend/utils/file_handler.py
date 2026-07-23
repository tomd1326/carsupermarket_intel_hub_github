import pandas as pd
import openpyxl as xl

# Excel's own default is Calibri; users here have Aptos as their default font,
# so every workbook we produce is normalised to it.
DEFAULT_FONT_NAME = 'Aptos'
DEFAULT_FONT_SIZE = 11


def set_default_font(wb, name: str = DEFAULT_FONT_NAME, size: int = DEFAULT_FONT_SIZE):
    """Point the workbook's default font (font id 0) at `name`.

    Every cell without an explicit font uses font id 0, so rewriting that one
    entry restyles the whole sheet. The Normal named style is updated to match,
    but on its own it has no effect — openpyxl writes styles.xml from the
    registered font list, so the entry in wb._fonts is the one that counts.

    Cells given an explicit Font() elsewhere keep that font, so build any
    bold/coloured fonts with Font(name=DEFAULT_FONT_NAME, ...).
    """
    default_font = list(wb._fonts)[0]
    default_font.name = name
    default_font.sz = size
    # Theme-linked colours make Excel fall back to the theme's minor font,
    # which is Calibri in openpyxl's built-in theme.
    default_font.scheme = None
    normal = wb._named_styles['Normal']
    normal.font = default_font
    return wb


def read_excel_safe(file_path: str) -> pd.DataFrame:
    wb = xl.load_workbook(file_path, read_only=True)
    try:
        sheet = wb.active
        data = list(sheet.values)
    finally:
        wb.close()
    cols = data[0]
    return pd.DataFrame(data[1:], columns=cols)


def read_dataframe_file(input_file_path: str) -> pd.DataFrame:
    if input_file_path.endswith('.csv'):
        df = pd.read_csv(input_file_path)
    elif input_file_path.endswith('.xlsx') or input_file_path.endswith('.xls'):
        df = read_excel_safe(input_file_path)
    else:
        raise ValueError("Unsupported file format. Please use a .csv or .xlsx file.")

    return df


def write_dataframe_file(df: pd.DataFrame, output_file_path: str):
    if output_file_path.endswith('.csv'):
        df.to_csv(output_file_path, index=False)
    elif output_file_path.endswith('.xlsx') or output_file_path.endswith('.xls'):
        with pd.ExcelWriter(output_file_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
            set_default_font(writer.book)
    else:
        raise ValueError("Unsupported file format. Please use a .csv or .xlsx file.")
