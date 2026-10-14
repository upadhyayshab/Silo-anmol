# Crop Guide & Agromonitoring Integration — Full Spec, Built on /api/v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement every requirement in the spec (Channel 1 farm-profile reads, Channel 2 event dispatch with a transactional outbox fallback, pincode-based zone fallback, wizard session idempotency, and the full crop-report/dossier system) entirely under the existing `/api/v2` namespace — not by merging into `/api/v1/recommend` or mounting at `/api/v6`, which the spec's own §1.2/§5 literally say to do. That merge was considered and explicitly overridden earlier this session: `/api/v2/wizard/submit` stays the submission endpoint, permanently.

**Architecture:** Builds additively on top of commit `f4711f3` (already on `origin/dev`), which gave `/api/v2/wizard/submit` subscription quota metering, `recommendation_history` audit writes, and active-crop rotation baseline. This plan adds: (1) an `EventBrokerService` wrapper around the existing MSK producer with a `broker_type` config switch and a PostgreSQL `event_outbox` fallback table for when Kafka is down, replayed by a continuous in-process background worker started from `app.py`'s existing `lifespan` context manager (no separate process, no Celery); (2) a direct pincode-to-zone fallback in the scoring engine for farmers without GPS; (3) a `wizard_session_id` passthrough for cross-repo idempotency; (4) a new, from-scratch crop-report/dossier subsystem (`crop_reports` master catalog seeded from the spec's 104-crop `crops.json`, `user_crop_reports` per-run cache, 3 read endpoints) under `/api/v2/reports`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic, `confluent-kafka` (existing MSK producer), `unittest.IsolatedAsyncioTestCase` (this repo's test convention — no pytest).

**Spec:** `E:\AGRO\kissan-agromonitoring\docs\MSK\requirementdocforreviewcropguideintegration\Requirement Document- Crop Guide & Agromonitoring Integration .md` — read alongside this plan. Every `§` reference in this plan points there.

## Global Constraints

- **Additive-only schema changes.** Every migration in this plan only adds columns or creates new tables — never alters, renames, or drops an existing column (explicit instruction this session; also matches this repo's and the sibling Agromonitoring repo's established convention).
- **`/api/v2` only.** No endpoint in this plan mounts under `/api/v1` or `/api/v6`, regardless of what the spec's literal paths say. `/api/v2/wizard/submit` remains the one submission endpoint — it is never merged into `/api/v1/recommend`, and `/api/v1/recommend` is not touched by this plan.
- **No fabricated data.** The spec's example `RecommendationResponse`/`CropRecommendationItem` payloads (not reproduced in this plan — the real `SingleCropDossier`/`UnifiedCropReportResponse` models in §5.4 are what's implemented) include fields like `profit_per_acre` that don't exist in the real seed data (`crops.json` has `cost_per_acre_inr` + `profit_potential` + `profit_note`, never a single profit number). This plan follows §5.4's actual Pydantic models, not the narrative examples elsewhere in the spec — nothing is invented to fill a field the seed data can't back.
- **No new task-queue dependency, and no separate process.** The spec says a "Celery background worker" replays the outbox; this codebase has no Celery anywhere, and the replayer is NOT a standalone script needing its own deployment/orchestration (explicit instruction this session — the consumer runs inside the service itself). It's an `asyncio.create_task` spawned from `app.py`'s existing `lifespan` context manager, living and dying with the FastAPI process, same technique already used there for the startup schema-creation step.
- **Test convention:** every test file uses `unittest.IsolatedAsyncioTestCase` / `unittest.TestCase` with `unittest.mock` — matches every existing test in `tests/`. No pytest, no new test framework.
- **Channel 1 (spec §5 Phase 3's `AgromonitoringClient`) already exists — no task touches it.** `app/services/agroInternalService.py`'s `AgroInternalService.get_farm(farm_id, user_id)` already does exactly what the spec's `AgromonitoringClient.get_farm_profile` describes: same headers (`X-Internal-Service`/`X-Internal-Secret`/`X-User-Id`), same 2.0s timeout default (spec §Phase 1's `agromonitoring_timeout_seconds` default), same graceful-`None`-on-any-failure contract. Renaming it to match the spec's class name would be pure churn.
- **`broker_type` only supports `memory` and `kafka`.** The spec lists `sqs` as a third option; nothing in this codebase talks to SQS and building an unused client is scope creep. `sqs` is accepted as a config value (so it round-trips cleanly later) but `EventBrokerService` raises `NotImplementedError` if it's ever selected — loud failure, not a silent no-op.

## Review Focus

- **Outbox replay must not double-publish.** A replayed event that *did* actually reach Kafka the first time (producer succeeded but the HTTP response path crashed before marking the outbox row) must not be re-published and double-create a farm. Task 2's replay step checks `status` is still `PENDING` inside the same transaction that flips it to `PROCESSING`, so a second replayer run (or a concurrent one) can't double-send — pinned by a test in Task 3.
- **Pincode fallback must not silently produce a wrong zone for a malformed pincode.** `pincode="abc123"` or a pincode with no `pincode_zone_mappings` row must resolve to "zone unresolved, skip zone filter" (existing behavior for unmapped pincodes) — not raise, and not silently match zone 0. Pinned in Task 4.
- **`wizard_session_id` is optional and must never block submission.** A client that doesn't send it (older app versions) must still submit successfully; it's forwarded as `null` in the broker event, not defaulted to a generated UUID (that would hide real duplicate-submission bugs behind a per-request-unique value, defeating the whole point of idempotency). Pinned in Task 5.
- **Seeding `crop_reports` must be idempotent.** Running the seed script twice (e.g. on every deploy) must upsert by `crop_id`, not create duplicate rows or crash on a unique-constraint violation. Pinned in Task 6.
- **A crop with no `crop_reports` match must not break the unified report.** The scoring engine's `crop.name_en` and `crop_reports.name_en` are matched case-insensitively, same fallback pattern already used for rotation lookups elsewhere in this codebase — but if a crop genuinely has no master-catalog row (seed data gap), `get_or_create_unified_report` must skip that crop's dossier (log + omit), not 500 the whole report for every crop in the run. Pinned in Task 8.

---

## Task 1: Config, `event_outbox` table, and additive migration

**Files:**
- Modify: `app/config.py`
- Create: `app/managers/cropReportManagers.py`
- Modify: `app/managers/__init__.py`
- Create: `migrations/versions/f1a2b3c4d5e6_add_crop_guide_v6_schema.py`
- Test: `tests/test_cropreport_managers.py`

**Interfaces:**
- Produces: `Settings.broker_type: str` (`"memory"` | `"kafka"` | `"sqs"`, default `"memory"`). `EventOutboxSchema(BaseSchema)` with columns `topic: str`, `event_key: Optional[str]`, `payload: dict` (JSON), `status: str` (default `"PENDING"`), `retry_count: int` (default `0`), `last_error: Optional[str]`. `EventOutboxManager(GenericManager[EventOutboxSchema])`. `CropReportSchema(BaseSchema)` with the 27 columns listed below, PK is the inherited `uid`, plus a unique `crop_id: str`. `CropReportManager(GenericManager[CropReportSchema])`. `UserCropReportSchema(BaseSchema)` with columns `user_id, farm_id, recommendation_id, farm_name, season, language, top_crop_id, top_crop_name, top_crop_score, total_recommendations, report_data` (JSON). `UserCropReportManager(GenericManager[UserCropReportSchema])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cropreport_managers.py
"""Schema/manager existence and shape — no DB touched, mirrors
tests/test_farm_schema_v6_columns.py's style in the sibling Agro repo."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestEventOutboxSchema(TestCase):
    def test_columns_exist(self):
        from managers.cropReportManagers import EventOutboxSchema
        cols = EventOutboxSchema.__table__.columns.keys()
        for col in ("uid", "topic", "event_key", "payload", "status", "retry_count", "last_error"):
            self.assertIn(col, cols)

    def test_status_defaults_to_pending(self):
        from managers.cropReportManagers import EventOutboxSchema
        self.assertEqual(EventOutboxSchema.__table__.columns["status"].default.arg, "PENDING")


class TestCropReportSchema(TestCase):
    def test_columns_match_seed_data_shape(self):
        from managers.cropReportManagers import CropReportSchema
        cols = CropReportSchema.__table__.columns.keys()
        for col in (
            "uid", "crop_id", "name_en", "name_local", "family", "crop_type", "seasons",
            "duration_days", "years_to_first_harvest", "suitable_soil_types", "soil_texture",
            "soil_drainage", "soil_ph_min", "soil_ph_max", "water_requirement",
            "cost_per_acre_inr", "expected_yield_per_acre", "yield_unit", "profit_potential",
            "sowing_windows", "profit_note", "weather", "risks", "tips",
            "soil_economic_benefits", "hero_image_url", "icon_url", "is_active",
            "data_source", "review_status",
        ):
            self.assertIn(col, cols)

    def test_crop_id_is_unique(self):
        from managers.cropReportManagers import CropReportSchema
        self.assertTrue(CropReportSchema.__table__.columns["crop_id"].unique)


class TestUserCropReportSchema(TestCase):
    def test_columns_exist(self):
        from managers.cropReportManagers import UserCropReportSchema
        cols = UserCropReportSchema.__table__.columns.keys()
        for col in (
            "uid", "user_id", "farm_id", "recommendation_id", "farm_name", "season",
            "language", "top_crop_id", "top_crop_name", "top_crop_score",
            "total_recommendations", "report_data",
        ):
            self.assertIn(col, cols)


class TestBrokerTypeSetting(TestCase):
    def test_defaults_to_memory(self):
        from config import get_settings
        self.assertEqual(get_settings().broker_type, "memory")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && PYTHONPATH=".;../SharedBackend/src" ../.venv/Scripts/python.exe -m unittest ../tests/test_cropreport_managers.py -v` (Windows: use `;` in `PYTHONPATH`, run from repo root as `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_cropreport_managers -v`)
