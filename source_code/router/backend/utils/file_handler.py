import pandas as pd
import openpyxl as xl


def read_excel_safe(file_path: str) -> pd.DataFrame:
    wb = xl.load_workbook(file_path)
    sheet = wb.active
    data = sheet.values
    cols = next(data)
    data = list(data)
    wb.close()
    return pd.DataFrame(data, columns=cols)


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
        df.to_excel(output_file_path, index=False)
    else:
        raise ValueError("Unsupported file format. Please use a .csv or .xlsx file.")
