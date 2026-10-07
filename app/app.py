from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from SharedBackend.managers import ApiKeyManager, EntityManager
from SharedBackend.middlewares import SDKMiddleware, EntityMiddleware
from config import get_settings, get_engine
from routers import admin_router, v1_router
from routers.manure import router as manure_router
from routers.disease import router as disease_router
from routers.manureReport import router as manure_report_router
from routers.diseaseReport import router as disease_report_router
from routers.reports import router as reports_router

settings = get_settings()
engine = get_engine(settings.name)


# Tables are created by alembic migrations only (see migrations/), never at
# startup, so the schema can't drift from alembic_version.
app = FastAPI()
app.include_router(admin_router, prefix="/admin")
app.include_router(v1_router, prefix="/api/v1")
app.include_router(manure_router)
app.include_router(disease_router)
app.include_router(manure_report_router)
app.include_router(disease_report_router)
app.include_router(reports_router)

app.add_middleware(
    CORSMiddleware,  # type: ignore
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key_manager = ApiKeyManager(engine)
app.add_middleware(
    SDKMiddleware,  # type: ignore
    key_manager=api_key_manager,
)

entity_manager = EntityManager(engine)
app.add_middleware(
    EntityMiddleware,  # type: ignore
    entity_manager=entity_manager,
)

if __name__ == '__main__':
    import uvicorn

    uvicorn.run("app:app", host="localhost", port=8080, reload=True)
