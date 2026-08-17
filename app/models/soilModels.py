from typing import Optional

from pydantic import BaseModel


class SoilTextureResponse(BaseModel):
    latitude: float
    longitude: float
    soil_class: Optional[int]
    texture_class: str
    status: str
    message: str = ""
    lang: str = "en"


__all__ = ["SoilTextureResponse"]
