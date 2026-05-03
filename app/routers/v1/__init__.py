from fastapi import APIRouter, status

from config import get_settings, get_engine
from models import *

from .users import router as users_router
from .outlets import router as outlets_router
from .products import router as products_router
from .inventory import router as inventory_router
from .orders import router as orders_router
from .invoices import router as invoices_router
from .transfers import router as transfers_router
from .reports import router as reports_router
from .dashboard import router as dashboard_router
from .config import router as config_router
from .notifications import router as notifications_router
from .activity_logs import router as activity_logs_router
from .transactions import router as transactions_router
from .outlet_collections import router as outlet_collections_router
from .outlet_payouts import router as outlet_payouts_router
from .crm import router as crm_router
from .delivery_guys import router as delivery_guys_router
from .outlet_mappings import router as outlet_mappings_router
from .delivery_handovers import router as delivery_handovers_router

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter()

# ERP routers
router.include_router(users_router)
router.include_router(outlets_router)
router.include_router(products_router)
router.include_router(inventory_router)
router.include_router(orders_router)
router.include_router(invoices_router)
router.include_router(transfers_router)
router.include_router(reports_router)
router.include_router(dashboard_router)
router.include_router(config_router)
router.include_router(notifications_router)
router.include_router(activity_logs_router)
router.include_router(transactions_router)
router.include_router(outlet_collections_router)
router.include_router(outlet_payouts_router)
router.include_router(crm_router)
router.include_router(delivery_guys_router)
router.include_router(outlet_mappings_router)
router.include_router(delivery_handovers_router)



@router.get("/health-check", response_model=HealthCheckResponse)
async def health_check():
    return HealthCheckResponse(
        status=f"{settings.name}[node:{0}/{0}] is running with {0}/{0} passing",
        version=settings.version,
        commit=settings.commit,
        branch=settings.branch,
        build_time=settings.build_time,
        build_number=settings.build_number,
        build_tags=settings.build_tags,
    )
