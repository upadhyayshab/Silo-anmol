import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from SharedBackend.managers import ApiKeyManager, BaseSchema, EntityManager
from SharedBackend.middlewares import SDKMiddleware, EntityMiddleware
from managers import EventOutboxManager
from managers.cropReportManagers import CropReportManager
from config import get_settings, get_engine
from routers import admin_router, v1_router, v2_router
from scripts.seed_crop_reports import seed_crop_reports
from workers.event_outbox_replayer import run_replay_loop

settings = get_settings()
engine = get_engine(settings.name)
outbox_manager = EventOutboxManager(engine)


logger = logging.getLogger("app")
CROPS_FILE = Path(__file__).resolve().parent / "data" / "crops.json"


def _warn_if_memory_broker() -> None:
    """BROKER_TYPE defaults to "memory", which logs CropGuideCompleted events
    and drops them -- no farm is ever created in Agro. That default once went
    to a deployed environment unnoticed; on a server it must be loud."""
    # ECS (Fargate platform 1.4+) sets ECS_CONTAINER_METADATA_URI_V4 in every
    # container; nothing in our task definitions sets FARGATE.
    on_server = os.getenv("ECS_CONTAINER_METADATA_URI_V4") or os.getenv("FARGATE")
    if settings.broker_type == "memory" and on_server:
        logger.critical(
            "BROKER_TYPE=memory on a deployed server: CropGuideCompleted events are dropped and "
            "Agro will never create wizard farms. Set BROKER_TYPE=kafka and MSK_BOOTSTRAP_SERVERS."
        )


async def _seed_crop_reports() -> None:
    """crop_reports holds the 139 crop info pages read by the wizard report
    and /crops/{id}/report. Nothing in the deploy ran the seed script, so dev
    had none and every info page was missing. The seed upserts by crop_id, so
    running it on every start also picks up crops.json changes; the advisory
    lock stops two tasks starting together from racing on crop_id. A failure
    is logged, not fatal: the service starts and the next start retries."""
    manager = CropReportManager(engine)
    try:
        async with manager.session_factory() as session:
            if engine.dialect.name == "postgresql":
                await session.execute(db.text("SELECT pg_advisory_xact_lock(hashtextextended('seed_crop_reports', 0))"))
            count = await seed_crop_reports(manager, session, str(CROPS_FILE))
            await session.commit()
        logger.info(f"crop_reports up to date ({count} crops)")
    except Exception as exc:
        logger.error(f"crop_reports seed failed; crop info pages may be missing: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _warn_if_memory_broker()
    async with engine.begin() as conn:
        if settings.supports_schema:
            await conn.execute(db.text(f'CREATE SCHEMA IF NOT EXISTS "{settings.name}"'))
        await conn.run_sync(BaseSchema.metadata.create_all)
    await _seed_crop_reports()

    replay_task = asyncio.create_task(run_replay_loop(outbox_manager))
    try:
        yield
    finally:
        replay_task.cancel()
        try:
            await replay_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.include_router(admin_router, prefix="/admin")
app.include_router(v1_router, prefix="/api/v1")
app.include_router(v2_router, prefix="/api/v2")
# The specs (TRD, integration spec, SDUI search_endpoint) use /api/v6 paths;
# same routes, hidden from the OpenAPI docs so each appears once.
app.include_router(v2_router, prefix="/api/v6", include_in_schema=False)

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