Expected: FAIL / ImportError — `managers.cropReportManagers` does not exist, `Settings` has no `broker_type`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/managers/cropReportManagers.py
"""New, additive schemas for the crop-guide spec's event outbox and
report/dossier subsystem. Kept in their own file — cropManagers.py already
holds the scoring-table managers and is large enough."""
import sqlalchemy as db
from SharedBackend.managers import BaseSchema, GenericManager


class EventOutboxSchema(BaseSchema):
    """Transactional-outbox fallback for Channel 2 (spec §3.3 Scenario 6B):
    when the direct Kafka publish fails, the event is parked here instead
    of being dropped, and a standalone replayer (Task 2/3) retries it."""
    __tablename__ = "event_outbox"

    topic = db.Column(db.String(128), nullable=False)
    event_key = db.Column(db.String(128), nullable=True)
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="PENDING")
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)


class EventOutboxManager(GenericManager[EventOutboxSchema]):
    pass


class CropReportSchema(BaseSchema):
    """Master agronomic dossier catalog — one row per crop, seeded from the
    spec's crops.json (104 crops). `crop_id` is the natural-key slug from
    that file (e.g. 'amla'); `uid` (from BaseSchema) stays the real PK."""
    __tablename__ = "crop_reports"

    crop_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    name_en = db.Column(db.String(128), nullable=False, index=True)
    name_local = db.Column(db.JSON, nullable=False)
    family = db.Column(db.String(64), nullable=False)
    crop_type = db.Column(db.String(32), nullable=False)
    seasons = db.Column(db.JSON, nullable=False)
    duration_days = db.Column(db.Integer, nullable=True)
    years_to_first_harvest = db.Column(db.Numeric(4, 2), nullable=True)
    suitable_soil_types = db.Column(db.JSON, nullable=False)
    soil_texture = db.Column(db.JSON, nullable=False)
    soil_drainage = db.Column(db.String(64), nullable=False)
    soil_ph_min = db.Column(db.Numeric(3, 1), nullable=False)
    soil_ph_max = db.Column(db.Numeric(3, 1), nullable=False)
    water_requirement = db.Column(db.String(32), nullable=False)
    cost_per_acre_inr = db.Column(db.Integer, nullable=False)
    expected_yield_per_acre = db.Column(db.Numeric(10, 2), nullable=False)
    yield_unit = db.Column(db.String(32), nullable=False)
    profit_potential = db.Column(db.String(32), nullable=False)
    sowing_windows = db.Column(db.JSON, nullable=False)
    profit_note = db.Column(db.Text, nullable=False)
    weather = db.Column(db.JSON, nullable=False)
    risks = db.Column(db.JSON, nullable=False)
    tips = db.Column(db.JSON, nullable=False)
    soil_economic_benefits = db.Column(db.JSON, nullable=False)
    hero_image_url = db.Column(db.String(512), nullable=True)
    icon_url = db.Column(db.String(512), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    data_source = db.Column(db.Text, nullable=True)
    review_status = db.Column(db.String(32), nullable=False, default="ai_draft")


class CropReportManager(GenericManager[CropReportSchema]):
    pass


class UserCropReportSchema(BaseSchema):
    """One row per recommendation run (not per crop) — report_data holds the
    full hydrated dossier list for every ranked crop in that run, per TRD
    §5.4 UnifiedCropReportResponse.crops / §2.1's 'user_crop_reports'."""
    __tablename__ = "user_crop_reports"

    user_id = db.Column(db.String(64), nullable=False, index=True)
    farm_id = db.Column(db.String(64), nullable=True, index=True)
    recommendation_id = db.Column(db.String(64), nullable=False, index=True)
    farm_name = db.Column(db.String(128), nullable=True)
    season = db.Column(db.String(32), nullable=False)
    language = db.Column(db.String(10), nullable=False, default="en")
    top_crop_id = db.Column(db.String(64), nullable=False)
    top_crop_name = db.Column(db.String(128), nullable=False)
    top_crop_score = db.Column(db.Integer, nullable=False)
    total_recommendations = db.Column(db.Integer, nullable=False, default=1)
    report_data = db.Column(db.JSON, nullable=False)


class UserCropReportManager(GenericManager[UserCropReportSchema]):
    pass


__all__ = [
    "EventOutboxSchema", "EventOutboxManager",
    "CropReportSchema", "CropReportManager",
    "UserCropReportSchema", "UserCropReportManager",
]
```

Add to `app/managers/__init__.py`:
```python
from .exampleManager import *
from .cropManagers import *
from .cropReportManagers import *
```

Add to `app/config.py`'s `Settings` class (after the existing `internal_service_secret` line):
```python
    broker_type: str = os.getenv("BROKER_TYPE", "memory")  # memory | kafka | sqs
```

```python
# migrations/versions/f1a2b3c4d5e6_add_crop_guide_v6_schema.py
"""Add event_outbox, crop_reports, user_crop_reports, and
recommendation_history audit columns (spec §5 Phase 6) — additive only.
Column names/types copied verbatim from the spec's Phase 6 migration
block — farm_id, farm_name, livestock, current_crop_code, sowing_date.
(wizard_session_id is NOT one of these — per spec §3.3 item 4 it only
flows through the CropGuideCompleted broker event, Task 5; it is not a
recommendation_history column.)

Revision ID: f1a2b3c4d5e6
Revises: d3e4f5a6b7c8
Create Date: 2026-10-14 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("recommendation_history", sa.Column("farm_id", sa.String(255), nullable=True))
    op.add_column("recommendation_history", sa.Column("farm_name", sa.String(255), nullable=True))
    op.add_column("recommendation_history", sa.Column("livestock", sa.String(255), nullable=True))
    op.add_column("recommendation_history", sa.Column("current_crop_code", sa.String(255), nullable=True))
    op.add_column("recommendation_history", sa.Column("sowing_date", sa.Date(), nullable=True))

    op.create_table(
        "event_outbox",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("idx_event_outbox_status", "event_outbox", ["status"])

    op.create_table(
        "crop_reports",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("crop_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name_en", sa.String(128), nullable=False),
        sa.Column("name_local", sa.JSON(), nullable=False),
        sa.Column("family", sa.String(64), nullable=False),
        sa.Column("crop_type", sa.String(32), nullable=False),
        sa.Column("seasons", sa.JSON(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("years_to_first_harvest", sa.Numeric(4, 2), nullable=True),
        sa.Column("suitable_soil_types", sa.JSON(), nullable=False),
        sa.Column("soil_texture", sa.JSON(), nullable=False),
        sa.Column("soil_drainage", sa.String(64), nullable=False),
        sa.Column("soil_ph_min", sa.Numeric(3, 1), nullable=False),
        sa.Column("soil_ph_max", sa.Numeric(3, 1), nullable=False),
        sa.Column("water_requirement", sa.String(32), nullable=False),
        sa.Column("cost_per_acre_inr", sa.Integer(), nullable=False),
        sa.Column("expected_yield_per_acre", sa.Numeric(10, 2), nullable=False),
        sa.Column("yield_unit", sa.String(32), nullable=False),
        sa.Column("profit_potential", sa.String(32), nullable=False),
        sa.Column("sowing_windows", sa.JSON(), nullable=False),
        sa.Column("profit_note", sa.Text(), nullable=False),
        sa.Column("weather", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("tips", sa.JSON(), nullable=False),
        sa.Column("soil_economic_benefits", sa.JSON(), nullable=False),
        sa.Column("hero_image_url", sa.String(512), nullable=True),
        sa.Column("icon_url", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("data_source", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(32), nullable=False, server_default="ai_draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("idx_crop_reports_family", "crop_reports", ["family"])

    op.create_table(
        "user_crop_reports",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("farm_id", sa.String(64), nullable=True),
        sa.Column("recommendation_id", sa.String(64), nullable=False),
        sa.Column("farm_name", sa.String(128), nullable=True),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("top_crop_id", sa.String(64), nullable=False),
        sa.Column("top_crop_name", sa.String(128), nullable=False),
        sa.Column("top_crop_score", sa.Integer(), nullable=False),
        sa.Column("total_recommendations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("report_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("idx_user_crop_reports_user_id", "user_crop_reports", ["user_id"])
    op.create_index("idx_user_crop_reports_rec_id", "user_crop_reports", ["recommendation_id"])
    op.create_index("idx_user_crop_reports_farm_id", "user_crop_reports", ["farm_id"])


def downgrade():
    op.drop_table("user_crop_reports")
    op.drop_table("crop_reports")
    op.drop_table("event_outbox")
    op.drop_column("recommendation_history", "sowing_date")
    op.drop_column("recommendation_history", "current_crop_code")
    op.drop_column("recommendation_history", "livestock")
    op.drop_column("recommendation_history", "farm_name")
    op.drop_column("recommendation_history", "farm_id")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /e/AGRO/ks_crop_recommendation_system_v2 && PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_cropreport_managers -v`
Expected: `OK` (5 tests).

Verify the migration itself with the project's established method (never run against a real DB):
Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m alembic upgrade head --sql | tail -80`
Expected: SQL output ending with `CREATE TABLE user_crop_reports ...` and the five `ALTER TABLE recommendation_history ADD COLUMN ...` statements, no errors.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/managers/cropReportManagers.py app/managers/__init__.py migrations/versions/f1a2b3c4d5e6_add_crop_guide_v6_schema.py tests/test_cropreport_managers.py
git commit -m "feat: add broker_type config, event_outbox, and crop-report schema (additive)"
```

---

## Task 2: `EventBrokerService` — broker_type switch + outbox fallback

**Files:**
- Create: `app/services/eventBrokerService.py`
- Test: `tests/test_event_broker_service.py`

**Interfaces:**
- Consumes: `app.config.get_settings().broker_type` (Task 1). `app.managers.EventOutboxManager`, `EventOutboxSchema` (Task 1). `services.kafka_client.publish_event(topic, payload, key=None)` (existing).
- Produces: `EventBrokerService` with `async def publish(self, topic: str, payload: dict, key: str | None = None) -> None` — never raises. On `broker_type == "memory"`, logs and returns (no network call — this is what lets unit tests and local dev run without live MSK credentials, spec §Phase 1/§Phase 9). On `broker_type == "kafka"`, calls `publish_event(topic, json.dumps(payload), key=key)`; on any exception, writes the payload to `event_outbox` with `status="PENDING"` instead of swallowing it silently. On `broker_type == "sqs"`, raises `NotImplementedError("SQS broker_type is not implemented")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_broker_service.py
import sys
import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestEventBrokerServiceMemoryMode(IsolatedAsyncioTestCase):
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_memory_mode_never_calls_real_publish(self, mock_publish, mock_get_settings):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "memory"

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")

        mock_publish.assert_not_called()


class TestEventBrokerServiceKafkaMode(IsolatedAsyncioTestCase):
    @patch("services.eventBrokerService.EventOutboxManager")
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_kafka_mode_publishes_directly_on_success(
        self, mock_publish, mock_get_settings, mock_outbox_manager_cls
    ):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "kafka"
        mock_outbox_instance = mock_outbox_manager_cls.return_value
        mock_outbox_instance.create = AsyncMock()

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")

        mock_publish.assert_awaited_once()
        topic_arg, payload_arg = mock_publish.call_args.args[0], mock_publish.call_args.args[1]
        self.assertEqual(topic_arg, "crop_guide_completed")
        self.assertEqual(json.loads(payload_arg)["event_id"], "evt-1")
        mock_outbox_instance.create.assert_not_called()

    @patch("services.eventBrokerService.EventOutboxManager")
    @patch("services.eventBrokerService.get_settings")
    @patch("services.eventBrokerService.publish_event", new_callable=AsyncMock)
    async def test_publish_failure_falls_back_to_outbox_not_raise(
        self, mock_publish, mock_get_settings, mock_outbox_manager_cls
    ):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "kafka"
        mock_publish.side_effect = RuntimeError("MSK unreachable")
        mock_outbox_instance = mock_outbox_manager_cls.return_value
        mock_outbox_instance.create = AsyncMock()

        service = EventBrokerService()
        await service.publish("crop_guide_completed", {"event_id": "evt-1"}, key="k1")  # must NOT raise

        mock_outbox_instance.create.assert_awaited_once()
        outbox_row = mock_outbox_instance.create.call_args.args[0]
        self.assertEqual(outbox_row.topic, "crop_guide_completed")
        self.assertEqual(outbox_row.payload["event_id"], "evt-1")
        self.assertEqual(outbox_row.status, "PENDING")

    @patch("services.eventBrokerService.get_settings")
    async def test_sqs_mode_raises_not_implemented(self, mock_get_settings):
        from services.eventBrokerService import EventBrokerService
        mock_get_settings.return_value.broker_type = "sqs"

        service = EventBrokerService()
        with self.assertRaises(NotImplementedError):
            await service.publish("crop_guide_completed", {"event_id": "evt-1"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_event_broker_service -v`
Expected: FAIL/ImportError — `services.eventBrokerService` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/eventBrokerService.py
"""Wraps the MSK producer (services/kafka_client.py) with a broker_type
switch (for local/CI runs with no live MSK — spec §Phase 1) and an
event_outbox fallback (spec §3.3 Scenario 6B) so a Kafka outage never
drops a CropGuideCompleted event."""
import json
import logging

from config import get_settings
from managers import EventOutboxManager, EventOutboxSchema
from services.kafka_client import publish_event

logger = logging.getLogger("event_broker_service")


class EventBrokerService:
    def __init__(self):
        settings = get_settings()
        self.engine = None  # set lazily per-call via get_engine, matching this repo's module-level pattern
        from config import get_engine
        self.outbox_manager = EventOutboxManager(get_engine(settings.name))

    async def publish(self, topic: str, payload: dict, key: str | None = None) -> None:
        settings = get_settings()

        if settings.broker_type == "memory":
            logger.debug(f"[MEMORY BROKER] {topic}: {payload}")
            return

        if settings.broker_type == "sqs":
            raise NotImplementedError("SQS broker_type is not implemented")

        try:
            await publish_event(topic, json.dumps(payload), key=key)
        except Exception as exc:
            logger.error(f"Direct publish to {topic} failed, falling back to outbox: {exc}")
            await self.outbox_manager.create(EventOutboxSchema(
                topic=topic, event_key=key, payload=payload, status="PENDING",
            ))


__all__ = ["EventBrokerService"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_event_broker_service -v`
Expected: `OK` (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/eventBrokerService.py tests/test_event_broker_service.py
git commit -m "feat: add EventBrokerService with broker_type switch and outbox fallback"
```

---

## Task 3: Outbox replay worker — continuous, in-process (runs inside this service, not a separate deployment)

**Files:**
- Create: `app/workers/event_outbox_replayer.py`
- Modify: `app/app.py`
- Test: `tests/test_event_outbox_replayer.py`
- Test: `tests/test_app_replayer_lifespan.py`

**Interfaces:**
- Consumes: `EventOutboxManager`, `EventOutboxSchema` (Task 1). `services.kafka_client.publish_event` (existing).
- Produces: `async def replay_pending_events(outbox_manager, session, limit: int = 50) -> int` — returns the count of events successfully replayed (unchanged in shape from the original design). `async def run_replay_loop(outbox_manager, poll_interval_seconds: int = 60) -> None` — an infinite `await asyncio.sleep(...)` loop (never a blocking `time.sleep`, since Task 2 instructed this to run as an `asyncio.create_task` inside the same event loop as the FastAPI app, not a separate process — a blocking sleep would freeze every request). `app.py`'s `lifespan` context manager starts this loop as a background task on startup and cancels it on shutdown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_outbox_replayer.py
"""Review Focus: replay must not double-publish an event whose status
already moved past PENDING (e.g. a concurrent replayer run, or a row
that actually succeeded before the outbox write happened)."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReplayPendingEvents(IsolatedAsyncioTestCase):
    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_replays_pending_rows_and_marks_completed(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events
        import json

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=0)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        mock_outbox_manager.update = AsyncMock()
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 1)
        mock_publish.assert_awaited_once()
        published_topic, published_payload = mock_publish.call_args.args[0], mock_publish.call_args.args[1]
        self.assertEqual(published_topic, "crop_guide_completed")
        self.assertEqual(json.loads(published_payload)["event_id"], "evt-1")
        update_calls = mock_outbox_manager.update.call_args_list
        # First call flips PENDING -> PROCESSING (claim), second flips to COMPLETED.
        self.assertEqual(update_calls[0].kwargs["updates"]["status"], "PROCESSING")
        self.assertEqual(update_calls[-1].kwargs["updates"]["status"], "COMPLETED")

    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_skips_row_not_pending_after_claim(self, mock_publish):
        # Review Focus: simulates a concurrent claim winning the race — the
        # update() that flips PENDING->PROCESSING reports 0 rows affected,
        # so this replayer must not publish it again.
        from workers.event_outbox_replayer import replay_pending_events

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=0)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        # update() returning a falsy/zero-rows result signals the claim lost the race.
        mock_outbox_manager.update = AsyncMock(return_value=None)
        mock_outbox_manager.claim_pending = AsyncMock(return_value=False)
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 0)
        mock_publish.assert_not_called()

    @patch("workers.event_outbox_replayer.publish_event", new_callable=AsyncMock)
    async def test_publish_failure_increments_retry_count_not_completed(self, mock_publish):
        from workers.event_outbox_replayer import replay_pending_events

        row = MagicMock(uid="outbox-1", topic="crop_guide_completed", event_key="k1",
                         payload={"event_id": "evt-1"}, status="PENDING", retry_count=2)
        mock_outbox_manager = MagicMock()
        mock_outbox_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[row]))
        mock_outbox_manager.update = AsyncMock()
        mock_outbox_manager.claim_pending = AsyncMock(return_value=True)
        mock_publish.side_effect = RuntimeError("still unreachable")
        mock_session = AsyncMock()

        count = await replay_pending_events(mock_outbox_manager, mock_session, limit=50)

        self.assertEqual(count, 0)
        last_update = mock_outbox_manager.update.call_args_list[-1]
        self.assertEqual(last_update.kwargs["updates"]["status"], "PENDING")
        self.assertEqual(last_update.kwargs["updates"]["retry_count"], 3)
