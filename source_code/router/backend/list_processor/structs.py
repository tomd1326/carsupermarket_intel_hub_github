import pydantic
from typing import Optional


class VehicleRow(pydantic.BaseModel):
    vrm: str
    mileage: int
    registration_date: Optional[str] = None
    capid: Optional[int] = None

