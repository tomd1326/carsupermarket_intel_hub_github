import pydantic
from typing import Optional
import datetime


class VehicleRow(pydantic.BaseModel):
    vrm: str
    mileage: int
    registration_date: Optional[datetime.date] = None
    capid: Optional[int] = None

