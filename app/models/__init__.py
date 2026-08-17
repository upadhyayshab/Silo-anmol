from pydantic import BaseModel

from .generateApiKeyModels import *
from .healthCheckModels import *
from .soilModels import *
from .optionsModels import *
from .recommendationModels import *
from .cropModels import *


class ListResponse[ModelType: BaseModel](BaseModel):
    items: list[ModelType]
    count: int


class StatusResponse(BaseModel):
    status: str = "ok"
