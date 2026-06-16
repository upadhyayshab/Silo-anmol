from pydantic import BaseModel
from typing import Optional

from .generateApiKeyModels import *
from .healthCheckModels import *
from .erpModels import *
from .authModels import *
from .crmModels import *
from .leadModels import *


class ListResponse[ModelType: BaseModel](BaseModel):
    items: list[ModelType]
    count: int


class StatusResponse(BaseModel):
    status: str = "ok"
    message: Optional[str] = None