```

```python
# tests/test_app_replayer_lifespan.py
"""Confirms the outbox replayer starts as a background task when the app
starts, and is cleanly cancelled on shutdown — no separate process, no
leaked task after the app stops."""
import asyncio
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReplayerLifespan(IsolatedAsyncioTestCase):
    @patch("app.run_replay_loop")
    async def test_lifespan_starts_and_cancels_replay_task(self, mock_run_replay_loop):
        import app as app_module

        # run_replay_loop normally never returns; make the mock behave the
        # same way so cancellation is actually exercised, not a no-op await.
        async def _never_returns(*_args, **_kwargs):
            await asyncio.Event().wait()
        mock_run_replay_loop.side_effect = _never_returns

        async with app_module.lifespan(app_module.app):
            pass  # lifespan's `yield` point — the app is "running" here

        # After the `async with` exits, lifespan's finally-block must have
        # cancelled the task it created — asserted indirectly: the mock
        # was awaited (task actually ran) and no exception propagated out
        # of the `async with`, which would happen if cancellation leaked.
        mock_run_replay_loop.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_event_outbox_replayer tests.test_app_replayer_lifespan -v`
Expected: FAIL/ImportError — `workers.event_outbox_replayer` does not exist; `app.run_replay_loop` does not exist to patch.

- [ ] **Step 3: Write minimal implementation**

```python
# app/workers/event_outbox_replayer.py
"""Replays event_outbox rows the Kafka producer couldn't reach at request
time (spec §3.3 Scenario 6B). Runs as a continuous background task INSIDE
this service's own process — started from app.py's lifespan as an
asyncio.create_task, not a separate script/process (explicit instruction
this session). Uses asyncio.sleep, never a blocking time.sleep, because
it shares the event loop with every FastAPI request."""
import asyncio
import json
import logging

