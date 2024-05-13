import re

from pydantic import BaseModel
from typing import Optional, Dict
from list_processor.structs import VehicleRow


def get_registration_date(year: int, vrm_middle_numbers: int) -> str:
    if vrm_middle_numbers == year % 100:
        month = "03"
    elif vrm_middle_numbers % 10 == year % 10:
        month = "09"
    else:
        month = "01"

    return f"01/{month}/{year}"

# Base mapper class
class BaseColumnsMapper:
    def map_row(self, row: Dict) -> VehicleRow:
        raise NotImplementedError("This method needs to be implemented by subclasses.")


# Mapper for Enterprise
class EnterpriseColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> VehicleRow:
        return VehicleRow(
            vrm=row["REGNO"],
            mileage=int(row["MILES"])
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

        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Mileage"]),
            capid=int(row.get("CAP ID", 0)),  # Assuming CAP ID might be absent, default to 0
            registration_date=registration_date
        )


# Mapper for 1Link
class OneLinkColumnsMapper(BaseColumnsMapper):
    def map_row(self, row: Dict) -> VehicleRow:
        return VehicleRow(
            vrm=row["Reg No."],
            mileage=int(row["Odometer"]),
            capid=int(row.get("CAP ID", 0)),
            registration_date=row["Date Of Registration"]
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
