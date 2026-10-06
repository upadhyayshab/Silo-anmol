"""
Read access to the cattle disease knowledge base (the disease_kb table), cached in
memory so a scan does not query the DB for the KB on every request.

The cache holds every live row (active, not deleted) in every language and is
refreshed after KB_CACHE_TTL_SECONDS, or immediately via invalidate() — call
that after writing to disease_kb so the next scan sees the change.
"""
import asyncio
import time

import sqlalchemy as db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from config import get_engine, get_settings
from managers import DiseaseKbSchema

KB_CACHE_TTL_SECONDS = 300
DEFAULT_LANG = "en"


class DiseaseKbService:
    def __init__(self):
        self._session_factory = sessionmaker(  # noqa
            bind=get_engine(get_settings().name), class_=AsyncSession, expire_on_commit=False,
        )
        self._rows: list[DiseaseKbSchema] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def _live_rows(self) -> list[DiseaseKbSchema]:
        if time.monotonic() - self._loaded_at < KB_CACHE_TTL_SECONDS:
            return self._rows
        async with self._lock:
            if time.monotonic() - self._loaded_at >= KB_CACHE_TTL_SECONDS:
                async with self._session_factory() as session:
                    result = await session.execute(
                        db.select(DiseaseKbSchema)
                        .where(DiseaseKbSchema.is_active, DiseaseKbSchema.deleted_at.is_(None))
                        .order_by(DiseaseKbSchema.disease_id)
                    )
                    self._rows = list(result.scalars())
                self._loaded_at = time.monotonic()
        return self._rows

    async def classification_rows(self) -> list[DiseaseKbSchema]:
        """The English disease rows the model classifies against."""
        return [
            row for row in await self._live_rows()
            if row.lang_code == DEFAULT_LANG
        ]

    async def localized(self, row: DiseaseKbSchema, lang_code: str) -> DiseaseKbSchema:
        """The same KB entry in ``lang_code``, falling back to the row given
        (English) until that language's rows are added."""
        for candidate in await self._live_rows():
            if candidate.kb_uid == row.kb_uid and candidate.lang_code == lang_code:
                return candidate
        return row

    async def get_by_id(self, disease_id: str, lang_code: str = DEFAULT_LANG) -> DiseaseKbSchema | None:
        rows = await self._live_rows()
        by_lang = {row.lang_code: row for row in rows if row.disease_id == disease_id}
        return by_lang.get(lang_code) or by_lang.get(DEFAULT_LANG)


disease_kb_service = DiseaseKbService()

__all__ = ["DiseaseKbService", "disease_kb_service"]