from managers import EventOutboxManager
from services.kafka_client import publish_event

logger = logging.getLogger("event_outbox_replayer")

MAX_RETRIES = 5


async def _claim(outbox_manager: EventOutboxManager, row) -> bool:
    """Atomically flip PENDING -> PROCESSING so two replayer runs (or this
    one racing a fresh failed-publish write) can't both send the same row."""
    if hasattr(outbox_manager, "claim_pending"):
        return await outbox_manager.claim_pending(row.uid)
    await outbox_manager.update(uid=row.uid, updates={"status": "PROCESSING"})
    return True


async def replay_pending_events(outbox_manager: EventOutboxManager, session, limit: int = 50) -> int:
    pending = await outbox_manager.fetch_all(filters={"status": "PENDING"}, limit=limit, session=session)
    replayed = 0

    for row in pending.items:
        claimed = await _claim(outbox_manager, row)
        if not claimed:
            continue

        try:
            await publish_event(row.topic, json.dumps(row.payload), key=row.event_key)
            await outbox_manager.update(uid=row.uid, updates={"status": "COMPLETED"}, session=session)
            replayed += 1
        except Exception as exc:
            new_retry_count = row.retry_count + 1
            new_status = "FAILED" if new_retry_count >= MAX_RETRIES else "PENDING"
            logger.error(f"Replay failed for outbox row {row.uid} (attempt {new_retry_count}): {exc}")
            await outbox_manager.update(
                uid=row.uid,
                updates={"status": new_status, "retry_count": new_retry_count, "last_error": str(exc)},
                session=session,
            )

    return replayed


async def run_replay_loop(outbox_manager: EventOutboxManager, poll_interval_seconds: int = 60) -> None:
    """Runs until cancelled (app.py cancels this task on shutdown). Each
    iteration opens its own session so a single failed commit doesn't wedge
    the loop for the rest of the process's lifetime."""
    while True:
        try:
            async with outbox_manager.session_factory() as session:
                count = await replay_pending_events(outbox_manager, session, limit=50)
                await session.commit()
            if count:
                logger.info(f"Replayed {count} outbox event(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Outbox replay loop iteration failed: {exc}")
        await asyncio.sleep(poll_interval_seconds)


__all__ = ["replay_pending_events", "run_replay_loop"]
```

In `app/app.py`, start and cleanly stop this loop from the existing `lifespan`:
```python
import asyncio

from managers import EventOutboxManager
from workers.event_outbox_replayer import run_replay_loop

# ... existing imports/settings/engine stay as-is ...

