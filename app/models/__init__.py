from pydantic import BaseModel

from .generateApiKeyModels import *
from .healthCheckModels import *
from .erpModels import *
from .authModels import *


class ListResponse[ModelType: BaseModel](BaseModel):
    items: list[ModelType]
    count: int


class StatusResponse(BaseModel):
    status: str = "ok"
    message: Optional[str] = None


from typing import Optional
