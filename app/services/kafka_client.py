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
        # message.timeout.ms = our deadline: a message that missed it is
        # dropped by the client, never sent late after the caller has already
        # kept it in event_outbox (which would deliver it twice). The connect
        # attempt ends there too: librdkafka's default keeps a hung connect
        # (an unreachable private broker) going for 30 s.
        deadline_ms = int(timeout * 1000)
        producer = make_producer({
            "client.id": "crop-rec-publisher",
            "message.timeout.ms": deadline_ms,
            "socket.connection.setup.timeout.ms": max(deadline_ms, 1000),  # librdkafka's minimum
        })
        delivery_error: Exception | None = None

        def _on_delivery(err: Any, msg: Any) -> None:
            nonlocal delivery_error
            if err is not None:
                delivery_error = KafkaException(err)

        val_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        key_bytes = key.encode("utf-8") if key is not None else None

        try:
            producer.produce(topic=topic, value=val_bytes, key=key_bytes, on_delivery=_on_delivery)
            remaining = producer.flush(timeout=timeout)
            if remaining > 0:
                # Given up: the caller keeps the event in event_outbox.
                producer.purge()
        finally:
            # The exception raised below sits in a reference cycle with the
            # executor's future, so this frame (and a producer it still held)
            # would live until the next GC pass: a client reconnecting in the
            # background, with its threads, after every failed publish.
            del producer
        if remaining > 0:
            raise KafkaException(f"Timed out flushing producer ({remaining} message(s) un-flushed)")
        if delivery_error is not None:
            raise delivery_error

    await asyncio.to_thread(_produce_sync)


__all__ = ["make_producer", "publish_event"]
