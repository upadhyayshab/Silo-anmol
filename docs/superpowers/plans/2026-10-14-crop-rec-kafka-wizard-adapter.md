# Crop Rec ↔ Agro Kafka/Wizard Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the two repos together per the TRD's inter-service contract: `ks_crop_recommendation_system_v2` gets a `/api/v2/wizard/questions` + `/api/v2/wizard/submit` adapter (reusing the existing `/options` + `/recommend` scoring engine) that publishes a `CropGuideCompleted` Kafka event, and `kissan-agromonitoring` gets the internal farm-query endpoint the wizard calls for suppression plus a real (non-stub) consumer that persists that event — completing the loop the previous Agro-only retrofit left stubbed.

**Architecture:** Two repos, ten tasks. Agro side (Tasks A1–A4): a new `internal_service_secret`-gated dependency, a new `GET /api/v6/internal/farms/{farmId}` endpoint on the existing Agro v6 router (dual-alias response shape — this is Agro's own existing versioning, unrelated to Crop Rec's new v2), and the already-stubbed `crop_guide_completed_consumer.py` wired to `FarmManager` with the Standing Crop Protection rule enforced. Crop Rec side (Tasks B1–B6): a Kafka producer module mirroring Agro's `kafka_client.py`, an HTTP client to Agro's new internal endpoint, new wizard Pydantic models matching `FM_frontend_api_contract_guide.md`'s `WizardAnswersPayload`/`WizardSubmitRequest` TypeScript interfaces verbatim, and a new `app/routers/v2/wizard.py` (Crop Rec's existing `/options`+`/recommend` live at `/api/v1` — the wizard adapter is a new `/api/v2`, per explicit instruction, not a v6 match with Agro) that wraps the existing `CropScoringEngine` rather than reimplementing scoring.

**Tech Stack:** FastAPI, confluent-kafka (AWS MSK IAM auth, mirroring Agro's existing producer/consumer factory), Pydantic v2, SQLAlchemy 2.0 async, `unittest` (stdlib, matching both repos' existing test convention — no new test framework).

**Spec:** `E:\AGRO\kissan-agromonitoring\docs\MSK\updated_agromonitoring_technical_requirements.md` sections 8.2 (wizard questions + suppression), 8.6–8.7 (wizard submit + event emission), 9.2 (internal farm query), 9.3 (`CropGuideCompleted` event schema + consumer logic) and `E:\AGRO\kissan-agromonitoring\docs\MSK\FM_frontend_api_contract_guide.md` section 4 (`WizardAnswersPayload`, `WizardSubmitRequest` TypeScript interfaces).

## Global Constraints

