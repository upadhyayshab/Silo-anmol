"""
Wraps reference_docs/Soil_texture_lookup_centroid (1).py's SoilTextureLookup engine
(reads ST_classes_IND.tif from S3 via rasterio's /vsis3/ driver) for use as a FastAPI
dependency. The raster connection is opened once (module-level singleton) and reused
across requests, matching the reference engine's SoilTextureLookup design.
"""
import os
import logging
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import rasterio
from rasterio.windows import Window

from utils.constants import SOIL_TEXTURE_CLASSES

logger = logging.getLogger("SoilTextureLookup")

DEFAULT_S3_RASTER = (
    "/vsis3/kissansampurna-crops/Geospatial_SF/soil-texture-isric/IN/ST_classes_IND_COG.tif"
)
S3_RASTER_PATH = os.getenv("SOIL_TEXTURE_RASTER", DEFAULT_S3_RASTER)


# The ISRIC raster leaves many farm pixels empty (0); on dev, 11 of 54 farm
# centres hit one, and 6 of those had soil data within ~500 m. An empty pixel
# falls back to the most common class in the surrounding 5 x 5 pixels
# (~200 m each), clipped to the raster.
_NEARBY_RADIUS_PX = 2


def _nearby_soil_class(ds, row: int, col: int, valid_classes) -> Optional[int]:
    top, left = max(row - _NEARBY_RADIUS_PX, 0), max(col - _NEARBY_RADIUS_PX, 0)
    bottom = min(row + _NEARBY_RADIUS_PX + 1, ds.height)
    right = min(col + _NEARBY_RADIUS_PX + 1, ds.width)
    block = ds.read(1, window=Window(left, top, right - left, bottom - top))
    nodata = int(ds.nodata) if ds.nodata is not None else None
    counts = Counter(
        int(v) for v in block.ravel()
        if int(v) != 0 and int(v) != nodata and int(v) in valid_classes
    )
    return counts.most_common(1)[0][0] if counts else None


@dataclass
class SoilTextureResult:
    latitude: float
    longitude: float
    soil_class: Optional[int]
    texture_class: str
    status: str
    message: str = ""


class SoilTextureLookup:
    def __init__(self, s3_path: str = S3_RASTER_PATH):
        self.s3_path = s3_path
        os.environ.setdefault("AWS_DEFAULT_REGION", os.getenv("AWS_DEFAULT_REGION", "ap-south-1"))
        logger.info("Opening soil texture raster from S3...")
        self.ds = rasterio.open(self.s3_path)

    def close(self):
        try:
            if self.ds:
                self.ds.close()
                logger.info("Raster connection closed.")
        except Exception as exc:
            logger.warning("Error while closing raster: %s", exc)

    def get_texture(self, lat: float, lon: float) -> SoilTextureResult:
        try:
            ds = self.ds
            b = ds.bounds
            if not (b.left <= lon <= b.right and b.bottom <= lat <= b.top):
                return SoilTextureResult(
                    latitude=lat, longitude=lon, soil_class=None,
                    texture_class="Out of coverage area", status="out_of_bounds",
                    message="Coordinate falls outside raster extent.",
                )

            row, col = ds.index(lon, lat)
            if row < 0 or row >= ds.height or col < 0 or col >= ds.width:
                return SoilTextureResult(
                    latitude=lat, longitude=lon, soil_class=None,
                    texture_class="Out of coverage area", status="out_of_bounds",
                    message="Computed pixel falls outside raster dimensions.",
                )

            window = Window(col, row, 1, 1)
            pixel_value = int(ds.read(1, window=window)[0, 0])

            if ds.nodata is not None and pixel_value == int(ds.nodata):
                pixel_value = 0

            nearby = False
            if pixel_value == 0:
                nearby_class = _nearby_soil_class(ds, row, col, SOIL_TEXTURE_CLASSES)
                if nearby_class is not None:
                    pixel_value, nearby = nearby_class, True

            texture_class = SOIL_TEXTURE_CLASSES.get(pixel_value, "Unknown")
            if pixel_value not in SOIL_TEXTURE_CLASSES:
                logger.warning("Unexpected class value %s at lat=%s lon=%s", pixel_value, lat, lon)

            if pixel_value == 0:
                return SoilTextureResult(
                    latitude=lat, longitude=lon, soil_class=0,
                    texture_class=texture_class, status="nodata",
                    message="NoData / Road / Water / Buildings.",
                )

            return SoilTextureResult(
                latitude=lat, longitude=lon, soil_class=pixel_value,
                texture_class=texture_class, status="ok",
                message="Soil texture from the nearest mapped soil (within ~500 m)." if nearby
                else "Soil texture retrieved successfully.",
            )
        except Exception as exc:
            logger.exception("Failed query for lat=%s lon=%s", lat, lon)
            return SoilTextureResult(
                latitude=lat, longitude=lon, soil_class=None,
                texture_class="Error", status="error", message=str(exc),
            )


@lru_cache()
def get_soil_texture_lookup() -> SoilTextureLookup:
    return SoilTextureLookup()


__all__ = ["SoilTextureLookup", "SoilTextureResult", "get_soil_texture_lookup"]
