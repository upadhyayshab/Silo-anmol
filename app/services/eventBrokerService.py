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
            # warning, not debug: a production deployment that forgets to set
            # BROKER_TYPE=kafka must see this loudly in its own logs, not have
            # every CropGuideCompleted event silently dropped below the
            # default log level (Review finding Important #4).
            logger.warning(f"[MEMORY BROKER] {topic}: {payload}")
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
