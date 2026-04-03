from pydantic import BaseModel
from typing import Optional
from utils.constants import LeadSource

class CrmPayload:
    class Lead(BaseModel):
        customer_name:str
        customer_phone:str
        customer_email:str
        LastName:Optional[str]=None
        Created_On:Optional[str]=None
        State:Optional[str]=None
        Country:Optional[str]=None
        address_line:str
        address_line_2:Optional[str]=None
        city:Optional[str]=None
        district:Optional[str]=None
        Source:Optional[LeadSource]=None


__all__ =["CrmPayload"]