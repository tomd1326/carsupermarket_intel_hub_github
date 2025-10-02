# source_code/router/backend/list_processor/structs.py

from pydantic import BaseModel, Field
from typing import Optional
import datetime


class VehicleRow(BaseModel):
    """
    Pydantic model for vehicle data row.
    Handles both scenarios: files with existing CAPIDs and files needing VRM lookup.
    """
    vrm: str = Field(..., description="Vehicle Registration Mark (e.g., ABC123XYZ)")
    mileage: int = Field(..., description="Vehicle mileage in miles", ge=0)
    capid: Optional[int] = Field(None, description="CAP ID (5-digit integer) - may be None if needs VRM lookup")
    registration_date: Optional[datetime.date] = Field(None, description="Vehicle registration date")

    class Config:
        # Allow datetime.date serialization
        json_encoders = {
            datetime.date: lambda v: v.isoformat() if v else None
        }

    @property
    def has_capid(self) -> bool:
        """Returns True if CAPID is available (from source file)"""
        return self.capid is not None

    @property
    def needs_vrm_lookup(self) -> bool:
        """Returns True if VRM API lookup is needed to get CAPID"""
        return self.capid is None