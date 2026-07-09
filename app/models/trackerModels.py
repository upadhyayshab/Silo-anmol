from datetime import date
from decimal import Decimal

from pydantic import BaseModel, field_validator

__all__ = ["TrackerInputUpsert"]


class TrackerInputUpsert(BaseModel):
    tracker_date: date
    region: str
    metric_key: str
    value: Decimal

    @field_validator("value")
    @classmethod
    def non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("value must be >= 0")
        return v
