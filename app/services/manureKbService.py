"""
Read access to the manure knowledge base (the manure_kb table), cached in
memory so a scan does not query the DB for the KB on every request.

The cache holds every live row (active, not deleted) in every language and is
refreshed after KB_CACHE_TTL_SECONDS, or immediately via invalidate() — call
that after writing to manure_kb so the next scan sees the change.
"""
import asyncio
import time

import sqlalchemy as db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from config import get_engine, get_settings
from managers import ManureKbSchema

KB_CACHE_TTL_SECONDS = 300
DEFAULT_LANG = "en"
FREE_SUFFIX = "_FREE"  # score-level free-tier rows, e.g. '4_FREE'


class ManureKbService:
    def __init__(self):
        self._session_factory = sessionmaker(  # noqa
            bind=get_engine(get_settings().name), class_=AsyncSession, expire_on_commit=False,
        )
        self._rows: list[ManureKbSchema] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def _live_rows(self) -> list[ManureKbSchema]:
        if time.monotonic() - self._loaded_at < KB_CACHE_TTL_SECONDS:
            return self._rows
        async with self._lock:
            if time.monotonic() - self._loaded_at >= KB_CACHE_TTL_SECONDS:
                async with self._session_factory() as session:
                    result = await session.execute(
                        db.select(ManureKbSchema)
                        .where(ManureKbSchema.is_active, ManureKbSchema.deleted_at.is_(None))
                        .order_by(ManureKbSchema.score, ManureKbSchema.sub_code)
                    )
                    self._rows = list(result.scalars())
                self._loaded_at = time.monotonic()
        return self._rows

    async def classification_rows(self) -> list[ManureKbSchema]:
        """The English sub-code rows the model classifies against (free-tier
        score rows carry no visual discriminators, so they are left out)."""
        return [
            row for row in await self._live_rows()
            if row.lang_code == DEFAULT_LANG and not row.sub_code.endswith(FREE_SUFFIX)
        ]

    async def localized(self, row: ManureKbSchema, lang_code: str) -> ManureKbSchema:
        """The same KB entry in ``lang_code``, falling back to the row given
        (English) until that language's rows are added."""
        for candidate in await self._live_rows():
            if candidate.kb_uid == row.kb_uid and candidate.lang_code == lang_code:
                return candidate
        return row

    async def free_row(self, score: int, lang_code: str) -> ManureKbSchema | None:
        """The score-level free-tier row (e.g. '4_FREE'), if the KB has one yet."""
        rows = await self._live_rows()
        sub_code = f"{score}{FREE_SUFFIX}"
        by_lang = {row.lang_code: row for row in rows if row.sub_code == sub_code}
        return by_lang.get(lang_code) or by_lang.get(DEFAULT_LANG)


manure_kb_service = ManureKbService()

__all__ = ["ManureKbService", "manure_kb_service"]
