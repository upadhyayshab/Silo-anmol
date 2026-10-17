"""Replays event_outbox rows the Kafka producer couldn't reach at request
time (spec Section 3.3 Scenario 6B). Runs as a continuous background task INSIDE
this service's own process — started from app.py's lifespan as an
asyncio.create_task, not a separate script/process (explicit instruction
this session). Uses asyncio.sleep, never a blocking time.sleep, because
it shares the event loop with every FastAPI request.

Double-publish safety (claim-before-publish):
A row is only published after it has been "claimed" by flipping its
status PENDING -> PROCESSING. If `EventOutboxManager` exposes an atomic
`claim_pending(uid)` (a conditional `UPDATE ... WHERE status='PENDING'`
with a rowcount check — the only way to make this truly race-safe across
multiple service replicas each running their own replay loop), that is
used and its boolean result decides whether the claim was won. Today's
`EventOutboxManager` (Task 1) does not implement that method, so this
falls back to a plain `update()` call, whose return value still gates
the publish: GenericManager.update() raises on a missing uid and returns
the record otherwise, so a falsy/None return (as from a hardened manager
rejecting an already-claimed row) still blocks the publish. This keeps
the single in-process replay loop (which never runs two iterations
concurrently) safe today, while leaving the hook in place for a future
atomic claim_pending() to close the multi-replica gap without changing
this call site.
"""
import asyncio
import json
import logging

from managers import EventOutboxManager
from services.kafka_client import publish_event

logger = logging.getLogger("event_outbox_replayer")

MAX_RETRIES = 5


async def _claim(outbox_manager: EventOutboxManager, row, session) -> bool:
    """Atomically flip PENDING -> PROCESSING so two replayer runs (or this
    one racing a fresh failed-publish write) can't both send the same row.

    Deliberately checks `asyncio.iscoroutinefunction(claim_pending)` rather
    than `hasattr(outbox_manager, "claim_pending")`: a bare `MagicMock()`
    always reports `hasattr(...) == True` for any attribute (it auto-vivifies
    one on access), which would make a hasattr check take the claim_pending
    branch even when a test never configured it as awaitable, crashing on
    `await` of a non-coroutine. Checking it's actually an async callable
    works for both real managers (attribute genuinely absent -> None ->
    False) and test doubles (only an explicitly-configured AsyncMock passes).
    """
    claim_pending = getattr(outbox_manager, "claim_pending", None)
    if asyncio.iscoroutinefunction(claim_pending):
        return bool(await claim_pending(row.uid))
    result = await outbox_manager.update(uid=row.uid, updates={"status": "PROCESSING"}, session=session)
    return bool(result)


async def replay_pending_events(outbox_manager: EventOutboxManager, session, limit: int = 50) -> int:
    pending = await outbox_manager.fetch_all(filters={"status": "PENDING"}, limit=limit, session=session)
    replayed = 0

    for row in pending.items:
        claimed = await _claim(outbox_manager, row, session)
        if not claimed:
            continue

        try:
            await publish_event(row.topic, json.dumps(row.payload), key=row.event_key)
            await outbox_manager.update(uid=row.uid, updates={"status": "COMPLETED"}, session=session)
            # Commit per-row, not once per up-to-50-row batch: update() above only
            # flushes. If asyncio.CancelledError (an ordinary SIGTERM shutdown, not
            # just a crash) arrives while a later row in this same batch is still
            # awaiting publish_event, it propagates out of this function before any
            # batch-level commit runs, and an uncommitted flush is discarded on
            # session close -- silently reverting this row to PENDING even though
            # publish_event already succeeded, causing a real double-publish on the
            # next process start. Committing immediately after each row reaches a
            # terminal state closes that window down to "mid-publish-call-for-this-
            # exact-row", the inherent, accepted limit of any non-2PC outbox.
            await session.commit()
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
            await session.commit()

    return replayed


async def run_replay_loop(outbox_manager: EventOutboxManager, poll_interval_seconds: int = 60) -> None:
    """Runs until cancelled (app.py cancels this task on shutdown). Each
    iteration opens its own session so a single failed commit doesn't wedge
    the loop for the rest of the process's lifetime.

    No commit happens here: replay_pending_events commits after each row's
    terminal update, not once for the whole batch, so a CancelledError
    arriving mid-batch can only discard the uncommitted claim-flip of the
    row currently in flight (which reverts to its original PENDING state,
    safe to re-claim) -- never a row already published and flushed to
    COMPLETED/FAILED/retry-PENDING by an earlier iteration of this loop."""
    while True:
        try:
            async with outbox_manager.session_factory() as session:
                count = await replay_pending_events(outbox_manager, session, limit=50)
            if count:
                logger.info(f"Replayed {count} outbox event(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Outbox replay loop iteration failed: {exc}")
        await asyncio.sleep(poll_interval_seconds)


__all__ = ["replay_pending_events", "run_replay_loop"]
