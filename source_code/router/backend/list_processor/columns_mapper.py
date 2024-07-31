import datetime
import re

from pydantic import BaseModel, ValidationError
from typing import Optional, Dict
import pandas as pd
from list_processor.structs import VehicleRow
import logging


# Get the logger
logger = logging.getLogger(__name__)


def get_registration_date(year: int, vrm_middle_numbers: int) -> datetime.date:
    if vrm_middle_numbers == year % 100:
        month = "03"
    elif vrm_middle_numbers % 10 == year % 10:
        month = "09"
    else:
        month = "01"

    return datetime.date(year, int(month), 1)


# Base mapper class
class BaseColumnsMapper:
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        raise NotImplementedError("This method needs to be implemented by subclasses.")


class EnterpriseColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        mileage = 0
        mileage_keys = ["MILES", "Mileage", "ODO"]
        
        for key in mileage_keys:
            if key in row:
                if isinstance(row[key], int):
                    mileage = row[key]
                elif isinstance(row[key], str):
                    mileage = re.sub(r"[^\d]", "", row[key])
                    mileage = int(mileage)
                break
        
        return VehicleRow(
            vrm=row["REGNO"],
            mileage=mileage
        )


# Mapper for Motability
class MotabilityColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        vrm = row["Reg No."]
        if re.match(r"^[A-Z]{2}\d{2}[A-Z]{3}$", vrm):
            vrm_middle_numbers = int(vrm[2:4])
            registration_date = get_registration_date(int(row["Year"]), vrm_middle_numbers)
        else:
            registration_date = None

        try:
            capid = row.get("CAP ID", float('nan'))
            capid = int(capid)
        except ValueError:
            capid = None

        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Mileage"]),
            capid=capid,
            registration_date=registration_date
        )


# Mapper for 1Link
class OneLinkColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        try:
            capid = row.get("CAP ID", float('nan'))
            capid = int(capid)
        except ValueError:
            capid = None

        # Parse the date
        registration_date = None
        # parse different formats
        if not pd.isnull(row["Date Of Registration"]):
            for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", '%Y-%m-%d', '%Y/%m/%d']:
                try:
                    registration_date = datetime.datetime.strptime(row["Date Of Registration"], fmt).date()
                    break
                except ValueError:
                    pass

        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Odometer"]),
            capid=capid,
            registration_date=registration_date
        )


class UniversalColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        # Match VRM / Registration Number / License Plate
        vrm = None
        for pattern in ["vrm", "reg.*n[uo]?"]:
            for col in row.keys():
                if pd.isnull(row[col]):
                    continue
                match_col_name = re.match(fr'{pattern}', col, re.IGNORECASE)
                if match_col_name and re.match(r"^[A-Z]{2,3}\d{2,3}[A-Z]{2,3}$", str(row[col])):
                    vrm = row[col]

        # Match CAP ID
        capid = None
        for pattern in ["cap[\s_-]?(id|code)"]:
            for col in row.keys():
                if pd.isnull(row[col]):
                    continue
                match_col_name = re.match(fr'{pattern}', col, re.IGNORECASE)
                if match_col_name and re.match(r"\d{5}", str(row[col])):
                    try:
                        capid = int(row[col])
                    except ValueError:
                        pass

        # Match mileage
        mileage = None
        for key in ["mileage", "miles", "odo"]:
            for col in row.keys():
                if pd.isnull(row[col]):
                    continue
                if key in col.lower():
                    try:
                        value_row = re.sub(r"[^\d]", "", str(row[col]))
                        mileage = int(value_row)
                    except ValueError:
                        pass

        # Match registration date
        registration_date = None
        for pattern in ["date", r"reg.*n([\s-]*[^uo]|$)", 'dfr']:
            for col in row.keys():
                if pd.isnull(row[col]):
                    continue
                match_col_name = re.match(fr'.*{pattern}.*', col, re.IGNORECASE)
                if match_col_name:
                    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", '%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            registration_date = datetime.datetime.strptime(row[col], fmt).date()
                            break
                        except ValueError:
                            pass
        if not registration_date:
            for col in row.keys():
                if "year" in col.lower() and not pd.isnull(row[col]):
                    registration_date = get_registration_date(int(row[col]), int(vrm[2:4]))

        try:
            return VehicleRow(
                vrm=vrm,
                mileage=mileage,
                capid=capid,
                registration_date=registration_date
            )
        except ValidationError as e:
            logger.warning(f"Error validating row: {e}")
            return None


# Factory to get the appropriate mapper
def get_columns_mapper(supplier_name: str) -> BaseColumnsMapper:
    if supplier_name == "Enterprise":
        return EnterpriseColumnsMapper()
    elif supplier_name == "Motability":
        return MotabilityColumnsMapper()
    elif supplier_name == "1link":
        return OneLinkColumnsMapper()
    elif supplier_name in ("Universal", "Generic"):
        return UniversalColumnsMapper()
    else:
        raise ValueError(f"Unknown supplier name: {supplier_name}")
