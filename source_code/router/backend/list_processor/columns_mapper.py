import datetime
import re

from pydantic import BaseModel
from typing import Optional, Dict
from list_processor.structs import VehicleRow


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
    def map_row(self, row: Dict) -> VehicleRow:
        raise NotImplementedError("This method needs to be implemented by subclasses.")


# Mapper for Enterprise
class EnterpriseColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> VehicleRow:
        mileage = re.sub(r"[^\d]", "", row["MILES"])
        if mileage == "":
            mileage = 0
        else:
            mileage = int(mileage)
        return VehicleRow(
            vrm=row["REGNO"],
            mileage=mileage
        )


# Mapper for Motability
class MotabilityColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> VehicleRow:
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
    def map_row(self, row: Dict) -> VehicleRow:
        try:
            capid = row.get("CAP ID", float('nan'))
            capid = int(capid)
        except ValueError:
            capid = None

        # Parse the date
        registration_date = None
        # parse different formats
        for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"]:
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


# Factory to get the appropriate mapper
def get_columns_mapper(supplier_name: str) -> BaseColumnsMapper:
    if supplier_name == "Enterprise":
        return EnterpriseColumnsMapper()
    elif supplier_name == "Motability":
        return MotabilityColumnsMapper()
    elif supplier_name == "1link":
        return OneLinkColumnsMapper()
    else:
        raise ValueError(f"Unknown supplier name: {supplier_name}")