- **Standing Crop Protection (TRD §9.3, non-negotiable):** the consumer MUST NOT overwrite `farms.crop` or `farms.has_active_crop` on an existing farm (`event.farm_id is not None`). Only `farms.boundary_status = "PENDING"` + `coordinates = NULL` apply on the NO-farm create path.
- **`CropGuideCompleted` event schema is exact** (TRD §9.3): `{event_id, event_type: "CropGuideCompleted", version: "1.0", timestamp, user_id, farm_id, data: {farm_name, center, has_active_crop, crop, sowing_date, previous_crop, irrigation_methods, season, budget, livestock}}`.
- **Internal farm-query response is dual-alias** (TRD §9.2): both `id`/`farm_id`, both `center`/`location`, both `irrigation_methods`/`irrigation` keys present in the same response.
- **Wizard payload shape is the frontend contract guide's `WizardAnswersPayload`/`WizardSubmitRequest` TS interfaces verbatim** — field names `location`, `season`, `has_active_crop`, `crop`, `sowing_date`, `previous_crop`, `irrigation`, `budget`, `livestock`, `farm_name`; request wraps it as `{farmId, answers}`.
- **No new test framework.** Agro side: `unittest.IsolatedAsyncioTestCase` + `unittest.mock`, run via `.venv/Scripts/python.exe -m unittest` with `PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app"` (Windows `;` separator). Crop Rec side: same pattern, `PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app"`, via Crop Rec's own `.venv/Scripts/python.exe`.
- **Field-mapping rulings** (the TRD's wizard vocabulary and the already-built `CropScoringEngine`'s vocabulary don't match 1:1 — these are the translation rules Task B5 implements, recorded here so every task sees the same mapping): season `Kharif→KHARIF, Rabi→RABI, Summer→ZAID, Annual→LONGTERM` (case-insensitive on the English word); budget `low→B1, medium→B3, high→B4` (collapsing 3 wizard tiers onto the engine's 4 `BudgetBucket` values — B2 has no wizard-reachable input, a deliberate simplification, not a bug); irrigation method names are upper-cased before matching `WaterMethod`.
- **Dependency additions required before Crop Rec tests can import the new Kafka module:** `confluent-kafka>=2.16.0` and `aws-msk-iam-sasl-signer-python>=1.0.1` (same versions as Agro's `pyproject.toml`) must be installed into Crop Rec's `.venv` — Task B1 includes the install command as its own step, not an assumption.
- **Never run the Agro consumer's real Kafka connection or publish a real event against the live MSK cluster during this plan.** All tests mock `services.kafka_client` / the Kafka producer call. The one place a real network call would be needed (the actual end-to-end MSK round-trip) is explicitly out of scope — flagged at the end for manual verification.

## Review Focus

- **Consumer idempotency / at-least-once redelivery is still unimplemented** (the existing file's own `ponytail:` comment already flags no Redis store exists) — `handle_event()` must not crash or double-charge subscription quota on a redelivered event; Task A4's tests must cover calling `handle_event` twice with the same event and confirm it doesn't error (even though true dedup is out of scope, matching the existing documented gap).
- **The internal farm endpoint is called synchronously inside `GET /wizard/questions`** — if Agro is down or slow, the whole wizard-questions request must not hang indefinitely or crash uglily; Task B2's HTTP client needs a timeout and Task B4's endpoint needs to degrade (treat as "no farm context" / unsuppressed questions) rather than 500 when the call fails.
- **`POST /wizard/submit` publishing to Kafka must never break the synchronous recommendation response** — TRD's own diagram shows the HTTP 200 and the Kafka publish as parallel branches from the same submit, not a dependency chain; a Kafka/MSK outage must not turn a working recommendation into a 500.
- **A farmId passed to `/wizard/submit` that doesn't exist (404 from Agro's internal endpoint)** — must not crash; the wizard should treat it the same as no farmId (fall through to requiring `answers.location`) and, if `answers.location` is also missing in that case, return a clear 400 rather than letting `scoring_engine.recommend()` fail on `lat=None`.
- **Season/budget/irrigation values outside the known mapping** (e.g. a wizard payload sends `"season": "Autumn"`, not one of the four TRD-named seasons) — must return a clear 400, not a silent `KeyError` 500 inside the mapping dict lookups Task B5 writes.

---

## Agro side (kissan-agromonitoring)

### Task A1: Internal-service shared secret — config + auth dependency

**Files:**
- Modify: `E:\AGRO\kissan-agromonitoring\app\config.py`
- Modify: `E:\AGRO\kissan-agromonitoring\app\utils\dependencies.py`
- Test: `E:\AGRO\kissan-agromonitoring\tests\test_verify_internal_service.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `settings.internal_service_secret: str` (config.py), `verify_internal_service(request: Request) -> str` dependency (dependencies.py, returns the validated `X-Internal-Service` header value) — consumed by Task A3's endpoint.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the internal-service auth dependency — no DB, no app import."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestVerifyInternalService(IsolatedAsyncioTestCase):
    @patch("utils.dependencies.settings")
    async def test_valid_secret_and_service_name_passes(self, mock_settings):
        from utils.dependencies import verify_internal_service

        mock_settings.internal_service_secret = "shared-secret-123"
        request = MagicMock()
        request.headers = {
            "X-Internal-Service": "crop-recommendation",
            "X-Internal-Secret": "shared-secret-123",
        }

        result = await verify_internal_service(request)
        self.assertEqual(result, "crop-recommendation")

    @patch("utils.dependencies.settings")
    async def test_wrong_secret_raises_403(self, mock_settings):
        from utils.dependencies import verify_internal_service
        from fastapi import HTTPException

        mock_settings.internal_service_secret = "shared-secret-123"
        request = MagicMock()
        request.headers = {
            "X-Internal-Service": "crop-recommendation",
            "X-Internal-Secret": "wrong-secret",
        }

        with self.assertRaises(HTTPException) as ctx:
            await verify_internal_service(request)
        self.assertEqual(ctx.exception.status_code, 403)

    @patch("utils.dependencies.settings")
    async def test_missing_headers_raises_401(self, mock_settings):
        from utils.dependencies import verify_internal_service
        from fastapi import HTTPException

        mock_settings.internal_service_secret = "shared-secret-123"
        request = MagicMock()
        request.headers = {}

        with self.assertRaises(HTTPException) as ctx:
            await verify_internal_service(request)
        self.assertEqual(ctx.exception.status_code, 401)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_verify_internal_service -v`
Expected: FAIL — `ImportError: cannot import name 'verify_internal_service' from 'utils.dependencies'`.

- [ ] **Step 3: Add the config setting and dependency**

In `app/config.py`, find the `msk_bootstrap_servers` block (around the `MSK_BOOTSTRAP_SERVERS` env var) and add directly below it:

```python
    internal_service_secret: str = os.getenv("INTERNAL_SERVICE_SECRET", "")
```

In `app/utils/dependencies.py`, add near `admin_required` (reusing the module's existing `settings` and `HTTPException`/`Request` imports already present at the top of the file):

```python
async def verify_internal_service(request: Request) -> str:
    """Gates internal-only endpoints (TRD §9.2) behind a shared secret header,
    bound to internal network/VPC in production — this check is the
    application-layer backstop."""
    service_name = request.headers.get("X-Internal-Service")
    secret = request.headers.get("X-Internal-Secret")

    if not service_name or not secret:
        raise HTTPException(status_code=401, detail="X-Internal-Service and X-Internal-Secret headers required")

    if not settings.internal_service_secret or secret != settings.internal_service_secret:
        raise HTTPException(status_code=403, detail="Invalid internal service secret")

    return service_name
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_verify_internal_service -v`
Expected: `OK` (3 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/utils/dependencies.py tests/test_verify_internal_service.py
git commit -m "feat: internal-service shared-secret auth dependency"
```

---

### Task A2: Internal farm-query endpoint (dual-alias response)

**Files:**
- Modify: `E:\AGRO\kissan-agromonitoring\app\routers\v6\farms.py` (append route)
- Test: `E:\AGRO\kissan-agromonitoring\tests\test_internal_farms_v6.py`

**Interfaces:**
- Consumes: `router`, `farm_manager`, `_farm_to_dict` (already in this file from the previous plan); `verify_internal_service` (Task A1).
- Produces: `get_internal_farm_v6` route function.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the internal farm-query endpoint."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestInternalFarmQueryV6(IsolatedAsyncioTestCase):
    @patch("routers.v6.farms.farm_manager")
    async def test_returns_dual_alias_shape(self, mock_farm_manager):
        from routers.v6.farms import get_internal_farm_v6

        farm = MagicMock(
            uid="farm-1", user_uid="user-1", name="Green Valley Farm",
            center=[77.7125, 13.1985], has_active_crop=True, crop="Tomato",
            sowing_date="2026-10-01", previous_crop="Wheat",
            irrigation_methods=["Drip"], livestock="Cattle farm - Cows & Buffaloes",
            boundary_status="MAPPED",
        )
        mock_farm_manager.fetch_one = AsyncMock(return_value=farm)

        result = await get_internal_farm_v6(farmId="farm-1")

        self.assertEqual(result["id"], "farm-1")
        self.assertEqual(result["farm_id"], "farm-1")
        self.assertEqual(result["center"], [77.7125, 13.1985])
        self.assertEqual(result["location"], [77.7125, 13.1985])
        self.assertEqual(result["irrigation_methods"], ["Drip"])
        self.assertEqual(result["irrigation"], ["Drip"])
        self.assertEqual(result["boundary_status"], "MAPPED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_internal_farms_v6 -v`
Expected: FAIL — `ImportError: cannot import name 'get_internal_farm_v6' from 'routers.v6.farms'`.

- [ ] **Step 3: Append the route to `app/routers/v6/farms.py`**

Add this import near the top, alongside the other `utils` import:

```python
from utils.dependencies import verify_internal_service
```

Append at the end of the file:

```python
@router.get("/internal/farms/{farmId}", dependencies=[Depends(verify_internal_service)])
async def get_internal_farm_v6(farmId: str):
    """TRD §9.2: internal farm-query for Crop Rec's wizard suppression.
    Dual-alias response (id/farm_id, center/location, irrigation_methods/
    irrigation) for interoperability with the Crop Rec service's own field
    names. No user_uid filter — this is a trusted internal call, farmId
    alone is the lookup key (matches the TRD's literal contract, which
    takes X-User-Id as an informational header, not a query filter)."""
    farm = await farm_manager.fetch_one(filters={"uid": farmId})
    return {
        "id": farm.uid,
        "farm_id": farm.uid,
        "user_id": farm.user_uid,
        "name": farm.name,
        "center": farm.center,
        "location": farm.center,
        "has_active_crop": farm.has_active_crop,
        "crop": farm.crop,
        "sowing_date": farm.sowing_date,
        "previous_crop": farm.previous_crop,
        "irrigation_methods": farm.irrigation_methods,
        "irrigation": farm.irrigation_methods,
        "livestock": farm.livestock,
        "boundary_status": farm.boundary_status,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_internal_farms_v6 -v`
Expected: `OK` (1 test passed).

- [ ] **Step 5: Commit**

```bash
git add app/routers/v6/farms.py tests/test_internal_farms_v6.py
git commit -m "feat: internal farm-query endpoint for Crop Rec wizard suppression"
```

---

### Task A3: Add `create_with_meta`-friendly FarmManager usage check + wire the consumer (NO-farm path)

**Files:**
- Modify: `E:\AGRO\kissan-agromonitoring\app\workers\crop_guide_completed_consumer.py`
- Test: `E:\AGRO\kissan-agromonitoring\tests\test_crop_guide_completed_consumer.py`

**Interfaces:**
- Consumes: `FarmManager`, `FarmSchema` (`app/managers/farmManager.py`).
- Produces: `handle_event(event: dict) -> None` (same signature, now with real behavior) — consumed by Task A4 (which only adds the HAS-farm branch to the same function).

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the CropGuideCompleted consumer's NO-farm (create) path."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestConsumerCreatePath(IsolatedAsyncioTestCase):
    @patch("workers.crop_guide_completed_consumer.farm_manager")
    async def test_no_farm_id_creates_pending_farm(self, mock_farm_manager):
        from workers.crop_guide_completed_consumer import handle_event

        created = MagicMock(uid="farm-new-1")
        mock_farm_manager.create = AsyncMock(return_value=created)

        event = {
            "event_id": "evt-1", "event_type": "CropGuideCompleted", "version": "1.0",
            "timestamp": "2026-10-06T08:15:00Z", "user_id": "usr_99182", "farm_id": None,
            "data": {
                "farm_name": "My New Farm", "center": [73.8567, 18.5204],
                "has_active_crop": True, "crop": "Wheat", "sowing_date": "2026-10-01",
                "previous_crop": "Soybean", "irrigation_methods": ["Drip"],
                "season": "Rabi", "budget": "20K-40K",
                "livestock": "Cattle farm - Cows & Buffaloes",
            },
        }

        await handle_event(event)

        created_schema_arg = mock_farm_manager.create.call_args[0][0]
        self.assertEqual(created_schema_arg.user_uid, "usr_99182")
        self.assertEqual(created_schema_arg.name, "My New Farm")
        self.assertEqual(created_schema_arg.center, [73.8567, 18.5204])
        self.assertIsNone(created_schema_arg.coordinates)
        self.assertEqual(created_schema_arg.boundary_status, "PENDING")
        self.assertEqual(created_schema_arg.crop, "Wheat")
        self.assertEqual(created_schema_arg.has_active_crop, True)
        self.assertEqual(created_schema_arg.livestock, "Cattle farm - Cows & Buffaloes")

    async def test_unknown_event_type_is_skipped_not_errored(self):
        from workers.crop_guide_completed_consumer import handle_event
        # Must not raise — just logs and returns.
        await handle_event({"event_type": "SomethingElse"})

    @patch("workers.crop_guide_completed_consumer.farm_manager")
    async def test_handling_same_event_twice_does_not_raise(self, mock_farm_manager):
        # Review Focus: no idempotency store exists yet (documented gap) —
        # redelivery must not crash, even though true dedup is out of scope.
        from workers.crop_guide_completed_consumer import handle_event

        mock_farm_manager.create = AsyncMock(return_value=MagicMock(uid="farm-new-1"))
        event = {
            "event_id": "evt-1", "event_type": "CropGuideCompleted", "farm_id": None,
            "user_id": "usr_1", "data": {"farm_name": "F", "center": [1.0, 2.0], "has_active_crop": False},
        }
        await handle_event(event)
        await handle_event(event)  # redelivery — must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_crop_guide_completed_consumer -v`
Expected: FAIL — `test_no_farm_id_creates_pending_farm` fails with `AttributeError: <module ...> does not have the attribute 'farm_manager'` (the module has no `farm_manager` yet).

- [ ] **Step 3: Wire the consumer's create path**

Replace the full contents of `app/workers/crop_guide_completed_consumer.py` with:

```python
"""
Standalone MSK consumer for the `crop_guide_completed` topic (event contract:
docs TRD section 9.3). Run as its own process so a slow/stuck consumer never
blocks the FastAPI event loop. Run with the same PYTHONPATH as app/app.py
(see run.sh/run.bat):

    python app/workers/crop_guide_completed_consumer.py

ponytail: no idempotency store wired yet (TRD calls for a Redis
processed_events:{event_id} check) because this codebase has no live Redis
client today. At-least-once delivery means handle_event() may run twice for
the same event_id — for the create path this means a duplicate PENDING farm
row rather than a crash; add the Redis check when that's a real concern.
"""
import asyncio
import json
import logging

from confluent_kafka import KafkaError

from config import get_engine, get_settings
from managers.farmManager import FarmManager, FarmSchema
from services.kafka_client import make_consumer

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)
farm_manager = FarmManager(engine)


async def handle_event(event: dict) -> None:
    if event.get("event_type") != "CropGuideCompleted":
        logger.warning("Skipping unknown event_type: %s", event.get("event_type"))
        return

    data = event.get("data", {})
    farm_id = event.get("farm_id")
    user_id = event.get("user_id")
    logger.info(
        "CropGuideCompleted event_id=%s farm_id=%s user_id=%s",
        event.get("event_id"), farm_id, user_id,
    )

    if farm_id is None:
        # NO Farm Path (TRD §9.3): fresh farm discovery, boundary_status
        # always PENDING / coordinates always NULL regardless of event data.
        await farm_manager.create(
            FarmSchema(
                user_uid=user_id,
                name=data.get("farm_name") or "My Farm",
                coordinates=None,
                center=data.get("center"),
                boundary_status="PENDING",
                has_active_crop=bool(data.get("has_active_crop")),
                crop=data.get("crop"),
                sowing_date=data.get("sowing_date"),
                previous_crop=data.get("previous_crop"),
                irrigation_methods=data.get("irrigation_methods"),
                livestock=data.get("livestock"),
            ),
        )
    else:
        # HAS Farm Path (TRD §9.3): wired in Task A4 — Standing Crop Protection.
        pass


async def run() -> None:
    consumer = make_consumer(settings.crop_guide_completed_topic, settings.crop_guide_completed_group_id)
    logger.info(
        "Consuming topic=%s group=%s",
        settings.crop_guide_completed_topic,
        settings.crop_guide_completed_group_id,
    )
    try:
        while True:
            msg = await asyncio.to_thread(consumer.poll, 1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("Consumer error: %s", msg.error())
                continue

            try:
                raw_val = msg.value()
                if raw_val is not None:
                    event = json.loads(raw_val.decode("utf-8"))
                    await handle_event(event)
                await asyncio.to_thread(consumer.commit, msg, asynchronous=False)
            except Exception:
                logger.exception("Failed to process message at offset %s", msg.offset())
    finally:
        logger.info("Closing Kafka consumer")
        await asyncio.to_thread(consumer.close)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Consumer stopped by user")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_crop_guide_completed_consumer -v`
Expected: `OK` (3 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/workers/crop_guide_completed_consumer.py tests/test_crop_guide_completed_consumer.py
git commit -m "feat: wire CropGuideCompleted consumer's create (NO-farm) path to FarmManager"
```

---

### Task A4: Consumer's HAS-farm path — Standing Crop Protection

**Files:**
- Modify: `E:\AGRO\kissan-agromonitoring\app\workers\crop_guide_completed_consumer.py` (replace the `pass` branch)
- Test: `E:\AGRO\kissan-agromonitoring\tests\test_crop_guide_completed_consumer.py` (append)

**Interfaces:**
- Consumes: `handle_event` (Task A3, same function, HAS-farm branch now implemented).
- Produces: nothing new — this completes `handle_event`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crop_guide_completed_consumer.py`:

```python
class TestConsumerUpdatePath(IsolatedAsyncioTestCase):
    @patch("workers.crop_guide_completed_consumer.farm_manager")
    async def test_existing_farm_never_overwrites_crop_or_has_active_crop(self, mock_farm_manager):
        # CRITICAL — TRD §9.3 Standing Crop Protection: the farm's real
        # standing crop (e.g. Tomato, under active satellite monitoring)
        # must never be silently replaced by the wizard's advisory crop.
        from workers.crop_guide_completed_consumer import handle_event

        mock_farm_manager.update = AsyncMock()
        event = {
            "event_id": "evt-2", "event_type": "CropGuideCompleted", "farm_id": "farm-existing-1",
            "user_id": "usr_1",
            "data": {
                "farm_name": "Existing Farm", "center": [73.0, 18.0],
                "has_active_crop": True, "crop": "Watermelon",  # advisory rotation suggestion — must NOT land in the DB
                "irrigation_methods": ["Drip"], "livestock": "Cattle farm - Cows & Buffaloes",
            },
        }

        await handle_event(event)

        mock_farm_manager.update.assert_awaited_once()
        updates_arg = mock_farm_manager.update.call_args.kwargs.get("updates") or mock_farm_manager.update.call_args[1].get("updates")
        self.assertNotIn("crop", updates_arg)
        self.assertNotIn("has_active_crop", updates_arg)
        self.assertEqual(updates_arg["livestock"], "Cattle farm - Cows & Buffaloes")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_crop_guide_completed_consumer.TestConsumerUpdatePath -v`
Expected: FAIL — `mock_farm_manager.update.assert_awaited_once()` raises `AssertionError: Expected update to have been called once. Called 0 times.` (the HAS-farm branch is still just `pass`).

- [ ] **Step 3: Implement the HAS-farm branch**

In `app/workers/crop_guide_completed_consumer.py`, replace:

```python
    else:
        # HAS Farm Path (TRD §9.3): wired in Task A4 — Standing Crop Protection.
        pass
```

with:

```python
    else:
        # HAS Farm Path (TRD §9.3): CRITICAL STANDING CROP PROTECTION — the
        # farm's real standing crop stays under active monitoring; the
        # wizard's crop/has_active_crop are advisory only and never land here.
        # Only onboarding metadata the farm doesn't already have is patched in.
        updates = {}
        if data.get("livestock") is not None:
            updates["livestock"] = data["livestock"]
        if data.get("irrigation_methods"):
            updates["irrigation_methods"] = data["irrigation_methods"]
        if updates:
            await farm_manager.update(uid=farm_id, updates=updates)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/kissan-agromonitoring && export PYTHONPATH="E:\AGRO\kissan-agromonitoring\SharedBackend\src;E:\AGRO\kissan-agromonitoring\app" && .venv/Scripts/python.exe -m unittest tests.test_crop_guide_completed_consumer -v`
Expected: `OK` (4 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/workers/crop_guide_completed_consumer.py tests/test_crop_guide_completed_consumer.py
git commit -m "feat: consumer HAS-farm path — Standing Crop Protection, metadata-only patch"
```

---

## Crop Rec side (ks_crop_recommendation_system_v2)

### Task B1: Kafka producer infra

**Files:**
- Modify: `E:\AGRO\ks_crop_recommendation_system_v2\requirements\dev.txt`
- Create: `E:\AGRO\ks_crop_recommendation_system_v2\app\services\kafka_client.py`
- Modify: `E:\AGRO\ks_crop_recommendation_system_v2\app\config.py`
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_kafka_client.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `publish_event(topic: str, payload: str | bytes, key: str | None = None) -> None` (async) — consumed by Task B6.

- [ ] **Step 1: Install the new dependencies into Crop Rec's venv**

```bash
cd E:/AGRO/ks_crop_recommendation_system_v2
.venv/Scripts/python.exe -m pip install "confluent-kafka>=2.16.0" "aws-msk-iam-sasl-signer-python>=1.0.1"
```

Then add the two lines to `requirements/dev.txt` (the file's only populated requirements file — `prod.txt`/`stage.txt` are currently empty in this repo):

```
confluent-kafka>=2.16.0
aws-msk-iam-sasl-signer-python>=1.0.1
```

- [ ] **Step 2: Write the failing test**

```python
"""Unit tests for the Kafka producer wrapper — mocks confluent_kafka.Producer
entirely, never touches a real broker."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestPublishEvent(IsolatedAsyncioTestCase):
    @patch("services.kafka_client.make_producer")
    async def test_publish_event_produces_and_flushes(self, mock_make_producer):
        from services.kafka_client import publish_event

        mock_producer = MagicMock()
        mock_producer.flush.return_value = 0  # 0 = nothing left un-flushed
        mock_make_producer.return_value = mock_producer

        await publish_event("crop_guide_completed", '{"event_id": "evt-1"}', key="usr_1")

        mock_producer.produce.assert_called_once()
        call_kwargs = mock_producer.produce.call_args.kwargs
        self.assertEqual(call_kwargs["topic"], "crop_guide_completed")
        self.assertEqual(call_kwargs["value"], b'{"event_id": "evt-1"}')
        self.assertEqual(call_kwargs["key"], b"usr_1")

    @patch("services.kafka_client.make_producer")
    async def test_publish_event_raises_on_flush_timeout(self, mock_make_producer):
        from services.kafka_client import publish_event
        from confluent_kafka import KafkaException

        mock_producer = MagicMock()
        mock_producer.flush.return_value = 1  # 1 message still un-flushed = timeout
        mock_make_producer.return_value = mock_producer

        with self.assertRaises(KafkaException):
            await publish_event("crop_guide_completed", "{}", key=None)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_kafka_client -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.kafka_client'`.

- [ ] **Step 4: Add the config settings and write the producer module**

In `app/config.py`, add to the `Settings` class (alongside the existing `subscription_base_url` line):

```python
    msk_bootstrap_servers: str = os.getenv("MSK_BOOTSTRAP_SERVERS", "")
    msk_region: str = os.getenv("MSK_REGION", "") or os.getenv("REGION_NAME", "ap-south-1")
    msk_role_arn: str = os.getenv("MSK_ROLE_ARN", "")
    crop_guide_completed_topic: str = os.getenv("CROP_GUIDE_COMPLETED_TOPIC", "crop_guide_completed")
```

Create `app/services/kafka_client.py` (producer-only — this service never consumes):

```python
"""
AWS MSK producer using confluent-kafka with IAM auth (SASL_SSL + OAUTHBEARER),
mirroring kissan-agromonitoring's app/services/kafka_client.py exactly (same
MSK cluster/topic). Crop Rec only ever publishes — no consumer here.
"""
import asyncio
import logging
from typing import Any, Callable

from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
from confluent_kafka import KafkaException, Producer

from config import get_settings

logger = logging.getLogger(__name__)


def _make_oauth_cb(region: str, role_arn: str | None = None) -> Callable[[str], tuple[str, float]]:
    def oauth_cb(oauth_config: str) -> tuple[str, float]:
        if role_arn:
            auth_token, expiry_ms = MSKAuthTokenProvider.generate_auth_token_from_role_arn(region, role_arn)
        else:
            auth_token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        return auth_token, expiry_ms / 1000.0

    return oauth_cb


def _msk_conf(extra_conf: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.msk_bootstrap_servers:
        raise RuntimeError("MSK_BOOTSTRAP_SERVERS is not set")

    conf: dict[str, Any] = {
        "bootstrap.servers": settings.msk_bootstrap_servers,
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "oauth_cb": _make_oauth_cb(settings.msk_region, settings.msk_role_arn or None),
    }
    if extra_conf:
        conf.update(extra_conf)
    return conf


def make_producer(extra_conf: dict[str, Any] | None = None) -> Producer:
    return Producer(_msk_conf(extra_conf))


async def publish_event(topic: str, payload: str | bytes, key: str | None = None, timeout: float = 10.0) -> None:
    """One-shot publish: spins up a producer, sends, flushes, verifies delivery.
    Runs in a worker thread so the FastAPI event loop stays unblocked."""
    def _produce_sync() -> None:
        producer = make_producer({"client.id": "crop-rec-publisher"})
        delivery_error: Exception | None = None

        def _on_delivery(err: Any, msg: Any) -> None:
            nonlocal delivery_error
            if err is not None:
                delivery_error = KafkaException(err)

        val_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        key_bytes = key.encode("utf-8") if key is not None else None

        producer.produce(topic=topic, value=val_bytes, key=key_bytes, on_delivery=_on_delivery)
        remaining = producer.flush(timeout=timeout)
        if remaining > 0:
            raise KafkaException(f"Timed out flushing producer ({remaining} message(s) un-flushed)")
        if delivery_error is not None:
            raise delivery_error

    await asyncio.to_thread(_produce_sync)


__all__ = ["make_producer", "publish_event"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_kafka_client -v`
Expected: `OK` (2 tests passed).

- [ ] **Step 6: Commit**

```bash
git add requirements/dev.txt app/services/kafka_client.py app/config.py tests/test_kafka_client.py
git commit -m "feat: Kafka producer infra mirroring Agro's kafka_client.py"
```

---

### Task B2: Agro internal-farm HTTP client

**Files:**
- Create: `E:\AGRO\ks_crop_recommendation_system_v2\app\services\agroInternalService.py`
- Modify: `E:\AGRO\ks_crop_recommendation_system_v2\app\config.py`
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_agro_internal_service.py`

**Interfaces:**
- Consumes: nothing new (uses `httpx`, already a dependency via `locationService.py`'s pattern).
- Produces: `AgroInternalService.get_farm(farm_id: str, user_id: str) -> dict | None` (returns `None` on any failure — 404, timeout, network error — per Review Focus: callers degrade gracefully, never crash) — consumed by Tasks B4 and B5.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for AgroInternalService — mocks httpx entirely."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestAgroInternalService(IsolatedAsyncioTestCase):
    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_farm_dict_on_200(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"id": "farm-1", "center": [77.7, 13.1]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("farm-1", "usr_1")

        self.assertEqual(result["id"], "farm-1")
        call_kwargs = mock_client.get.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["X-Internal-Service"], "crop-recommendation")
        self.assertEqual(call_kwargs["headers"]["X-Internal-Secret"], "s3cret")
        self.assertEqual(call_kwargs["headers"]["X-User-Id"], "usr_1")

    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_none_on_404(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService

        mock_response = MagicMock(status_code=404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("nonexistent", "usr_1")

        self.assertIsNone(result)

    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_none_on_network_error_not_raises(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("farm-1", "usr_1")

        self.assertIsNone(result)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_agro_internal_service -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.agroInternalService'`.

- [ ] **Step 3: Add config settings and write the service**

In `app/config.py`, add alongside `subscription_base_url`:

```python
    agro_internal_base_url: str = os.getenv("AGRO_INTERNAL_BASE_URL", "")
    internal_service_secret: str = os.getenv("INTERNAL_SERVICE_SECRET", "")
```

Create `app/services/agroInternalService.py`:

```python
"""
HTTP client for Agromonitoring's internal farm-query endpoint
(GET /api/v6/internal/farms/{farmId}, TRD §9.2) — used by the wizard's
farmId-based question suppression. Returns None on any failure (farm not
found, Agro unreachable, timeout) rather than raising: callers degrade to
"no farm context" instead of crashing the wizard request (Review Focus).
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agro_internal_service")


class AgroInternalService:
    def __init__(self, base_url: str, shared_secret: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self.timeout = timeout

    async def get_farm(self, farm_id: str, user_id: str) -> Optional[dict]:
        if not self.base_url:
            logger.warning("AGRO_INTERNAL_BASE_URL is not configured.")
            return None

        url = f"{self.base_url}/api/v6/internal/farms/{farm_id}"
        headers = {
            "X-Internal-Service": "crop-recommendation",
            "X-Internal-Secret": self.shared_secret,
            "X-User-Id": user_id,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            logger.warning(f"Agro internal farm query failed for {farm_id}: {exc}")
            return None

        if response.status_code != 200:
            logger.info(f"Agro internal farm query non-200 for {farm_id}: {response.status_code}")
            return None

        return response.json()


__all__ = ["AgroInternalService"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_agro_internal_service -v`
Expected: `OK` (3 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/agroInternalService.py app/config.py tests/test_agro_internal_service.py
git commit -m "feat: Agro internal farm-query HTTP client"
```

---

### Task B3: Wizard Pydantic models

**Files:**
- Create: `E:\AGRO\ks_crop_recommendation_system_v2\app\models\wizardModels.py`
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_wizard_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WizardQuestionOption`, `WizardQuestion`, `WizardQuestionsResponse`, `WizardAnswersPayload`, `WizardSubmitRequest`, `WizardRecommendationItem`, `WizardSubmitResponse` — consumed by Tasks B4 and B5.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the wizard Pydantic models — matches
FM_frontend_api_contract_guide.md's WizardAnswersPayload/WizardSubmitRequest
TS interfaces field-for-field."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


class TestWizardAnswersPayload(TestCase):
    def test_all_fields_optional_except_season(self):
        payload = WizardAnswersPayload(season="Rabi")
        self.assertIsNone(payload.location)
        self.assertIsNone(payload.has_active_crop)
        self.assertIsNone(payload.irrigation)

    def test_full_payload_round_trips(self):
        payload = WizardAnswersPayload(
            location=[73.85, 18.52], season="Kharif", has_active_crop="Yes",
            crop="Tomato", sowing_date="2026-10-01", previous_crop="Wheat",
            irrigation=["Drip"], budget="high", livestock="cattle", farm_name="North Field",
        )
        self.assertEqual(payload.season, "Kharif")
        self.assertEqual(payload.irrigation, ["Drip"])


class TestWizardSubmitRequest(TestCase):
    def test_farm_id_nullable(self):
        req = WizardSubmitRequest(farmId=None, answers=WizardAnswersPayload(season="Rabi"))
        self.assertIsNone(req.farmId)

    def test_farm_id_present(self):
        req = WizardSubmitRequest(farmId="farm-1", answers=WizardAnswersPayload(season="Rabi"))
        self.assertEqual(req.farmId, "farm-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_models -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.wizardModels'`.

- [ ] **Step 3: Write the models**

```python
from typing import List, Optional

from pydantic import BaseModel, Field


class WizardQuestionOption(BaseModel):
    label: str
    value: str


class WizardQuestion(BaseModel):
    id: str
    type: str
    label: str
    description: Optional[str] = None
    options: Optional[List[WizardQuestionOption]] = None
    required: bool = True


class FarmContext(BaseModel):
    farm_id: str
    name: str
    location: Optional[List[float]] = None
    crop: Optional[str] = None
    has_active_crop: bool = False
    irrigation: Optional[List[str]] = None
    boundary_status: Optional[str] = None


class WizardQuestionsResponse(BaseModel):
    farm_context: Optional[FarmContext] = None
    questions: List[WizardQuestion]


class WizardAnswersPayload(BaseModel):
    """Matches FM_frontend_api_contract_guide.md's WizardAnswersPayload TS
    interface field-for-field."""
    location: Optional[List[float]] = Field(None, description="[lng, lat]")
    season: str
    has_active_crop: Optional[str] = None  # "Yes" | "No" | string, per the TS interface
    crop: Optional[str] = None
    sowing_date: Optional[str] = None
    previous_crop: Optional[str] = None
    irrigation: Optional[List[str]] = None
    budget: Optional[str] = None
    livestock: Optional[str] = None
    farm_name: Optional[str] = None


class WizardSubmitRequest(BaseModel):
    farmId: Optional[str] = None
    answers: WizardAnswersPayload


class WizardRecommendationItem(BaseModel):
    id: str
    crop_name: str
    suitability_score: float
    roi_estimate: str
    duration_days: Optional[int] = None
    water_requirement: Optional[str] = None
    reason: str


class WizardSubmitResponse(BaseModel):
    recommendations: List[WizardRecommendationItem]


__all__ = [
    "WizardQuestionOption", "WizardQuestion", "FarmContext", "WizardQuestionsResponse",
    "WizardAnswersPayload", "WizardSubmitRequest", "WizardRecommendationItem", "WizardSubmitResponse",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_models -v`
Expected: `OK` (4 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/models/wizardModels.py tests/test_wizard_models.py
git commit -m "feat: wizard Pydantic models matching frontend contract guide"
```

---

### Task B4: `GET /api/v2/wizard/questions` — SDUI catalog with farmId suppression

**Files:**
- Create: `E:\AGRO\ks_crop_recommendation_system_v2\app\routers\v2\__init__.py`
- Create: `E:\AGRO\ks_crop_recommendation_system_v2\app\routers\v2\wizard.py`
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_wizard_questions.py`

**Interfaces:**
- Consumes: `CropManager` (`app/managers/cropManagers.py`); `WizardQuestion`, `WizardQuestionOption`, `WizardQuestionsResponse`, `FarmContext` (Task B3); `AgroInternalService` (Task B2).
- Produces: `router` (new `APIRouter`, this file's module-level object — Task B5 adds a second route onto it), `get_wizard_questions` route function.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for GET /wizard/questions — suppression logic per TRD §8.2."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestWizardQuestions(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.crop_manager")
    async def test_no_farm_id_returns_all_questions_unsuppressed(self, mock_crop_manager):
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = [MagicMock(name_en="Rice"), MagicMock(name_en="Wheat")]
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)

        result = await get_wizard_questions(farmId=None)

        self.assertIsNone(result.farm_context)
        question_ids = [q.id for q in result.questions]
        self.assertIn("location", question_ids)
        self.assertIn("season", question_ids)
        self.assertIn("has_active_crop", question_ids)
        self.assertIn("irrigation", question_ids)
        self.assertIn("budget", question_ids)
        self.assertIn("livestock", question_ids)
        self.assertIn("farm_name", question_ids)

    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.crop_manager")
    async def test_farm_id_with_center_suppresses_location_and_crop_fields(
        self, mock_crop_manager, mock_agro_service
    ):
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = []
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "name": "Green Valley Farm", "center": [77.7, 13.1],
            "crop": "Tomato", "has_active_crop": True, "previous_crop": "Wheat",
            "irrigation_methods": ["Drip"], "boundary_status": "MAPPED",
        })

        result = await get_wizard_questions(farmId="farm-1")

        self.assertIsNotNone(result.farm_context)
        self.assertEqual(result.farm_context.farm_id, "farm-1")
        question_ids = [q.id for q in result.questions]
        self.assertNotIn("location", question_ids)
        self.assertNotIn("farm_name", question_ids)
        self.assertNotIn("has_active_crop", question_ids)
        self.assertNotIn("crop", question_ids)
        self.assertNotIn("sowing_date", question_ids)
        self.assertNotIn("previous_crop", question_ids)
        self.assertNotIn("irrigation", question_ids)
        # season, budget, livestock are never farm-derived — always present
        self.assertIn("season", question_ids)
        self.assertIn("budget", question_ids)
        self.assertIn("livestock", question_ids)

    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.crop_manager")
    async def test_farm_id_agro_unreachable_degrades_to_unsuppressed(
        self, mock_crop_manager, mock_agro_service
    ):
        # Review Focus: Agro being down must not crash /wizard/questions.
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = []
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_agro_service.get_farm = AsyncMock(return_value=None)

        result = await get_wizard_questions(farmId="farm-unreachable")

        self.assertIsNone(result.farm_context)
        question_ids = [q.id for q in result.questions]
        self.assertIn("location", question_ids)  # nothing suppressed — degrade gracefully
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_questions -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'routers.v2'`.

- [ ] **Step 3: Write the router**

Create `app/routers/v2/wizard.py`:

```python
from typing import Optional

from fastapi import APIRouter, Depends, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager
from models.wizardModels import (
    FarmContext, WizardQuestion, WizardQuestionOption, WizardQuestionsResponse,
)
from services.agroInternalService import AgroInternalService

router = APIRouter(tags=["Wizard v2"])
settings = get_settings()
engine = get_engine(settings.name)

crop_manager = CropManager(engine)
agro_internal_service = AgroInternalService(
    base_url=settings.agro_internal_base_url,
    shared_secret=settings.internal_service_secret,
)

ALL_QUESTIONS: list[WizardQuestion] = [
    WizardQuestion(id="location", type="map_pin", label="Where is your farm?", required=True),
    WizardQuestion(
        id="season", type="radio", label="Which season are you planning to sow for?",
        options=[
            WizardQuestionOption(label="Kharif (Monsoon: Jun - Oct)", value="Kharif"),
            WizardQuestionOption(label="Rabi (Winter: Oct - Mar)", value="Rabi"),
            WizardQuestionOption(label="Summer / Zaid (Mar - Jun)", value="Summer"),
            WizardQuestionOption(label="Perennial / Annual", value="Annual"),
        ],
    ),
    WizardQuestion(
        id="has_active_crop", type="radio", label="Do you currently have a crop growing?",
        options=[WizardQuestionOption(label="Yes", value="Yes"), WizardQuestionOption(label="No", value="No")],
    ),
    WizardQuestion(id="crop", type="search", label="Which crop is growing?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="sowing_date", type="date", label="When did you sow?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="previous_crop", type="search", label="What was grown here before?", required=False),
    WizardQuestion(
        id="irrigation", type="multi_select", label="How do you water this field?",
        options=[
            WizardQuestionOption(label="Rainfed", value="Rainfed"),
            WizardQuestionOption(label="Surface (flood/canal/furrow)", value="Surface"),
            WizardQuestionOption(label="Drip", value="Drip"),
            WizardQuestionOption(label="Sprinkler", value="Sprinkler"),
        ],
    ),
    WizardQuestion(
        id="budget", type="radio", label="What is your planned investment capacity per acre?",
        options=[
            WizardQuestionOption(label="Low (< ₹15,000 / acre)", value="low"),
            WizardQuestionOption(label="Medium (₹15,000 - ₹35,000 / acre)", value="medium"),
            WizardQuestionOption(label="High (> ₹35,000 / acre)", value="high"),
        ],
    ),
    WizardQuestion(
        id="livestock", type="radio", label="Do you have livestock on your farm?",
        options=[
            WizardQuestionOption(label="Cattle / Buffaloes", value="cattle"),
            WizardQuestionOption(label="Goats / Sheep", value="small_ruminants"),
            WizardQuestionOption(label="No livestock", value="none"),
        ],
    ),
    WizardQuestion(id="farm_name", type="text", label="Give your farm a name", required=False),
]

# TRD §8.2 suppression logic, keyed by which farm field (on the Agro internal
# response) implies the question is already answered.
SUPPRESSION_RULES: list[tuple[str, list[str]]] = [
    ("center", ["location"]),
    ("name", ["farm_name"]),
    ("crop", ["has_active_crop", "crop", "sowing_date"]),
    ("previous_crop", ["previous_crop"]),
    ("irrigation_methods", ["irrigation"]),
]


@router.get("/wizard/questions", response_model=WizardQuestionsResponse, dependencies=[Depends(require_jwt)])
async def get_wizard_questions(farmId: Optional[str] = Query(None)):
    farm_context = None
    suppressed_ids: set[str] = set()

    if farmId:
        farm = await agro_internal_service.get_farm(farmId, user_id="")
        if farm:
            farm_context = FarmContext(
                farm_id=farm.get("farm_id") or farm.get("id"),
                name=farm.get("name"),
                location=farm.get("location") or farm.get("center"),
                crop=farm.get("crop"),
                has_active_crop=bool(farm.get("has_active_crop")),
                irrigation=farm.get("irrigation") or farm.get("irrigation_methods"),
                boundary_status=farm.get("boundary_status"),
            )
            for farm_field, suppressed in SUPPRESSION_RULES:
                if farm.get(farm_field):
                    suppressed_ids.update(suppressed)

    questions = [q for q in ALL_QUESTIONS if q.id not in suppressed_ids]
    return WizardQuestionsResponse(farm_context=farm_context, questions=questions)
```

Create `app/routers/v2/__init__.py`:

```python
from fastapi import APIRouter

from .wizard import router as wizard_router

router = APIRouter()
router.include_router(wizard_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_questions -v`
Expected: `OK` (3 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/routers/v2/__init__.py app/routers/v2/wizard.py tests/test_wizard_questions.py
git commit -m "feat: GET /api/v2/wizard/questions with farmId suppression"
```

---

### Task B5: `POST /api/v2/wizard/submit` — recommendation + field mapping

**Files:**
- Modify: `E:\AGRO\ks_crop_recommendation_system_v2\app\routers\v2\wizard.py` (append route + mapping helpers)
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_wizard_submit.py`

**Interfaces:**
- Consumes: `router`, `crop_manager`, `agro_internal_service` (Task B4); `CropScoringEngine`, `RecommendationRequest`, `CropRecommendation` (`app/utils/scoringEngine.py`, `app/models/recommendationModels.py`); `WizardSubmitRequest`, `WizardSubmitResponse`, `WizardRecommendationItem` (Task B3).
- Produces: `submit_wizard` route function; `_map_season`, `_map_budget`, `_map_irrigation` helper functions (pure, no I/O) — not consumed elsewhere but kept as named functions so Task B6's event-building code can reuse `_map_season`/`_map_budget` if needed (it doesn't, but the names are stable).

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for POST /wizard/submit — field mapping and farm-context merge."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


class TestMappingHelpers(IsolatedAsyncioTestCase):
    async def test_season_mapping(self):
        from routers.v2.wizard import _map_season
        self.assertEqual(_map_season("Kharif"), "KHARIF")
        self.assertEqual(_map_season("rabi"), "RABI")
        self.assertEqual(_map_season("Summer"), "ZAID")
        self.assertEqual(_map_season("Annual"), "LONGTERM")

    async def test_season_mapping_unknown_raises_value_error(self):
        from routers.v2.wizard import _map_season
        with self.assertRaises(ValueError):
            _map_season("Autumn")

    async def test_budget_mapping(self):
        from routers.v2.wizard import _map_budget
        self.assertEqual(_map_budget("low"), "B1")
        self.assertEqual(_map_budget("medium"), "B3")
        self.assertEqual(_map_budget("high"), "B4")

    async def test_irrigation_mapping(self):
        from routers.v2.wizard import _map_irrigation
        self.assertEqual(_map_irrigation(["drip", "Sprinkler"]), ["DRIP", "SPRINKLER"])


class TestWizardSubmit(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_without_farm_id_uses_payload_location(self, mock_scoring_engine, mock_publish):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            results=[CropRecommendation(
                crop="Tomato", family="Vegetable", horizon="SEASONAL",
                total=90, max_total=100, suitability_pct=90.0,
                factors=[],
            )],
        ))

        req = WizardSubmitRequest(
            farmId=None,
            answers=WizardAnswersPayload(
                location=[73.85, 18.52], season="Rabi", irrigation=["Drip"], budget="high",
                # previous_crop omitted -> was_fallow=True, skips the crop_manager lookup entirely
            ),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})

        self.assertEqual(len(result.recommendations), 1)
        self.assertEqual(result.recommendations[0].crop_name, "Tomato")
        mock_scoring_engine.recommend.assert_awaited_once()
        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.lat, 18.52)
        self.assertEqual(rec_request.lon, 73.85)
        self.assertEqual(rec_request.season, "RABI")
        self.assertEqual(rec_request.budget_bucket, "B4")

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_with_farm_id_uses_farm_center_when_location_omitted(
        self, mock_scoring_engine, mock_agro_service, mock_publish
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "center": [73.0, 18.0], "crop": "Rice", "has_active_crop": True,
        })
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
            # previous_crop omitted and not on the mocked farm -> was_fallow=True, skips crop lookup
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.lat, 18.0)
        self.assertEqual(rec_request.lon, 73.0)

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    async def test_submit_without_location_and_without_farm_id_returns_400(self, mock_publish):
        from routers.v2.wizard import submit_wizard
        from fastapi import HTTPException

        req = WizardSubmitRequest(farmId=None, answers=WizardAnswersPayload(season="Rabi"))
        with self.assertRaises(HTTPException) as ctx:
            await submit_wizard(req, auth_payload={"upid": "usr_1"})
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.scoring_engine")
    async def test_kafka_publish_failure_does_not_break_response(self, mock_scoring_engine, mock_publish):
        # Review Focus: Kafka outage must not turn a working recommendation into a 500.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))
        mock_publish.side_effect = RuntimeError("MSK unreachable")

        req = WizardSubmitRequest(
            farmId=None,
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual(result.recommendations, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_submit -v`
Expected: FAIL — `ImportError: cannot import name '_map_season' from 'routers.v2.wizard'`.

- [ ] **Step 3: Append the mapping helpers, event builder, and route**

Add these imports near the top of `app/routers/v2/wizard.py`:

```python
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from models.recommendationModels import RecommendationRequest
from models.wizardModels import WizardRecommendationItem, WizardSubmitRequest, WizardSubmitResponse
from services.kafka_client import publish_event
from utils.scoringEngine import CropScoringEngine

logger = logging.getLogger("wizard_router")
scoring_engine = CropScoringEngine(engine)
```

Append at the end of `app/routers/v2/wizard.py`:

```python
_SEASON_MAP = {"kharif": "KHARIF", "rabi": "RABI", "summer": "ZAID", "annual": "LONGTERM"}
_BUDGET_MAP = {"low": "B1", "medium": "B3", "high": "B4"}


def _map_season(season: str) -> str:
    mapped = _SEASON_MAP.get(season.lower())
    if mapped is None:
        raise ValueError(f"Unrecognized season '{season}'. Expected one of: Kharif, Rabi, Summer, Annual")
    return mapped


def _map_budget(budget: str) -> str:
    mapped = _BUDGET_MAP.get(budget.lower())
    if mapped is None:
        raise ValueError(f"Unrecognized budget '{budget}'. Expected one of: low, medium, high")
    return mapped


def _map_irrigation(methods: list[str]) -> list[str]:
    return [m.upper() for m in methods]


@router.post("/wizard/submit", response_model=WizardSubmitResponse)
async def submit_wizard(payload: WizardSubmitRequest, auth_payload: dict = Depends(require_jwt)):
    """TRD §8.6/§8.7: synchronous ML recommendation + async CropGuideCompleted
    emission. The Kafka publish never blocks or fails the HTTP response
    (Review Focus) — it's fired and its result is only logged."""
    upid = auth_payload["upid"]
    answers = payload.answers
    farm = None
    if payload.farmId:
        farm = await agro_internal_service.get_farm(payload.farmId, user_id=upid)

    location = answers.location
    if location is None and farm:
        location = farm.get("location") or farm.get("center")
    if location is None:
        raise HTTPException(status_code=400, detail="answers.location is required when farmId is not provided or the farm has no boundary yet")

    try:
        season = _map_season(answers.season)
        budget_bucket = _map_budget(answers.budget) if answers.budget else "B3"
        irrigation = _map_irrigation(answers.irrigation) if answers.irrigation else (
            _map_irrigation(farm.get("irrigation") or farm.get("irrigation_methods") or []) if farm else []
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not irrigation:
        raise HTTPException(status_code=400, detail="answers.irrigation is required (at least one method)")

    previous_crop = answers.previous_crop or (farm.get("previous_crop") if farm else None)
    was_fallow = previous_crop is None

    previous_crop_family = None
    if not was_fallow:
        # Same lookup-with-fallback as the existing /recommend handler
        # (app/routers/v1/recommend.py) — exact match first, then a
        # case-insensitive scan, then a clear 400 rather than letting
        # RecommendationRequest's validator raise on a None family.
        crops_res = await crop_manager.fetch_all(limit=1, filters={"name_en": previous_crop})
        if crops_res.items:
            previous_crop_family = crops_res.items[0].family
        else:
            all_crops = await crop_manager.fetch_all(limit=0)
            matched = next((c for c in all_crops.items if c.name_en.lower() == previous_crop.lower()), None)
            if not matched:
                raise HTTPException(status_code=400, detail=f"previous_crop '{previous_crop}' not found in crop database")
            previous_crop_family = matched.family

    rec_request = RecommendationRequest(
        lat=location[1], lon=location[0],
        season=season, water_methods=irrigation, budget_bucket=budget_bucket,
        previous_crop_family=previous_crop_family, was_fallow=was_fallow, top_n=5,
    )
    response = await scoring_engine.recommend(rec_request, auto_detect_soil=True)

    recommendations = [
        WizardRecommendationItem(
            id=f"rec_{i}", crop_name=r.crop, suitability_score=round(r.suitability_pct),
            roi_estimate="High" if r.suitability_pct >= 80 else "Medium" if r.suitability_pct >= 50 else "Low",
            duration_days=None, water_requirement=None,
            reason=f"Suitability {r.suitability_pct:.0f}% for {season.title()} season.",
        )
        for i, r in enumerate(response.results)
    ]

    asyncio.create_task(_publish_crop_guide_completed(upid, payload, farm, season, budget_bucket))

    return WizardSubmitResponse(recommendations=recommendations)


async def _publish_crop_guide_completed(
    upid: str, payload: WizardSubmitRequest, farm: dict | None, season: str, budget_bucket: str
) -> None:
    """Fire-and-forget: logs on failure, never raises into the caller."""
    answers = payload.answers
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "CropGuideCompleted",
        "version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": upid,
        "farm_id": payload.farmId,
        "data": {
            "farm_name": answers.farm_name,
            "center": answers.location or (farm.get("center") if farm else None),
            "has_active_crop": (answers.has_active_crop or "").lower() == "yes",
            "crop": answers.crop,
            "sowing_date": answers.sowing_date,
            "previous_crop": answers.previous_crop,
            "irrigation_methods": answers.irrigation,
            "season": season,
            "budget": budget_bucket,
            "livestock": answers.livestock,
        },
    }
    try:
        await publish_event("crop_guide_completed", str(event), key=payload.farmId or event["event_id"])
    except Exception:
        logger.exception("Failed to publish CropGuideCompleted event_id=%s", event["event_id"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_wizard_submit -v`
Expected: `OK` (8 tests passed).

- [ ] **Step 5: Commit**

```bash
git add app/routers/v2/wizard.py tests/test_wizard_submit.py
git commit -m "feat: POST /api/v2/wizard/submit — recommendation + async CropGuideCompleted"
```

---

### Task B6: Mount v2 router in the app

**Files:**
- Modify: `E:\AGRO\ks_crop_recommendation_system_v2\app\app.py`
- Test: `E:\AGRO\ks_crop_recommendation_system_v2\tests\test_app_v2_mount.py`

**Interfaces:**
- Consumes: `router` from `app/routers/v2/__init__.py` (Task B4).
- Produces: `/api/v2/wizard/*` live on the running app.

- [ ] **Step 1: Write the failing test**

```python
"""Confirms the v2 wizard router is mounted at /api/v2."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestV2Mount(TestCase):
    def test_wizard_routes_mounted_with_correct_prefix(self):
        import app as app_module

        paths = [r.path for r in app_module.app.routes]
        self.assertIn("/api/v2/wizard/questions", paths)
        self.assertIn("/api/v2/wizard/submit", paths)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_app_v2_mount -v`
Expected: FAIL — `/api/v2/wizard/questions` not in `paths`.

- [ ] **Step 3: Mount the router**

In `app/app.py`, change:

```python
from routers import admin_router, v1_router
```

to:

```python
from routers import admin_router, v1_router, v2_router
```

In `app/routers/__init__.py`, add:

```python
from .v2 import router as v2_router
```

In `app/app.py`, add after `app.include_router(v1_router, prefix="/api/v1")`:

```python
app.include_router(v2_router, prefix="/api/v2")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd E:/AGRO/ks_crop_recommendation_system_v2 && export PYTHONPATH="E:\AGRO\ks_crop_recommendation_system_v2\SharedBackend\src;E:\AGRO\ks_crop_recommendation_system_v2\app" && .venv/Scripts/python.exe -m unittest tests.test_app_v2_mount -v`
Expected: `OK` (1 test passed).

- [ ] **Step 5: Commit**

```bash
git add app/app.py app/routers/__init__.py tests/test_app_v2_mount.py
git commit -m "feat: mount v2 wizard router at /api/v2"
```

---

## After this plan

- **Set the shared secret and MSK env vars in both repos' real `.env`/deployment config** — `INTERNAL_SERVICE_SECRET` must be the same value in both services; `MSK_BOOTSTRAP_SERVERS`/`MSK_REGION`/`MSK_ROLE_ARN` and `AGRO_INTERNAL_BASE_URL` need real values. This plan only wires the code paths — it never touches real env files.
- **End-to-end MSK round-trip is unverified.** Every test here mocks Kafka and the internal HTTP call. Once deployed, manually trigger a `/wizard/submit` and confirm Agro's consumer actually persists a farm — that's the one thing unit tests can't prove.
- **Idempotency store** (Redis `processed_events:{event_id}`) is still not implemented on the consumer — same documented gap as before this plan, now doubly relevant since the wizard is live and could actually redeliver.
- **Dead-letter queue routing** (TRD §9.3: `crop_guide_completed_dlq` after 5 unhandled failures) is not implemented — the consumer currently just logs and moves on (`except Exception: logger.exception(...)` in the existing `run()` loop, unchanged by this plan). Same class of gap as the missing idempotency store; needs a persistent failure counter, which needs the same Redis (or equivalent) infra neither gap has today.
- **`was_fallow` is inferred, not a wizard field**: `WizardAnswersPayload` has no `was_fallow` field (TRD's wizard vocabulary doesn't have one) — Task B5's `submit_wizard` infers it from `previous_crop` being absent (on the payload AND on the farm record, when one exists). If a future spec adds explicit fallow signaling, revisit that inference.
