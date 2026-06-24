from fastapi import APIRouter, Depends, status

from config import get_settings, get_engine
from managers import AgencyManager, AgencySchema
from models import AgencyCreateRequest, AgencyResponse, ListResponse
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)
agency_manager = AgencyManager(engine)

router = APIRouter(prefix="/agencies", tags=["Agencies"])


@router.post("", response_model=AgencyResponse, status_code=status.HTTP_201_CREATED)
async def create_agency(
    payload: AgencyCreateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    created = await agency_manager.create(AgencySchema(name=payload.name, is_active=True))
    return AgencyResponse(uid=created.uid, name=created.name,
                          is_active=created.is_active, created_at=created.created_at)


@router.get("", response_model=ListResponse[AgencyResponse])
async def list_agencies(
    limit: int = 100, offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    res = await agency_manager.fetch_all(limit=limit, offset=offset, sorts=["name"])
    items = [AgencyResponse(uid=a.uid, name=a.name, is_active=a.is_active, created_at=a.created_at)
             for a in res.items]
    return ListResponse(items=items, count=len(items))