outbox_manager = EventOutboxManager(engine)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        if settings.supports_schema:
            await conn.execute(db.text(f'CREATE SCHEMA IF NOT EXISTS "{settings.name}"'))
        await conn.run_sync(BaseSchema.metadata.create_all)

    replay_task = asyncio.create_task(run_replay_loop(outbox_manager))
    try:
        yield
    finally:
        replay_task.cancel()
        try:
            await replay_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_event_outbox_replayer tests.test_app_replayer_lifespan -v`
Expected: `OK` (3 + 1 tests).

- [ ] **Step 5: Commit**

```bash
git add app/workers/event_outbox_replayer.py app/app.py tests/test_event_outbox_replayer.py tests/test_app_replayer_lifespan.py
git commit -m "feat: run event_outbox replayer as a continuous in-process worker via app lifespan"
```

---

## Task 4: Pincode fallback in the scoring engine

**Files:**
- Modify: `app/models/recommendationModels.py`
- Modify: `app/utils/scoringEngine.py`
- Test: `tests/test_scoring_engine_pincode.py`

**Interfaces:**
- Consumes: `managers.PincodeZoneMappingManager` (existing, already used by `CropScoringEngine.__init__`).
- Produces: `RecommendationRequest.pincode: Optional[str]` (new field). `CropScoringEngine.recommend()`'s zone-resolution block now also accepts a direct pincode when `lat`/`lon` are both `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scoring_engine_pincode.py
"""TRD §Phase 4: a farmer without GPS can submit a pincode directly and
still get zone-filtered recommendations."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestScoringEnginePincodeFallback(IsolatedAsyncioTestCase):
    async def _make_engine_with_mocks(self):
        from utils.scoringEngine import CropScoringEngine
        engine = CropScoringEngine.__new__(CropScoringEngine)  # bypass __init__'s real managers
        engine.crop_manager = MagicMock()
        engine.crop_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))
        engine.season_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.water_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.soil_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.budget_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.rotation_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.weight_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[
            MagicMock(factor="SEASON", weight=1), MagicMock(factor="WATER", weight=1),
            MagicMock(factor="ZONE", weight=1), MagicMock(factor="SOIL", weight=1),
            MagicMock(factor="BUDGET", weight=1), MagicMock(factor="ROTATION", weight=1),
        ])))
        engine.zone_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.pincode_manager = MagicMock()
        engine.location_service = MagicMock()
        return engine

    async def test_direct_pincode_resolves_zone_without_coordinates(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.pincode_manager.fetch_all = AsyncMock(
            return_value=MagicMock(items=[MagicMock(zone=9)])
        )

        request = RecommendationRequest(
            lat=None, lon=None, pincode="411001", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        response = await engine.recommend(request)

        self.assertTrue(response.zone_filter_applied)
        engine.pincode_manager.fetch_all.assert_awaited_once_with(limit=1, filters={"pincode": "411001"})
        engine.location_service.get_pincode_from_coordinates.assert_not_called()

    async def test_unmapped_pincode_skips_zone_filter_not_raise(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.pincode_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))

        request = RecommendationRequest(
            lat=None, lon=None, pincode="000000", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        response = await engine.recommend(request)  # must NOT raise

        self.assertFalse(response.zone_filter_applied)

    async def test_coordinates_still_take_priority_over_pincode(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.location_service.get_pincode_from_coordinates = AsyncMock(return_value="411001")
        engine.pincode_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[MagicMock(zone=9)]))

        request = RecommendationRequest(
            lat=18.5, lon=73.8, pincode="999999", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        await engine.recommend(request)

        engine.location_service.get_pincode_from_coordinates.assert_awaited_once_with(18.5, 73.8)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_scoring_engine_pincode -v`
Expected: FAIL — `RecommendationRequest` rejects the unexpected `pincode` kwarg (extra field), or (once the model field is added) the direct-pincode test fails because the engine never consults `request.pincode`.

- [ ] **Step 3: Write minimal implementation**

In `app/models/recommendationModels.py`, add to `RecommendationRequest`:
```python
    pincode: Optional[str] = Field(None, description="6-digit Indian postal code fallback when lat/lon are omitted")
```

In `app/utils/scoringEngine.py`, replace the existing zone-resolution block:
```python
        if request.lat is not None and request.lon is not None:
            pincode = await self.location_service.get_pincode_from_coordinates(request.lat, request.lon)
            if pincode:
                mapping = await self.pincode_manager.fetch_all(limit=1, filters={"pincode": pincode})
                if mapping.items:
                    zone = mapping.items[0].zone
                    zone_resolved = True
                    notes.append(f"Agro-climatic zone resolved to Zone {zone} (from pincode {pincode}).")
                else:
                    notes.append(f"Zone mapping not found for pincode {pincode}. Skipping zone filtering.")
            else:
                notes.append("Pincode could not be resolved from coordinates. Skipping zone filtering.")
        elif request.pincode:
            mapping = await self.pincode_manager.fetch_all(limit=1, filters={"pincode": request.pincode})
            if mapping.items:
                zone = mapping.items[0].zone
                zone_resolved = True
                notes.append(f"Agro-climatic zone resolved to Zone {zone} (from pincode {request.pincode}).")
            else:
                notes.append(f"Zone mapping not found for pincode {request.pincode}. Skipping zone filtering.")
        else:
            notes.append(ZONE_SKIPPED_NOTE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_scoring_engine_pincode -v`
Expected: `OK` (3 tests). Then run the whole suite once to confirm nothing in `test_recommend*`/`test_scoring*` regressed: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*scoring*" -v` (or run each `tests/test_*.py` file individually per this repo's established loop, since `discover` needs `tests/__init__.py` which doesn't exist here).

- [ ] **Step 5: Commit**

```bash
git add app/models/recommendationModels.py app/utils/scoringEngine.py tests/test_scoring_engine_pincode.py
git commit -m "feat: add direct pincode fallback for zone resolution when no GPS coordinates"
```

---

## Task 5: `wizard_session_id` passthrough + wizard pincode input

**Files:**
- Modify: `app/models/wizardModels.py`
- Modify: `app/routers/v2/wizard.py`
- Modify: `tests/test_wizard_submit.py`

**Interfaces:**
- Consumes: `RecommendationRequest.pincode` (Task 4). `EventBrokerService` (Task 2, wired in now to replace the direct `publish_event` call — this is also where `_publish_crop_guide_completed` moves off raw `kafka_client.publish_event`).
- Produces: `WizardAnswersPayload.pincode: Optional[str]`. `WizardSubmitRequest.wizard_session_id: Optional[str]`. The `CropGuideCompleted` event's `data` dict gains a `wizard_session_id` key (passthrough only — no idempotency logic in this repo; the consuming Agromonitoring worker owns deduplication).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_wizard_submit.py`:
```python
class TestWizardSubmitPincodeAndSessionId(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_pincode_used_when_location_and_farm_both_absent(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(pincode="411001", season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise 400

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertIsNone(rec_request.lat)
        self.assertIsNone(rec_request.lon)
        self.assertEqual(rec_request.pincode, "411001")

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_wizard_session_id_forwarded_to_broker_event(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1", wizard_session_id="wiz-sess-abc",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertEqual(published_payload["data"]["wizard_session_id"], "wiz-sess-abc")

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_wizard_session_id_omitted_forwards_null_not_generated(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        # Review Focus: must forward None, never silently generate a fresh
        # UUID — a generated-per-request value would defeat idempotency.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertIsNone(published_payload["data"]["wizard_session_id"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_wizard_submit.TestWizardSubmitPincodeAndSessionId -v`
Expected: FAIL — `WizardAnswersPayload` rejects `pincode` (extra field forbidden by default? check: current model has no `model_config`, Pydantic v2 default is to error on genuinely unexpected kwargs only if `extra="forbid"`; this model has no such config so it actually ignores extras by default — so the real failure is `WizardSubmitRequest` rejects `wizard_session_id`, and `rec_request.pincode` is `AttributeError` since nothing sets it) and `routers.v2.wizard` has no `event_broker` attribute.

- [ ] **Step 3: Write minimal implementation**

In `app/models/wizardModels.py`, add to `WizardAnswersPayload`:
```python
    pincode: Optional[str] = Field(None, description="6-digit Indian PIN code fallback when no GPS location")
```
And to `WizardSubmitRequest`:
```python
    wizard_session_id: Optional[str] = Field(None, description="Client-generated idempotency key, forwarded as-is")
```

In `app/routers/v2/wizard.py`:
1. Replace the `from services.kafka_client import publish_event` import with `from services.eventBrokerService import EventBrokerService`, and replace `from services.subscriptionService import SubscriptionService` block's sibling instantiation by adding `event_broker = EventBrokerService()` at module level (alongside `subscription_service = SubscriptionService()`).
2. In `submit_wizard`, change the location-required check:
```python
    location = answers.location
    if location is None and farm:
        location = farm.get("location") or farm.get("center")
    if location is None and not answers.pincode:
        raise HTTPException(status_code=400, detail="answers.location is required when farmId is not provided or the farm has no boundary yet")
```
3. Where `rec_request = RecommendationRequest(...)` is built, change the lat/lon construction to allow a location-less, pincode-only request:
```python
    rec_request = RecommendationRequest(
        lat=location[1] if location else None,
        lon=location[0] if location else None,
        pincode=answers.pincode if location is None else None,
        season=season, water_methods=irrigation, budget_bucket=budget_bucket,
        previous_crop_family=previous_crop_family, was_fallow=was_fallow, top_n=5,
    )
```
4. Replace the body of `_publish_crop_guide_completed` — swap `await publish_event("crop_guide_completed", json.dumps(event), key=payload.farmId or event["event_id"])` for `await event_broker.publish("crop_guide_completed", event, key=payload.farmId or event["event_id"])` (note: `EventBrokerService.publish` takes a `dict`, not a pre-serialized JSON string — it serializes internally only in `kafka` mode), and add `"wizard_session_id": payload.wizard_session_id,` to the event's `data` dict.
5. Remove the now-unused `import json` at the top of the file only if nothing else in the file still uses `json` — check first (`grep -n "json\." app/routers/v2/wizard.py`); if the `try/except` around the old direct publish call becomes dead code (since `EventBrokerService.publish` already never raises), simplify `_publish_crop_guide_completed`'s tail to a plain `await event_broker.publish(...)` with no surrounding `try/except`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_wizard_submit -v`
Expected: `OK` — all prior tests in this file plus the 3 new ones. Existing tests that patch `routers.v2.wizard.publish_event` will now fail (that name no longer exists in the module) — update those `@patch("routers.v2.wizard.publish_event", ...)` decorators to `@patch("routers.v2.wizard.event_broker")` with `mock_event_broker.publish = AsyncMock()`, matching the new tests' style; and update `test_published_payload_is_valid_json_not_python_repr` to assert against the dict `event_broker.publish`'s second positional arg directly (no `json.loads` needed — `EventBrokerService.publish` takes a dict now, so the "is it valid JSON" guarantee has moved inside `EventBrokerService` itself and is already covered by Task 2's tests).

- [ ] **Step 5: Commit**

```bash
git add app/models/wizardModels.py app/routers/v2/wizard.py tests/test_wizard_submit.py
git commit -m "feat: wizard pincode input + wizard_session_id passthrough; route publish through EventBrokerService"
```

---

## Task 6: `CropReportManager` seed script (104-crop master catalog)

**Files:**
- Create: `app/scripts/seed_crop_reports.py`
- Test: `tests/test_seed_crop_reports.py`

**Interfaces:**
- Consumes: `CropReportManager`, `CropReportSchema` (Task 1). The spec's `crops.json` (104 records) — copy it into this repo at `app/data/crops.json` (binary/data asset, not generated — copy verbatim from `E:\AGRO\kissan-agromonitoring\docs\MSK\requirementdocforreviewcropguideintegration\crops.json`, already confirmed to match `CropReportSchema`'s columns field-for-field).
- Produces: `async def seed_crop_reports(manager: CropReportManager, session, crops_file_path: str) -> int` — upserts by `crop_id`, returns the count of rows created-or-updated. Idempotent: running it twice leaves exactly 104 rows, never raises a unique-constraint error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_crop_reports.py
"""Review Focus: seeding must be idempotent — upsert by crop_id, not
insert-and-crash on the second run."""
import json
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestSeedCropReports(IsolatedAsyncioTestCase):
    def _write_sample_file(self, tmp_path):
        sample = [
            {
                "crop_id": "amla", "name_en": "Amla", "name_local": {"hi": "आंवला"},
                "family": "Fruit", "crop_type": "perennial", "seasons": ["kharif"],
                "years_to_first_harvest": 4, "suitable_soil_types": ["alluvial"],
                "soil_texture": ["loam"], "soil_drainage": "well_drained",
                "soil_ph_min": 6.5, "soil_ph_max": 9.0, "water_requirement": "low",
                "cost_per_acre_inr": 25000, "expected_yield_per_acre": 60,
                "yield_unit": "quintal", "profit_potential": "moderate",
                "sowing_windows": [{"season": "kharif", "start": "07-01", "end": "08-15"}],
                "profit_note": "note", "weather": [], "risks": [], "tips": [],
                "soil_economic_benefits": {"title": "t", "soil": [], "economic": []},
                "hero_image_url": None, "icon_url": None, "is_active": True,
                "data_source": "src", "review_status": "ai_draft",
            }
        ]
        file_path = tmp_path / "crops.json"
        file_path.write_text(json.dumps(sample), encoding="utf-8")
        return str(file_path)

    async def test_first_run_creates_rows(self):
        from scripts.seed_crop_reports import seed_crop_reports
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            file_path = self._write_sample_file(Path(tmp))
            manager = MagicMock()
            manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))  # no existing row
            manager.create = AsyncMock()
            session = AsyncMock()

            count = await seed_crop_reports(manager, session, file_path)

            self.assertEqual(count, 1)
            manager.create.assert_awaited_once()
            created_row = manager.create.call_args.args[0]
            self.assertEqual(created_row.crop_id, "amla")

    async def test_second_run_updates_not_duplicates(self):
        from scripts.seed_crop_reports import seed_crop_reports
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            file_path = self._write_sample_file(Path(tmp))
            existing_row = MagicMock(uid="existing-uid-1", crop_id="amla")
            manager = MagicMock()
            manager.fetch_all = AsyncMock(return_value=MagicMock(items=[existing_row]))
            manager.update = AsyncMock()
            manager.create = AsyncMock()
            session = AsyncMock()

            count = await seed_crop_reports(manager, session, file_path)

            self.assertEqual(count, 1)
            manager.create.assert_not_called()
            manager.update.assert_awaited_once()
            self.assertEqual(manager.update.call_args.kwargs["uid"], "existing-uid-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_seed_crop_reports -v`
Expected: FAIL/ImportError — `scripts.seed_crop_reports` does not exist.

- [ ] **Step 3: Write minimal implementation**

Copy the spec's `crops.json` into this repo:
```bash
cp "E:/AGRO/kissan-agromonitoring/docs/MSK/requirementdocforreviewcropguideintegration/crops.json" app/data/crops.json
```

```python
# app/scripts/seed_crop_reports.py
"""Idempotent seed loader for the crop_reports master catalog (spec's
104-crop crops.json). Upserts by crop_id so re-running on every deploy
never duplicates or crashes on the unique constraint."""
import json
import logging

from managers.cropReportManagers import CropReportManager, CropReportSchema

logger = logging.getLogger("seed_crop_reports")

_FIELDS = (
    "crop_id", "name_en", "name_local", "family", "crop_type", "seasons",
    "duration_days", "years_to_first_harvest", "suitable_soil_types", "soil_texture",
    "soil_drainage", "soil_ph_min", "soil_ph_max", "water_requirement",
    "cost_per_acre_inr", "expected_yield_per_acre", "yield_unit", "profit_potential",
    "sowing_windows", "profit_note", "weather", "risks", "tips",
    "soil_economic_benefits", "hero_image_url", "icon_url", "is_active",
    "data_source", "review_status",
)


async def seed_crop_reports(manager: CropReportManager, session, crops_file_path: str) -> int:
    with open(crops_file_path, encoding="utf-8") as f:
        crops = json.load(f)

    count = 0
    for crop in crops:
        fields = {k: crop.get(k) for k in _FIELDS}
        existing = await manager.fetch_all(limit=1, filters={"crop_id": fields["crop_id"]}, session=session)
        if existing.items:
            await manager.update(uid=existing.items[0].uid, updates=fields, session=session)
        else:
            await manager.create(CropReportSchema(**fields), session=session)
        count += 1

    return count


if __name__ == "__main__":
    import asyncio
    from pathlib import Path
    from config import get_engine, get_settings

    async def _main():
        settings = get_settings()
        manager = CropReportManager(get_engine(settings.name))
        crops_path = Path(__file__).resolve().parents[1] / "data" / "crops.json"
        async with manager.session_factory() as session:
            n = await seed_crop_reports(manager, session, str(crops_path))
            await session.commit()
            logger.info(f"Seeded {n} crop_reports rows")

    asyncio.run(_main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_seed_crop_reports -v`
Expected: `OK` (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/scripts/seed_crop_reports.py app/data/crops.json tests/test_seed_crop_reports.py
git commit -m "feat: idempotent crop_reports seed script from spec's 104-crop catalog"
```

---

## Task 7: Report pre-hydration on wizard submit

**Files:**
- Modify: `app/routers/v2/wizard.py`
- Modify: `tests/test_wizard_submit.py`

**Interfaces:**
- Consumes: `UserCropReportManager`, `UserCropReportSchema` (Task 1). `response.results` (`list[CropRecommendation]`, existing, from `scoring_engine.recommend()`). The 5 new `recommendation_history` audit columns from Task 1's migration (`farm_id`, `farm_name`, `livestock`, `current_crop_code`, `sowing_date`) exist in the DB after Task 1 but the pre-existing `history_manager.create(...)` call (from commit `f4711f3`, before this plan) never sets them — this task wires them in so they don't silently stay `NULL` forever.
- Produces: after a successful scoring run, `submit_wizard` writes one `user_crop_reports` row holding the full ranked-crop list for that run (best-effort — a write failure must not break the response, matching the existing `history_manager.create` error-handling pattern in the same function). The row's `recommendation_id` is the same `uuid4()` already minted for the `CropGuideCompleted` event's `event_id` — reused, not a second random id, so `GET /api/v2/reports/recommendations/{recommendation_id}/report` (Task 9) can look it up by the id the client already has from the submit response... except `WizardSubmitResponse` doesn't currently return one. Add `recommendation_id: str` to `WizardSubmitResponse` now, so Task 9's endpoints have something to key on.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_wizard_submit.py`:
```python
class TestWizardSubmitReportPreHydration(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_successful_submission_writes_user_crop_report_and_returns_recommendation_id(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            results=[
                CropRecommendation(crop="Mustard", family="Brassica", horizon="SEASONAL",
                                    total=95, max_total=100, suitability_pct=95.0, factors=[]),
                CropRecommendation(crop="Wheat", family="Cereal", horizon="SEASONAL",
                                    total=84, max_total=100, suitability_pct=84.0, factors=[]),
            ],
        ))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"], farm_name="East Plot",
            ),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})

        self.assertTrue(result.recommendation_id)
        mock_ucr_manager.create.assert_awaited_once()
        ucr_row = mock_ucr_manager.create.call_args.args[0]
        self.assertEqual(ucr_row.user_id, "usr_1")
        self.assertEqual(ucr_row.recommendation_id, result.recommendation_id)
        self.assertEqual(ucr_row.top_crop_name, "Mustard")
        self.assertEqual(ucr_row.top_crop_score, 95)
        self.assertEqual(ucr_row.total_recommendations, 2)
        self.assertEqual(len(ucr_row.report_data["results"]), 2)

    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_report_prehydration_failure_does_not_break_response(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual(result.recommendations, [])


class TestWizardSubmitHistoryAuditColumns(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_history_row_includes_new_audit_columns(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager
    ):
        # Task 1's migration adds farm_id/farm_name/livestock/current_crop_code/
        # sowing_date to recommendation_history — this pins that they're actually
        # populated, not just added to the schema and left NULL forever.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-9", subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"],
                farm_name="East Plot", livestock="cattle", sowing_date="2026-10-15",
            ),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        history_row = mock_history_manager.create.call_args.args[0]
        self.assertEqual(history_row.farm_id, "farm-9")
        self.assertEqual(history_row.farm_name, "East Plot")
        self.assertEqual(history_row.livestock, "cattle")
        self.assertEqual(history_row.sowing_date, "2026-10-15")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_wizard_submit.TestWizardSubmitReportPreHydration tests.test_wizard_submit.TestWizardSubmitHistoryAuditColumns -v`
Expected: FAIL — `routers.v2.wizard` has no `user_crop_report_manager`; `WizardSubmitResponse` has no `recommendation_id`.

- [ ] **Step 3: Write minimal implementation**

In `app/models/wizardModels.py`, add to `WizardSubmitResponse`:
```python
    recommendation_id: str
```

In `app/routers/v2/wizard.py`:
1. Add import: `from managers.cropReportManagers import UserCropReportManager, UserCropReportSchema`.
2. Add module-level instance alongside `history_manager`: `user_crop_report_manager = UserCropReportManager(engine)`.
3. In `_publish_crop_guide_completed`, the `event_id` is already minted via `str(uuid.uuid4())` — hoist that id generation one level up: in `submit_wizard`, generate `recommendation_id = str(uuid.uuid4())` right before the quota-verify step, pass it into `_publish_crop_guide_completed(upid, payload, farm, season, budget_bucket, recommendation_id)` (new last parameter), and have that function use the passed-in id as `event["event_id"]` instead of minting its own.
4. After the existing `history_manager.create(...)` try/except block in `submit_wizard`, add:
```python
    try:
        if response.results:
            top = response.results[0]
            await user_crop_report_manager.create(UserCropReportSchema(
                user_id=upid,
                farm_id=payload.farmId,
                recommendation_id=recommendation_id,
                farm_name=answers.farm_name or (farm.get("name") if farm else None),
                season=season,
                language="en",
                top_crop_id=top.crop.lower().replace(" ", "_"),
                top_crop_name=top.crop,
                top_crop_score=round(top.suitability_pct),
                total_recommendations=len(response.results),
                report_data={"results": [r.model_dump() for r in response.results]},
            ))
    except Exception as exc:
        logger.error(f"failed to pre-hydrate user_crop_reports for upid={upid}: {exc}")
```
5. In the existing `history_manager.create(RecommendationHistorySchema(...))` call (written in `f4711f3`, upstream of the block above), add the 5 new audit fields so Task 1's migration columns actually get populated instead of staying `NULL` forever:
```python
            farm_id=payload.farmId,
            farm_name=answers.farm_name or (farm.get("name") if farm else None),
            livestock=answers.livestock,
            current_crop_code=rotation_crop_name,
            sowing_date=answers.sowing_date,
```
6. Return `WizardSubmitResponse(recommendations=recommendations, recommendation_id=recommendation_id)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_wizard_submit -v`
Expected: `OK` — full file, including every test from Tasks 5 and 7 plus the pre-existing ones (update any test that constructs `WizardSubmitResponse` directly, or asserts on `_publish_crop_guide_completed`'s old no-recommendation_id-arg signature, to match).

- [ ] **Step 5: Commit**

```bash
git add app/models/wizardModels.py app/routers/v2/wizard.py tests/test_wizard_submit.py
git commit -m "feat: pre-hydrate user_crop_reports on wizard submit, return recommendation_id"
```

---

## Task 8: `reportModels.py` + `CropReportService`

**Files:**
- Create: `app/models/reportModels.py`
- Create: `app/services/cropReportService.py`
- Test: `tests/test_crop_report_service.py`

**Interfaces:**
- Consumes: `CropReportManager` (Task 1, master catalog lookups by `name_en`). `UserCropReportManager` (Task 1/7, per-run cache lookups by `recommendation_id`/`user_id`).
- Produces: the Pydantic models below, copied verbatim from spec §5.4 (`WeatherMetric`, `CropRisk`, `CropTip`, `SoilEconomicBenefits`, `SowingWindow`, `MoreForYourFarmItem`, `SingleCropDossier`, `UnifiedCropReportResponse`, `RecommendedCropSummaryItem`, `UserReportSummaryCard`, `UserReportsListResponse`). `CropReportService` with three async methods: `list_user_reports(user_id, farm_id=None, recommendation_id=None, lang="en") -> UserReportsListResponse`, `get_or_create_unified_report(recommendation_id, user_id, farm_id=None, lang="en", focus_crop_id=None) -> UnifiedCropReportResponse`, `get_master_report(crop_id, lang="en") -> SingleCropDossier`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crop_report_service.py
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def _crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica"):
    return MagicMock(
        crop_id=crop_id, name_en=name_en, name_local={"hi": "सरसों"}, family=family,
        crop_type="annual", seasons=["rabi"], duration_days=115, years_to_first_harvest=None,
        cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
        profit_potential="high", sowing_windows=[{"season": "rabi", "start": "10-01", "end": "10-20"}],
        profit_note="note", weather=[], risks=[], tips=[],
        soil_economic_benefits={"title": "t", "soil": [], "economic": []},
        hero_image_url=None, icon_url=None,
    )


class TestGetMasterReport(IsolatedAsyncioTestCase):
    async def test_returns_dossier_for_known_crop(self):
        from services.cropReportService import CropReportService

        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(
            return_value=MagicMock(items=[_crop_report_row()])
        )
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=MagicMock(),
        )

        dossier = await service.get_master_report("mustard", lang="en")

        self.assertEqual(dossier.crop_id, "mustard")
        self.assertEqual(dossier.name_en, "Mustard")
        self.assertEqual(dossier.family, "Brassica")


class TestGetOrCreateUnifiedReport(IsolatedAsyncioTestCase):
    async def test_builds_dossier_per_ranked_crop_from_cached_run(self):
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-1", recommendation_id="rec-1", user_id="usr_1", farm_id=None,
                farm_name="East Plot", season="RABI", top_crop_name="Mustard", top_crop_score=95,
                report_data={"results": [
                    {"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0},
                    {"crop": "Wheat", "family": "Cereal", "suitability_pct": 84.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(side_effect=[
            MagicMock(items=[_crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica")]),
            MagicMock(items=[_crop_report_row(crop_id="wheat", name_en="Wheat", family="Cereal")]),
        ])
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-1", user_id="usr_1")

        self.assertEqual(report.recommendation_id, "rec-1")
        self.assertEqual(report.total_recommended_crops, 2)
        self.assertEqual(len(report.crops), 2)
        self.assertTrue(report.crops[0].is_top_pick)
        self.assertFalse(report.crops[1].is_top_pick)

    async def test_crop_missing_from_master_catalog_is_skipped_not_500(self):
        # Review Focus: a scored crop with no crop_reports row must be
        # omitted from the dossier list, not crash the whole report.
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-1", recommendation_id="rec-2", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="ObscureCrop", top_crop_score=70,
                report_data={"results": [{"crop": "ObscureCrop", "family": "Unknown", "suitability_pct": 70.0}]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))  # no master row
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-2", user_id="usr_1")

        self.assertEqual(report.crops, [])
        self.assertEqual(report.total_recommended_crops, 1)  # count reflects the scoring run, not the dossiers found
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_crop_report_service -v`
Expected: FAIL/ImportError — `services.cropReportService` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# app/models/reportModels.py
"""Pydantic models for the crop-guide report/dossier system — copied
verbatim from spec §5.4, under /api/v2 rather than the spec's literal
/api/v6 mount point."""
from typing import List, Optional, Dict, Literal
from pydantic import BaseModel


class WeatherMetric(BaseModel):
    param: Literal["temperature", "rainfall", "humidity"]
    title: str
    icon: str
    unit: str
    ideal_min: float
    ideal_max: float
    tolerable_min: float
    tolerable_max: float
    description: str


class CropRisk(BaseModel):
    name: str
    local_name: str
    type: Literal["fungal_disease", "insect_pest", "nutrient_deficiency", "viral_disease", "bacterial_disease", "weed", "weather"]
    severity: Literal["high", "medium", "low"]
    icon: str
    solution: str


class CropTip(BaseModel):
    title: str
    description: str
    priority: Literal["high", "medium", "low"]
    category: Literal["sowing", "protection", "harvest", "irrigation", "fertilizer", "soil"]
    icon: str


class SoilEconomicBenefits(BaseModel):
    title: str = "Soil & Economic Benefits"
    soil: List[str]
    economic: List[str]


class SowingWindow(BaseModel):
    season: str
    start: str
    end: str


class MoreForYourFarmItem(BaseModel):
    crop_id: str
    name_en: str
    name_local: Dict[str, str]
    match_score: int
    is_top_pick: bool = False
    profit_note: str


class SingleCropDossier(BaseModel):
    crop_id: str
    name_en: str
    name_local: Dict[str, str]
    match_score: int
    is_top_pick: bool
    match_reasons: List[str]
    family: str
    crop_type: Literal["annual", "perennial"]
    seasons: List[str]
    duration_days: Optional[int] = None
    years_to_first_harvest: Optional[float] = None
    cost_per_acre_inr: int
    expected_yield_per_acre: float
    yield_unit: str
    profit_potential: Literal["high", "moderate", "low"]
    profit_note: str
    sowing_windows: List[SowingWindow]
    weather: List[WeatherMetric]
    risks: List[CropRisk]
    tips: List[CropTip]
    soil_economic_benefits: SoilEconomicBenefits
    hero_image_url: Optional[str] = None
    icon_url: Optional[str] = None


class UnifiedCropReportResponse(BaseModel):
    status: str = "success"
    report_id: str
    recommendation_id: str
    user_id: str
    farm_id: Optional[str] = None
    farm_name: Optional[str] = None
    season: str
    sowing_date: Optional[str] = None
    language: str = "en"
    top_crop_id: str
    top_crop_name: str
    top_crop_score: int
    total_recommended_crops: int
    crops: List[SingleCropDossier]
    more_for_your_farm: List[MoreForYourFarmItem]
    created_at: str


class RecommendedCropSummaryItem(BaseModel):
    crop_id: str
    crop_name: str
    crop_name_vernacular: str
    match_score: int
    is_top_pick: bool


class UserReportSummaryCard(BaseModel):
    report_id: str
    recommendation_id: str
    farm_id: Optional[str] = None
    farm_name: Optional[str] = None
    season: str
    top_crop_id: str
    top_crop_name: str
    top_crop_vernacular: str
    top_crop_score: int
    total_recommended_crops: int
    recommended_crops_summary: List[RecommendedCropSummaryItem]
    thumbnail_url: Optional[str] = None
    created_at: str


class UserReportsListResponse(BaseModel):
    status: str = "success"
    user_id: str
    total: int
    reports: List[UserReportSummaryCard]


__all__ = [
    "WeatherMetric", "CropRisk", "CropTip", "SoilEconomicBenefits", "SowingWindow",
    "MoreForYourFarmItem", "SingleCropDossier", "UnifiedCropReportResponse",
    "RecommendedCropSummaryItem", "UserReportSummaryCard", "UserReportsListResponse",
]
```

```python
# app/services/cropReportService.py
"""Assembles the three report/dossier read models from crop_reports (master
catalog) and user_crop_reports (per-run cache). No endpoint-layer logic
here — routers/v2/reports.py (Task 9) just calls these three methods."""
import logging
from datetime import datetime, timezone

from managers.cropReportManagers import CropReportManager, UserCropReportManager
from models.reportModels import (
    CropRisk, CropTip, SingleCropDossier, SoilEconomicBenefits, SowingWindow,
    UnifiedCropReportResponse, UserReportSummaryCard, UserReportsListResponse,
    RecommendedCropSummaryItem, WeatherMetric,
)

logger = logging.getLogger("crop_report_service")


class CropReportService:
    def __init__(self, crop_report_manager: CropReportManager, user_crop_report_manager: UserCropReportManager):
        self.crop_report_manager = crop_report_manager
        self.user_crop_report_manager = user_crop_report_manager

    async def _find_master_row(self, name_en: str):
        result = await self.crop_report_manager.fetch_all(limit=1, filters={"name_en": name_en})
        if result.items:
            return result.items[0]
        all_rows = await self.crop_report_manager.fetch_all(limit=0)
        return next((r for r in all_rows.items if r.name_en.lower() == name_en.lower()), None)

    @staticmethod
    def _to_dossier(master_row, match_score: int, is_top_pick: bool, match_reasons: list[str]) -> SingleCropDossier:
        return SingleCropDossier(
            crop_id=master_row.crop_id, name_en=master_row.name_en, name_local=master_row.name_local,
            match_score=match_score, is_top_pick=is_top_pick, match_reasons=match_reasons,
            family=master_row.family, crop_type=master_row.crop_type, seasons=master_row.seasons,
            duration_days=master_row.duration_days,
            years_to_first_harvest=float(master_row.years_to_first_harvest) if master_row.years_to_first_harvest else None,
            cost_per_acre_inr=master_row.cost_per_acre_inr,
            expected_yield_per_acre=float(master_row.expected_yield_per_acre),
            yield_unit=master_row.yield_unit, profit_potential=master_row.profit_potential,
            profit_note=master_row.profit_note,
            sowing_windows=[SowingWindow(**w) for w in master_row.sowing_windows],
            weather=[WeatherMetric(**w) for w in master_row.weather],
            risks=[CropRisk(**r) for r in master_row.risks],
            tips=[CropTip(**t) for t in master_row.tips],
            soil_economic_benefits=SoilEconomicBenefits(**master_row.soil_economic_benefits),
            hero_image_url=master_row.hero_image_url, icon_url=master_row.icon_url,
        )

    async def get_master_report(self, crop_id: str, lang: str = "en") -> SingleCropDossier:
        result = await self.crop_report_manager.fetch_all(limit=1, filters={"crop_id": crop_id})
        if not result.items:
            raise ValueError(f"No crop_reports row for crop_id '{crop_id}'")
        return self._to_dossier(result.items[0], match_score=0, is_top_pick=False, match_reasons=[])

    async def get_or_create_unified_report(
        self, recommendation_id: str, user_id: str, farm_id: str | None = None,
        lang: str = "en", focus_crop_id: str | None = None,
    ) -> UnifiedCropReportResponse:
        runs = await self.user_crop_report_manager.fetch_all(
            limit=1, filters={"recommendation_id": recommendation_id, "user_id": user_id},
        )
        if not runs.items:
            raise ValueError(f"No recommendation run '{recommendation_id}' for user '{user_id}'")
        run = runs.items[0]

        ranked = run.report_data.get("results", [])
        dossiers: list[SingleCropDossier] = []
        for i, crop_result in enumerate(ranked):
            master_row = await self._find_master_row(crop_result["crop"])
            if master_row is None:
                logger.warning(f"No crop_reports row for scored crop '{crop_result['crop']}' — skipping dossier")
                continue
            dossiers.append(self._to_dossier(
                master_row, match_score=round(crop_result["suitability_pct"]), is_top_pick=(i == 0),
                match_reasons=[f"Suitability {crop_result['suitability_pct']:.0f}% for {run.season.title()} season."],
            ))

        return UnifiedCropReportResponse(
            report_id=run.uid, recommendation_id=run.recommendation_id, user_id=run.user_id,
            farm_id=run.farm_id, farm_name=run.farm_name, season=run.season, language=lang,
            top_crop_id=dossiers[0].crop_id if dossiers else "",
            top_crop_name=run.top_crop_name, top_crop_score=run.top_crop_score,
            total_recommended_crops=len(ranked), crops=dossiers, more_for_your_farm=[],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def list_user_reports(
        self, user_id: str, farm_id: str | None = None,
        recommendation_id: str | None = None, lang: str = "en",
    ) -> UserReportsListResponse:
        filters = {"user_id": user_id}
        if farm_id:
            filters["farm_id"] = farm_id
        if recommendation_id:
            filters["recommendation_id"] = recommendation_id
        runs = await self.user_crop_report_manager.fetch_all(limit=0, filters=filters, sorts=["-created_at"])

        cards = [
            UserReportSummaryCard(
                report_id=run.uid, recommendation_id=run.recommendation_id, farm_id=run.farm_id,
                farm_name=run.farm_name, season=run.season, top_crop_id=run.top_crop_name.lower().replace(" ", "_"),
                top_crop_name=run.top_crop_name, top_crop_vernacular=run.top_crop_name,
                top_crop_score=run.top_crop_score, total_recommended_crops=run.total_recommendations,
                recommended_crops_summary=[
                    RecommendedCropSummaryItem(
                        crop_id=r["crop"].lower().replace(" ", "_"), crop_name=r["crop"],
                        crop_name_vernacular=r["crop"], match_score=round(r["suitability_pct"]),
                        is_top_pick=(i == 0),
                    )
                    for i, r in enumerate(run.report_data.get("results", []))
                ],
                created_at=run.created_at.isoformat() if run.created_at else "",
            )
            for run in runs.items
        ]
        return UserReportsListResponse(user_id=user_id, total=len(cards), reports=cards)


__all__ = ["CropReportService"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_crop_report_service -v`
Expected: `OK` (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/models/reportModels.py app/services/cropReportService.py tests/test_crop_report_service.py
git commit -m "feat: add reportModels and CropReportService (dossier assembly)"
```

---

## Task 9: Reports router, mounted at `/api/v2/reports`

**Files:**
- Create: `app/routers/v2/reports.py`
- Modify: `app/routers/v2/__init__.py`
- Test: `tests/test_app_v2_reports_mount.py`
- Test: `tests/test_reports_router.py`

**Interfaces:**
- Consumes: `CropReportService` (Task 8). `dependencies.auth.require_jwt` (existing).
- Produces: `GET /api/v2/reports/users/{user_id}/reports`, `GET /api/v2/reports/recommendations/{recommendation_id}/report`, `GET /api/v2/reports/crops/{crop_id}/report` — all under `/api/v2`, per this plan's Global Constraints (spec literally says `/api/v6`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_v2_reports_mount.py
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestV2ReportsMount(TestCase):
    def test_reports_routes_mounted_under_api_v2(self):
        import app as app_module
        paths = [r.path for r in app_module.app.routes]
        self.assertIn("/api/v2/reports/users/{user_id}/reports", paths)
        self.assertIn("/api/v2/reports/recommendations/{recommendation_id}/report", paths)
        self.assertIn("/api/v2/reports/crops/{crop_id}/report", paths)
```

```python
# tests/test_reports_router.py
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReportsRouter(IsolatedAsyncioTestCase):
    @patch("routers.v2.reports.report_service")
    async def test_list_user_reports_delegates_to_service(self, mock_service):
        from routers.v2.reports import list_user_reports
        from models.reportModels import UserReportsListResponse
        mock_service.list_user_reports = AsyncMock(
            return_value=UserReportsListResponse(user_id="usr_1", total=0, reports=[])
        )

        result = await list_user_reports(
            user_id="usr_1", farm_id=None, recommendation_id=None, lang="en",
            auth_payload={"upid": "usr_1"},
        )

        self.assertEqual(result.user_id, "usr_1")
        mock_service.list_user_reports.assert_awaited_once_with(
            user_id="usr_1", farm_id=None, recommendation_id=None, lang="en",
        )

    @patch("routers.v2.reports.report_service")
    async def test_get_master_crop_report_lowercases_crop_id(self, mock_service):
        from routers.v2.reports import get_master_crop_report
        from models.reportModels import SingleCropDossier, SoilEconomicBenefits
        mock_service.get_master_report = AsyncMock(return_value=SingleCropDossier(
            crop_id="mustard", name_en="Mustard", name_local={}, match_score=0, is_top_pick=False,
            match_reasons=[], family="Brassica", crop_type="annual", seasons=["rabi"],
            cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
            profit_potential="high", profit_note="n", sowing_windows=[], weather=[], risks=[], tips=[],
            soil_economic_benefits=SoilEconomicBenefits(soil=[], economic=[]),
        ))

        await get_master_crop_report(crop_id="MUSTARD", lang="en", auth_payload={"upid": "usr_1"})

        mock_service.get_master_report.assert_awaited_once_with(crop_id="mustard", lang="en")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_app_v2_reports_mount tests.test_reports_router -v`
Expected: FAIL/ImportError — `routers.v2.reports` does not exist, routes not mounted.

- [ ] **Step 3: Write minimal implementation**

```python
# app/routers/v2/reports.py
"""Crop guide report/dossier endpoints — spec §5 Phase 5, mounted at
/api/v2/reports rather than the spec's literal /api/v6 (this plan's
versioning decision)."""
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropReportManagers import CropReportManager, UserCropReportManager
from models.reportModels import SingleCropDossier, UnifiedCropReportResponse, UserReportsListResponse
from services.cropReportService import CropReportService

router = APIRouter(prefix="/reports", tags=["Crop Guide Reports v2"])
settings = get_settings()
engine = get_engine(settings.name)
report_service = CropReportService(
    crop_report_manager=CropReportManager(engine),
    user_crop_report_manager=UserCropReportManager(engine),
)


@router.get("/users/{user_id}/reports", response_model=UserReportsListResponse)
async def list_user_reports(
    user_id: str = Path(...),
    farm_id: Optional[str] = Query(None),
    recommendation_id: Optional[str] = Query(None),
    lang: str = Query("en"),
    auth_payload: dict = Depends(require_jwt),
):
    return await report_service.list_user_reports(
        user_id=user_id, farm_id=farm_id, recommendation_id=recommendation_id, lang=lang,
    )


@router.get("/recommendations/{recommendation_id}/report", response_model=UnifiedCropReportResponse)
async def get_recommendation_report(
    recommendation_id: str = Path(...),
    lang: str = Query("en"),
    crop_id: Optional[str] = Query(None),
    farm_id: Optional[str] = Query(None),
    auth_payload: dict = Depends(require_jwt),
):
    return await report_service.get_or_create_unified_report(
        recommendation_id=recommendation_id, user_id=auth_payload.get("upid"),
        farm_id=farm_id, lang=lang, focus_crop_id=crop_id,
    )


@router.get("/crops/{crop_id}/report", response_model=SingleCropDossier)
async def get_master_crop_report(
    crop_id: str = Path(...),
    lang: str = Query("en"),
    auth_payload: dict = Depends(require_jwt),
):
    return await report_service.get_master_report(crop_id=crop_id.lower(), lang=lang)


__all__ = ["router"]
```

In `app/routers/v2/__init__.py`, alongside the existing `wizard_router` mount:
```python
from .reports import router as reports_router
router.include_router(reports_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="app;SharedBackend/src" ./.venv/Scripts/python.exe -m unittest tests.test_app_v2_reports_mount tests.test_reports_router -v`
Expected: `OK` (3 tests). Then run the entire repo's test suite once (every `tests/test_*.py` file individually, per this repo's established loop — `discover` needs `tests/__init__.py` which doesn't exist) to confirm nothing earlier in this plan regressed.

- [ ] **Step 5: Commit**

```bash
git add app/routers/v2/reports.py app/routers/v2/__init__.py tests/test_app_v2_reports_mount.py tests/test_reports_router.py
git commit -m "feat: mount crop guide reports router at /api/v2/reports"
```

---

## Deployment note (not a task — operational follow-up)

After this plan lands: run `alembic upgrade head` (Task 1's migration), then `python app/scripts/seed_crop_reports.py` once per environment (Task 6 — idempotent, safe to re-run on every deploy). The outbox replayer (Task 3) needs no separate deployment step — it starts automatically with the service via `app.py`'s `lifespan`, the same process as the API itself.

**Broker configuration is also a deployment responsibility.** `config.py`'s `broker_type` setting defaults to `"memory"` — deliberately, so local dev and CI never need a live MSK cluster. In that mode `EventBrokerService.publish()` never touches Kafka; it logs a warning and returns. This means `BROKER_TYPE=kafka` (plus the existing `MSK_BOOTSTRAP_SERVERS` / `MSK_REGION` / `MSK_ROLE_ARN` variables) **must** be set explicitly in any environment (staging, production) where `CropGuideCompleted` events are actually expected to reach Kafka. Forgetting to set it is not fatal — events aren't lost, just not published anywhere outside this service's own logs — but it will silently defeat the whole point of the Kafka integration. Check for `[MEMORY BROKER]` warnings in that environment's logs as the signal that this was missed.
