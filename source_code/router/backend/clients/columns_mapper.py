import datetime
import re

from pydantic import ValidationError
from typing import Optional, Dict
import pandas as pd
from clients.structs import VehicleRow
import logging


# Get the logger
logger = logging.getLogger(__name__)


def _parse_price(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = re.sub(r'[^\d.]', '', str(value))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


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
        # Convert all keys in the row to lowercase for case-insensitive comparison
        lowercased_row = {key.lower(): value for key, value in row.items()}

        # Define required keys
        required_mileage_keys = ["miles", "mileage", "odo", "odom"]
        required_vrm_keys = ["vrm", "reg", "regno"]

        # Initialize flags for missing keys
        mileage_found = any(key in lowercased_row for key in required_mileage_keys)
        vrm_found = any(key in lowercased_row for key in required_vrm_keys)

        # Log and raise an exception if mileage or VRM is missing
        if not mileage_found or not vrm_found:
            if not mileage_found:
                logger.error("Mileage column not matched in row")
            if not vrm_found:
                logger.error("VRM column not matched in row")
            raise ValueError("Essential column(s) missing")

        # Process mileage
        mileage = None
        for key in required_mileage_keys:
            if key in lowercased_row:
                if isinstance(lowercased_row[key], int):
                    mileage = lowercased_row[key]
                elif isinstance(lowercased_row[key], str):
                    # Remove non-digit characters and convert to integer
                    mileage_str = re.sub(r"[^\d]", "", lowercased_row[key])
                    mileage = int(mileage_str) if mileage_str else None
                break

        # Process VRM
        vrm = None
        for key in required_vrm_keys:
            if key in lowercased_row:
                vrm = lowercased_row[key]
                break

        if mileage is None or vrm is None:
            logger.error("Could not extract both mileage and VRM from row")
            return None

        lowercased_row = {key.lower(): value for key, value in row.items()}
        asking_price = _parse_price(lowercased_row.get('price'))

        return VehicleRow(
            vrm=vrm,
            mileage=mileage,
            asking_price=asking_price,
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

        lowercased_row = {key.lower(): value for key, value in row.items()}
        asking_price = _parse_price(lowercased_row.get('buy now price'))

        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Mileage"]),
            asking_price=asking_price,
            capid=capid,
            registration_date=registration_date,
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

        lowercased_row = {key.lower(): value for key, value in row.items()}
        asking_price = _parse_price(lowercased_row.get('buy now price'))

        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Odometer"]),
            asking_price=asking_price,
            capid=capid,
            registration_date=registration_date,
        )


class UniversalColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        # Match VRM / Registration Number / License Plate
        vrm = None
        for pattern in ["vrm", "reg.*[n#][uo]?"]:
            for col in row.keys():
                if pd.isnull(row[col]):
                    continue
                match_col_name = re.match(fr'{pattern}', col, re.IGNORECASE)
                if match_col_name and re.match(r"^[A-Z]{2,3}\d{2,3}[A-Z]{2,3}$", str(row[col])):
                    vrm = row[col]

        # Match CAP ID
        capid = None
        for pattern in [r"cap[\s_-]?(id|code)"]:
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
                        value_row = row[col]
                        if not isinstance(value_row, (int, float)):
                            value_row = re.sub(r"[^\d.]", "", str(value_row))
                            # Remove all dots but not the last one
                            value_row = re.sub(r"(\.)(?=.*\.)", "", value_row)
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
                    # if instance of pd.Timestamp
                    if isinstance(row[col], pd.Timestamp):
                        registration_date = row[col].date()
                        break
                    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", '%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            registration_date = datetime.datetime.strptime(row[col], fmt).date()
                            break # TODO: need to break even for one more level up
                        except ValueError:
                            pass
        if not registration_date:
            for col in row.keys():
                if not pd.isnull(row[col]) and "year" in col.lower():
                    registration_date = get_registration_date(int(row[col]), int(vrm[2:4]))

        asking_price = None
        for col in row.keys():
            if 'price' in col.lower() and not pd.isnull(row[col]):
                asking_price = _parse_price(row[col])
                break

        try:
            return VehicleRow(
                vrm=vrm,
                mileage=mileage,
                asking_price=asking_price,
                capid=capid,
                registration_date=registration_date,
            )
        except ValidationError as e:
            logger.warning(f"Error validating row: {e}")
            return None


# Mapper for Purchase Pro
class PurchaseProColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> Optional[VehicleRow]:
        try:
            capid = row.get("CAP ID", float('nan'))
            capid = int(capid)
        except ValueError:
            capid = None

        registration_date = None
        if not pd.isnull(row["Date of Registration"]):
            for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", '%Y-%m-%d', '%Y/%m/%d']:
                try:
                    registration_date = datetime.datetime.strptime(row["Date of Registration"], fmt).date()
                    break
                except ValueError:
                    pass

        return VehicleRow(
            vrm=row["VRM"],
            mileage=int(row["Mileage"]),
            capid=capid,
            registration_date=registration_date,
        )


# Factory to get the appropriate mapper
def get_columns_mapper(supplier_name: str) -> BaseColumnsMapper:
    if supplier_name == "Enterprise":
        return EnterpriseColumnsMapper()
    elif supplier_name == "Motability":
        return MotabilityColumnsMapper()
    elif supplier_name == "1link":
        return OneLinkColumnsMapper()
    elif supplier_name == "Purchase Pro":
        return PurchaseProColumnsMapper()
    elif supplier_name == "Universal":
        return UniversalColumnsMapper()
    else:
        raise ValueError(f"Unknown supplier name: {supplier_name}")
