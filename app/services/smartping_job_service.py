import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import get_engine, get_settings
from managers import (
    SmartpingCampaignRegistryManager,
    SmartpingCampaignRegistrySchema,
    SmartpingMessageJobManager,
    SmartpingMessageJobSchema,
    smartping_delay_to_timedelta,
)
from utils.constants import SmartpingJobStatus
from models import (
    SmartpingCampaignRegistryCreateRequest,
    SmartpingCampaignRegistryUpdateRequest,
    SmartpingDispatchResponse,
    SmartpingEnqueueResponse,
    SmartpingEventEnqueueRequest,
    SmartpingJobResponse,
)
from services.smartping_service import SmartpingService


class SmartpingJobService:
    RETRIABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    RETRY_BACKOFF_MINUTES = [5, 15, 60, 240]

    def __init__(self):
        self.settings = get_settings()
        self.engine = get_engine(self.settings.name)
        self.registry_manager = SmartpingCampaignRegistryManager(self.engine)
        self.job_manager = SmartpingMessageJobManager(self.engine)
        self.smartping_service = SmartpingService()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _idempotency_key(
        self,
        *,
        event_key: str,
        version: int,
        business_event_ref: str,
        destination: str,
        send_at: datetime,
    ) -> str:
        raw = f"{event_key}|{version}|{business_event_ref}|{destination}|{send_at.isoformat()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _normalize_context(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return context or {}

    def _ensure_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _build_template_params(
        self,
        registry: SmartpingCampaignRegistrySchema,
        context: Dict[str, Any],
    ) -> List[str]:
        fallback = registry.params_fallback_value or {}
        template_params = []
        for key in registry.template_param_keys or []:
            if key in context and context[key] is not None:
                template_params.append(str(context[key]))
                continue

            if key in fallback and fallback[key] is not None:
                template_params.append(str(fallback[key]))
                continue

            raise ValueError(
                f"Missing SmartPing template value '{key}' for event_key '{registry.event_key}'"
            )
        return template_params

    def _build_request_payload(
        self,
        registry: SmartpingCampaignRegistrySchema,
        *,
        destination: str,
        user_name: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "campaignName": registry.campaign_name,
            "destination": destination,
            "userName": user_name,
            "templateParams": self._build_template_params(registry, context),
        }

        if registry.source:
            payload["source"] = registry.source
        if registry.media is not None:
            payload["media"] = registry.media
        if registry.tags is not None:
            payload["tags"] = registry.tags
        if registry.attributes is not None:
            payload["attributes"] = registry.attributes
        if registry.buttons is not None:
            payload["buttons"] = registry.buttons
        if registry.params_fallback_value is not None:
            payload["paramsFallbackValue"] = registry.params_fallback_value

        return payload

    def _registry_send_at(
        self,
        registry: SmartpingCampaignRegistrySchema,
        override_send_at: Optional[datetime] = None,
    ) -> datetime:
        if override_send_at:
            return self._ensure_utc(override_send_at)
        return self._now() + smartping_delay_to_timedelta(
            registry.trigger_delay_value,
            registry.trigger_delay_unit,
        )

    def _job_response(self, job: SmartpingMessageJobSchema) -> SmartpingJobResponse:
        return SmartpingJobResponse.model_validate(job)

    async def upsert_campaign_registry(
        self,
        payload: SmartpingCampaignRegistryCreateRequest | SmartpingCampaignRegistryUpdateRequest,
        *,
        event_key: str,
    ) -> SmartpingCampaignRegistrySchema:
        async with self.registry_manager.session_factory() as session:
            records = await self.registry_manager.fetch_all(
                filters={"event_key": event_key},
                session=session,
            )
            if records.items:
                existing = records.items[0]
                updates = payload.model_dump(exclude_unset=True)
                updates["event_key"] = event_key
                if isinstance(payload, SmartpingCampaignRegistryUpdateRequest) and payload.version is not None:
                    updates["version"] = payload.version
                else:
                    updates["version"] = existing.version + 1
                updated = await self.registry_manager.update(existing.uid, updates, session=session)
                await session.commit()
                # commit expires attributes; reload while still session-bound so the
                # caller's model_validate doesn't hit a DetachedInstanceError on updated_at
                await session.refresh(updated)
                return updated

            record = SmartpingCampaignRegistrySchema.model_load(
                {**payload.model_dump(), "event_key": event_key}
            )
            created = await self.registry_manager.create(record, session=session)
            await session.commit()
            await session.refresh(created)
            return created

    async def list_campaign_registries(
        self,
        *,
        business_event_key: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[SmartpingCampaignRegistrySchema]:
        filters: Dict[str, Any] = {}
        if business_event_key is not None:
            filters["business_event_key"] = business_event_key
        if is_active is not None:
            filters["is_active"] = is_active
        records = await self.registry_manager.fetch_all(
            filters=filters or None,
            sorts=["business_event_key", "trigger_delay_value", "event_key"],
        )
        return records.items

    async def get_campaign_registry(self, event_key: str) -> SmartpingCampaignRegistrySchema:
        return await self.registry_manager.fetch_one(filters={"event_key": event_key})

    async def enqueue_business_event(
        self,
        business_event_key: str,
        payload: SmartpingEventEnqueueRequest,
    ) -> SmartpingEnqueueResponse:
        registries = await self.registry_manager.fetch_active_by_business_event_key(business_event_key)
        if not registries:
            raise ValueError(f"No active SmartPing campaign registry found for '{business_event_key}'")

        created_jobs: List[SmartpingJobResponse] = []
        created_count = 0
        skipped_count = 0

        async with self.job_manager.session_factory() as session:
            for registry in registries:
                send_at = self._registry_send_at(registry, payload.send_at)
                context = self._normalize_context(payload.context)
                request_payload = self._build_request_payload(
                    registry,
                    destination=payload.destination,
                    user_name=payload.user_name,
                    context=context,
                )
                idempotency_key = self._idempotency_key(
                    event_key=registry.event_key,
                    version=registry.version,
                    business_event_ref=payload.business_event_ref,
                    destination=payload.destination,
                    send_at=send_at,
                )

                existing = await self.job_manager.get_by_idempotency_key(
                    idempotency_key,
                    session=session,
                )
                if existing is not None:
                    skipped_count += 1
                    created_jobs.append(self._job_response(existing))
                    continue

                job = SmartpingMessageJobSchema(
                    event_key=registry.event_key,
                    business_event_key=business_event_key,
                    business_event_ref=payload.business_event_ref,
                    registry_uid=registry.uid,
                    destination=payload.destination,
                    user_name=payload.user_name,
                    send_at=send_at,
                    status=SmartpingJobStatus.PENDING,
                    retry_count=0,
                    max_retries=payload.max_retries,
                    idempotency_key=idempotency_key,
                    context_payload=context,
                    request_payload=request_payload,
                )
                try:
                    created = await self.job_manager.create(job, session=session)
                    created_count += 1
                    created_jobs.append(self._job_response(created))
                except Exception:
                    existing = await self.job_manager.get_by_idempotency_key(
                        idempotency_key,
                        session=session,
                    )
                    if existing is not None:
                        skipped_count += 1
                        created_jobs.append(self._job_response(existing))
                        continue
                    raise

            await session.commit()

        return SmartpingEnqueueResponse(created=created_count, skipped=skipped_count, jobs=created_jobs)

    async def enqueue_single_event(
        self,
        event_key: str,
        payload: SmartpingEventEnqueueRequest,
    ) -> SmartpingEnqueueResponse:
        registry = await self.registry_manager.fetch_active_by_event_key(event_key)
        send_at = self._registry_send_at(registry, payload.send_at)
        context = self._normalize_context(payload.context)
        request_payload = self._build_request_payload(
            registry,
            destination=payload.destination,
            user_name=payload.user_name,
            context=context,
        )
        idempotency_key = self._idempotency_key(
            event_key=registry.event_key,
            version=registry.version,
            business_event_ref=payload.business_event_ref,
            destination=payload.destination,
            send_at=send_at,
        )

        async with self.job_manager.session_factory() as session:
            existing = await self.job_manager.get_by_idempotency_key(
                idempotency_key,
                session=session,
            )
            if existing is not None:
                return SmartpingEnqueueResponse(created=0, skipped=1, jobs=[self._job_response(existing)])

            job = SmartpingMessageJobSchema(
                event_key=registry.event_key,
                business_event_key=registry.business_event_key,
                business_event_ref=payload.business_event_ref,
                registry_uid=registry.uid,
                destination=payload.destination,
                user_name=payload.user_name,
                send_at=send_at,
                status=SmartpingJobStatus.PENDING,
                retry_count=0,
                max_retries=payload.max_retries,
                idempotency_key=idempotency_key,
                context_payload=context,
                request_payload=request_payload,
            )
            try:
                created = await self.job_manager.create(job, session=session)
                await session.commit()
                return SmartpingEnqueueResponse(created=1, jobs=[self._job_response(created)])
            except Exception:
                existing = await self.job_manager.get_by_idempotency_key(
                    idempotency_key,
                    session=session,
                )
                if existing is not None:
                    await session.commit()
                    return SmartpingEnqueueResponse(created=0, skipped=1, jobs=[self._job_response(existing)])
                raise

    def _retry_delay(self, retry_count: int) -> timedelta:
        index = min(retry_count, len(self.RETRY_BACKOFF_MINUTES) - 1)
        return timedelta(minutes=self.RETRY_BACKOFF_MINUTES[index])

    async def dispatch_due_jobs(self, limit: int = 100) -> SmartpingDispatchResponse:
        claimed_jobs = await self.job_manager.claim_due_jobs(limit=limit)
        if not claimed_jobs:
            return SmartpingDispatchResponse(claimed=0, sent=0, failed=0, retried=0, skipped=0)

        sent = 0
        failed = 0
        retried = 0
        skipped = 0
        details: List[Dict[str, Any]] = []

        for job in claimed_jobs:
            try:
                registry = await self.registry_manager.fetch(job.registry_uid)
                if not registry.is_active:
                    skipped += 1
                    await self.job_manager.update(
                        job.uid,
                        {
                            "status": SmartpingJobStatus.CANCELLED,
                            "last_error": "SmartPing campaign is inactive",
                            "locked_at": None,
                            "failed_at": self._now(),
                        },
                    )
                    details.append(
                        {
                            "job_uid": job.uid,
                            "event_key": job.event_key,
                            "status": "CANCELLED",
                            "reason": "inactive_campaign",
                        }
                    )
                    continue

                if not job.request_payload:
                    failed += 1
                    await self.job_manager.update(
                        job.uid,
                        {
                            "status": SmartpingJobStatus.FAILED,
                            "last_error": "Missing request payload",
                            "locked_at": None,
                            "failed_at": self._now(),
                        },
                    )
                    details.append(
                        {
                            "job_uid": job.uid,
                            "event_key": job.event_key,
                            "status": "FAILED",
                            "reason": "missing_request_payload",
                        }
                    )
                    continue

                result = await self.smartping_service.send_prebuilt_payload(job.request_payload)
                if not result.get("error"):
                    sent += 1
                    await self.job_manager.update(
                        job.uid,
                        {
                            "status": SmartpingJobStatus.SENT,
                            "response_payload": result,
                            "last_error": None,
                            "locked_at": None,
                            "sent_at": self._now(),
                        },
                    )
                    details.append(
                        {
                            "job_uid": job.uid,
                            "event_key": job.event_key,
                            "status": "SENT",
                        }
                    )
                    continue

                status_code = result.get("status_code")
                retryable = status_code is None or status_code in self.RETRIABLE_STATUSES
                if retryable and job.retry_count < job.max_retries:
                    retried += 1
                    next_send_at = self._now() + self._retry_delay(job.retry_count)
                    await self.job_manager.update(
                        job.uid,
                        {
                            "status": SmartpingJobStatus.PENDING,
                            "retry_count": job.retry_count + 1,
                            "send_at": next_send_at,
                            "response_payload": result,
                            "last_error": str(result.get("details") or "SmartPing request failed"),
                            "locked_at": None,
                        },
                    )
                    details.append(
                        {
                            "job_uid": job.uid,
                            "event_key": job.event_key,
                            "status": "RETRY",
                            "next_send_at": next_send_at.isoformat(),
                        }
                    )
                    continue

                failed += 1
                await self.job_manager.update(
                    job.uid,
                    {
                        "status": SmartpingJobStatus.FAILED,
                        "response_payload": result,
                        "last_error": str(result.get("details") or "SmartPing request failed"),
                        "locked_at": None,
                        "failed_at": self._now(),
                    },
                )
                details.append(
                    {
                        "job_uid": job.uid,
                        "event_key": job.event_key,
                        "status": "FAILED",
                        "retryable": retryable,
                    }
                )
            except Exception as exc:
                failed += 1
                await self.job_manager.update(
                    job.uid,
                    {
                        "status": SmartpingJobStatus.FAILED,
                        "last_error": str(exc),
                        "locked_at": None,
                        "failed_at": self._now(),
                    },
                )
                details.append(
                    {
                        "job_uid": job.uid,
                        "event_key": job.event_key,
                        "status": "FAILED",
                        "error": str(exc),
                    }
                )

        return SmartpingDispatchResponse(
            claimed=len(claimed_jobs),
            sent=sent,
            failed=failed,
            retried=retried,
            skipped=skipped,
            details=details,
        )


smartping_job_service = SmartpingJobService()
