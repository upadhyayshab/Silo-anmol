from typing import Optional

from pydantic import BaseModel


class CropModel(BaseModel):
    """Full crop directory entry: name in every supported language plus core attributes."""
    uid: str
    code: int
    name_en: str
    name_hn: Optional[str] = None
    name_kn: Optional[str] = None
    name_ta: Optional[str] = None
    name_te: Optional[str] = None
    name_ml: Optional[str] = None
    name_mr: Optional[str] = None
    name_gu: Optional[str] = None
    name_bn: Optional[str] = None
    name_pa: Optional[str] = None
    name_ur: Optional[str] = None
    name_ne: Optional[str] = None
    name_or: Optional[str] = None
    name_as: Optional[str] = None
    family: str
    horizon: str
    water_need: str
    cost_class: str


__all__ = ["CropModel"]
