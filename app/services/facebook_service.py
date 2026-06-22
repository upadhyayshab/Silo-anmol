"""Multi-page Facebook Lead Ads: per-page tokens (derived on demand, never stored),
page discovery + leadgen subscription, and historical backfill.

The only stored credential is the System User token (settings.fb_page_access_token,
which falls back to FB_SYSTEM_USER_TOKEN). It mints per-page tokens at runtime.

ponytail: page tokens live only in an in-process dict with a TTL — lost on
restart and re-derived lazily; no token ever touches the DB.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from config import get_settings, get_engine
from managers import FacebookPageManager, FacebookPageSchema

logger = logging.getLogger(__name__)
settings = get_settings()

GRAPH = "https://graph.facebook.com"
_TOKEN_TTL = 6 * 3600  # seconds
_token_cache: Dict[str, Tuple[str, float]] = {}  # page_id -> (token, monotonic_ts)


def _v() -> str:
    return settings.fb_graph_version


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{GRAPH}/{_v()}/{path}", params=params)
    r.raise_for_status()
    return r.json()


async def get_page_token(page_id: str) -> Optional[str]:
    """Derive (and briefly cache) a page access token from the System User token."""
    hit = _token_cache.get(page_id)
    if hit and (time.monotonic() - hit[1]) < _TOKEN_TTL:
        return hit[0]
    async with httpx.AsyncClient(timeout=15) as client:
        data = await _get(client, page_id, {
            "fields": "access_token",
            "access_token": settings.fb_page_access_token,
        })
    token = data.get("access_token")
    if token:
        _token_cache[page_id] = (token, time.monotonic())
    return token


async def list_owned_pages() -> List[dict]:
    """All pages under the business (paginated)."""
    out: List[dict] = []
    after = None
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            params = {"fields": "id,name", "limit": 100,
                      "access_token": settings.fb_page_access_token}
            if after:
                params["after"] = after
            data = await _get(client, f"{settings.fb_business_id}/owned_pages", params)
            rows = data.get("data", [])
            out.extend(rows)
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not after or not rows:
                break
    return out


async def _subscribe_leadgen(page_id: str, page_token: str) -> bool:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{_v()}/{page_id}/subscribed_apps",
            params={"subscribed_fields": "leadgen", "access_token": page_token},
        )
        r.raise_for_status()
        return bool(r.json().get("success"))


async def _row_for(mgr: FacebookPageManager, page_id: str) -> Optional[FacebookPageSchema]:
    rows = await mgr.fetch_all(filters={"page_id": page_id})
    return rows.items[0] if rows.items else None


async def sync_pages(engine) -> dict:
    """Discover every page, upsert it, and subscribe it to the leadgen webhook.

    Does NOT backfill. A newly-discovered page is recorded with
    ``last_backfill_at = NULL`` so an admin can review and pull its history
    explicitly via ``backfill_page``. Returns a summary (``new`` lists the
    page ids that were seen for the first time and may want a backfill).
    """
    mgr = FacebookPageManager(engine)
    now = datetime.now(timezone.utc)
    pages = await list_owned_pages()
    new, subscribed, failed = [], 0, []

    for p in pages:
        pid, name = p["id"], p.get("name")
        ok = False
        try:
            token = await get_page_token(pid)
            ok = await _subscribe_leadgen(pid, token) if token else False
        except Exception as e:
            logger.error(f"[fb] subscribe failed for page {pid}: {e}")
            failed.append(pid)

        row = await _row_for(mgr, pid)
        if row:
            await mgr.update(row.uid, {"page_name": name, "is_subscribed": ok,
                                      "last_synced_at": now})
        else:
            new.append(pid)
            await mgr.create(FacebookPageSchema(
                page_id=pid, page_name=name, is_subscribed=ok, last_synced_at=now))
        if ok:
            subscribed += 1

    logger.info(f"[fb] sync: {len(pages)} pages, {len(new)} new, {subscribed} subscribed, "
                f"{len(failed)} failed")
    return {"total": len(pages), "new": new, "subscribed": subscribed, "failed": failed}


async def backfill_page(engine, page_id: str) -> int:
    """Ingest existing leads for all of a page's forms. Returns lead count seen."""
    from services import facebook_leads, facebook_mapping  # lazy: avoid import cycle
    token = await get_page_token(page_id)
    if not token:
        return 0
    seen = 0
    lead_fields = facebook_mapping.GRAPH_LEAD_FIELDS
    async with httpx.AsyncClient(timeout=30) as client:
        # ponytail: first 100 forms only; a page with >100 lead forms is unheard of.
        forms = await _get(client, f"{page_id}/leadgen_forms",
                           {"access_token": token, "limit": 100})
        for f in forms.get("data", []):
            fid, after = f["id"], None
            while True:
                params = {"access_token": token, "limit": 100, "fields": lead_fields}
                if after:
                    params["after"] = after
                leads = await _get(client, f"{fid}/leads", params)
                rows = leads.get("data", [])
                for lj in rows:
                    lj.setdefault("form_id", fid)
                    try:
                        lead, _ = await facebook_leads.create_from_lead_json(engine, page_id, lj)
                        if lead is not None:                 # skipped (inactive form) -> don't count
                            seen += 1
                    except Exception as e:
                        logger.error(f"[fb] backfill lead {lj.get('id')} failed: {e}")
                after = leads.get("paging", {}).get("cursors", {}).get("after")
                if not after or not rows:
                    break

    row = await _row_for(FacebookPageManager(engine), page_id)
    if row:
        await FacebookPageManager(engine).update(
            row.uid, {"last_backfill_at": datetime.now(timezone.utc)})
    logger.info(f"[fb] backfilled {seen} leads for page {page_id}")
    return seen


def _parse_dt(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


async def sync_forms(engine, page_id: str) -> dict:
    """Pull a page's leadgen forms (+ their questions) and upsert fb_leadgen_forms.

    New forms default to status="active". Existing forms keep their admin-set
    status (activate/deactivate is preserved). Returns a summary.
    """
    from managers import FbLeadgenFormManager, FbLeadgenFormSchema
    token = await get_page_token(page_id)
    if not token:
        return {"page_id": page_id, "error": "no token", "synced": 0, "new": 0}

    mgr = FbLeadgenFormManager(engine)
    now = datetime.now(timezone.utc)
    synced, new = 0, 0
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _get(client, f"{page_id}/leadgen_forms",
                          {"access_token": token, "limit": 100,
                           "fields": "id,name,status,questions,created_time"})

    for f in data.get("data", []):
        fid = f.get("id")
        if not fid:
            continue
        questions = [
            {"name": q.get("key") or q.get("name"), "type": q.get("type"),
             "label": q.get("label")}
            for q in (f.get("questions") or [])
        ]
        common = {"page_id": page_id, "form_name": f.get("name"),
                  "questions": questions, "last_synced_at": now}
        rows = await mgr.fetch_all(filters={"form_id": fid})
        if rows.items:
            await mgr.update(rows.items[0].uid, common)
        else:
            new += 1
            await mgr.create(FbLeadgenFormSchema(
                form_id=fid, status="active",
                fb_created_time=_parse_dt(f.get("created_time")), **common))
        synced += 1

    logger.info(f"[fb] synced {synced} forms for page {page_id} ({new} new)")
    return {"page_id": page_id, "synced": synced, "new": new}


async def nightly_sync() -> None:
    """Scheduler entrypoint (no args): re-discover + subscribe pages. No backfill —
    new pages are surfaced (last_backfill_at = NULL) for an admin to pull explicitly."""
    await sync_pages(get_engine(settings.name))
