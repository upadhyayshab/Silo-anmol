"""Telecaller presence (Feature 3.3) — the liveness source for inbound routing.

Presence is a heartbeat: the frontend pings `heartbeat` every ~20s while the
telecaller is on shift (softphone open / Available toggle on). A telecaller is
"available" for an inbound call when their last heartbeat is fresh AND their
status is 'available'. Freshness is checked at read time, so there's no
background sweep marking people offline — a missed heartbeat just ages out.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Set

import sqlalchemy as db

from managers import TelecallerStatusManager, TelecallerStatusSchema

logger = logging.getLogger(__name__)

# How long a heartbeat keeps a telecaller "available" before they age out. The
# frontend pings well inside this (~20s), so one dropped ping is tolerated.
FRESHNESS_SECONDS = 45


async def heartbeat(engine, telecaller_id: str, status: str = "available") -> None:
    """Record a presence ping: upsert this telecaller's row with last_seen_at=now.

    Single INSERT ... ON CONFLICT (telecaller_id) DO UPDATE — one round-trip and no
    read-then-write race (the prior SELECT+UPDATE was 2 queries × 300 agents × every
    20s). The unique index on telecaller_id is the conflict target. We bypass the ORM
    `before_insert` uid listener (Core stmt), so set the same prefixed uid by hand.
    """
    now = datetime.now(timezone.utc)
    # postgres in prod/dev; sqlite only on the no-DB fallback. Both dialects expose the
    # same on_conflict_do_update(index_elements=..., set_=...) API.
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert
    stmt = _insert(TelecallerStatusSchema).values(
        uid=f"telecaller_status_{uuid.uuid4()}",
        telecaller_id=telecaller_id, status=status, last_seen_at=now,
    ).on_conflict_do_update(
        index_elements=["telecaller_id"],
        set_={"status": status, "last_seen_at": now},
    )
    mgr = TelecallerStatusManager(engine)
    async with mgr.session_factory() as session:
        await session.execute(stmt)
        await session.commit()


async def available_ids(engine, window_seconds: int = FRESHNESS_SECONDS) -> Set[str]:
    """Telecaller ids with a fresh 'available' heartbeat (eligible for an inbound call)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    mgr = TelecallerStatusManager(engine)
    async with mgr.session_factory() as session:
        rows = await session.execute(
            db.select(TelecallerStatusSchema.telecaller_id).where(
                TelecallerStatusSchema.status == "available",
                TelecallerStatusSchema.last_seen_at > cutoff,
            )
        )
        return {tid for (tid,) in rows.all()}


# "Logged in and working" for lead-assignment gating: a telecaller mid-call is still on
# shift, so on_call counts here even though available_ids (inbound routing) excludes it.
# These are the only statuses the frontend heartbeat ever sends (available | on_call).
PRESENT_STATUSES = ("available", "on_call")


async def present_ids(engine, window_seconds: int = FRESHNESS_SECONDS) -> Set[str]:
    """Telecaller ids 'logged in' for lead-assignment eligibility: a fresh heartbeat with
    a working status (available OR on_call). Broader than available_ids — used to gate
    auto-assignment so new leads aren't dealt to telecallers who aren't on shift."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    mgr = TelecallerStatusManager(engine)
    async with mgr.session_factory() as session:
        rows = await session.execute(
            db.select(TelecallerStatusSchema.telecaller_id).where(
                TelecallerStatusSchema.status.in_(PRESENT_STATUSES),
                TelecallerStatusSchema.last_seen_at > cutoff,
            )
        )
        return {tid for (tid,) in rows.all()}
