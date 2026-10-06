from pydantic import BaseModel

from .generateApiKeyModels import *
from .healthCheckModels import *
from .manureModels import *
from .diseaseModels import *
from .manureReportModels import *
from .diseaseReportModels import *


class ListResponse[ModelType: BaseModel](BaseModel):
    items: list[ModelType]
    count: int


class StatusResponse(BaseModel):
    status: str = "ok"
