"""Facebook Lead Ads webhook (Feature 1 — inbound lead ingestion).

Mounted under /api/v1, so the callback URL to register in the Meta App is:
    https://<host>/api/v1/webhooks/facebook

GET  : Meta subscription verification — echoes hub.challenge when the token matches.
POST : leadgen notifications — HMAC-verified, then each lead is fetched + created
       in a background task so we can return 200 fast (Meta retries on slow/non-2xx).
"""
import logging

from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Request, Response, HTTPException, Query, Depends, Body
from pydantic import BaseModel

from config import get_settings, get_engine
from managers import (
    FacebookPageManager,
    FbFieldMappingManager, FbFieldMappingSchema, FbLeadgenFormManager,
)
from services import facebook_leads, facebook_service, facebook_mapping
from utils.auth import require_roles
from utils.constants import UserRole

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/webhooks/facebook", tags=["CRM - Facebook Lead Ads"])


@router.get("")
async def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you click 'Verify and Save' on the webhook."""
    if mode == "subscribe" and token and token == settings.fb_verify_token:
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
async def receive(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not facebook_leads.verify_signature(settings.fb_app_secret, raw, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    if payload.get("object") != "page":
        return {"status": "ignored"}

    queued = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            leadgen_id = value.get("leadgen_id")
            page_id = value.get("page_id") or entry.get("id")
            if leadgen_id:
                background_tasks.add_task(
                    facebook_leads.ingest_leadgen, engine, page_id, leadgen_id)
                queued += 1

    logger.info(f"[fb] webhook accepted, queued {queued} leadgen event(s)")
    return {"status": "ok", "queued": queued}


# --------------------------------------------------------------------------
# Admin: connect/sync pages + per-page routing (SUPER_ADMIN/ADMIN only)
# --------------------------------------------------------------------------

pages_router = APIRouter(prefix="/facebook/pages", tags=["CRM - Facebook Pages"])

ADMIN = (UserRole.SUPER_ADMIN, UserRole.ADMIN)


class PagePatch(BaseModel):
    routing_state: str | None = None
    notes: str | None = None


@pages_router.post("/sync")
async def sync(_: str = Depends(require_roles(*ADMIN))):
    """Discover all business pages and subscribe each to the leadgen webhook.

    Does NOT backfill — new pages come back in `new`; pull each one's history
    explicitly via POST /facebook/pages/{page_id}/backfill.
    """
    return await facebook_service.sync_pages(engine)


@pages_router.get("")
async def list_pages(_: str = Depends(require_roles(*ADMIN))):
    rows = await FacebookPageManager(engine).fetch_all()
    return [r.model_dump() for r in rows.items]


@pages_router.patch("/{page_id}")
async def update_page(page_id: str, patch: PagePatch, _: str = Depends(require_roles(*ADMIN))):
    rows = await FacebookPageManager(engine).fetch_all(filters={"page_id": page_id})
    if not rows.items:
        raise HTTPException(status_code=404, detail="Page not found")
    changes = {k: v for k, v in patch.model_dump().items() if v is not None}
    return await FacebookPageManager(engine).update(rows.items[0].uid, changes)


@pages_router.post("/{page_id}/backfill")
async def backfill(page_id: str, background_tasks: BackgroundTasks,
                   _: str = Depends(require_roles(*ADMIN))):
    background_tasks.add_task(facebook_service.backfill_page, engine, page_id)
    return {"status": "backfill started", "page_id": page_id}


# --------------------------------------------------------------------------
# Default Mapping + per-form mapping (LSQ-style) — SUPER_ADMIN/ADMIN only
# --------------------------------------------------------------------------

mappings_router = APIRouter(prefix="/facebook/mappings", tags=["CRM - Facebook Mapping"])
forms_router = APIRouter(prefix="/facebook/forms", tags=["CRM - Facebook Forms"])


class MappingItem(BaseModel):
    field_kind: Literal["marketing", "question"] = "marketing"
    meta_field: str
    target: Optional[str] = None           # null = ignore (-Select Field-)
    target_kind: Literal["lead", "campaign_data", "custom"] = "campaign_data"
    is_active: bool = True
    label: Optional[str] = None            # ops English label (questions); shown on the lead


class MappingPut(BaseModel):
    items: List[MappingItem]


class FormPatch(BaseModel):
    status: Optional[str] = None           # active | inactive
    notes: Optional[str] = None


def _row_view(r) -> dict:
    # Marketing fields have a built-in friendly label; question labels are ops-set
    # (None until translated, which the UI flags as pending).
    label = r.label or (facebook_mapping.LABELS.get(r.meta_field, r.meta_field)
                        if r.field_kind == "marketing" else None)
    return {"uid": r.uid, "field_kind": r.field_kind, "meta_field": r.meta_field,
            "label": label, "target": r.target,
            "target_kind": r.target_kind, "is_active": r.is_active}


async def _upsert_mapping(scope: str, form_id: Optional[str], items: List[MappingItem]) -> None:
    mgr = FbFieldMappingManager(engine)
    for it in items:
        f = {"scope": scope, "field_kind": it.field_kind, "meta_field": it.meta_field}
        if form_id:
            f["form_id"] = form_id
        existing = await mgr.fetch_all(filters=f, limit=1)
        data = {"target": it.target, "target_kind": it.target_kind,
                "is_active": it.is_active, "label": it.label}
        if existing.items:
            await mgr.update(existing.items[0].uid, data)
        else:
            await mgr.create(FbFieldMappingSchema(
                scope=scope, form_id=form_id,
                field_kind=it.field_kind, meta_field=it.meta_field, **data))
    facebook_mapping.clear_cache()


async def _default_payload() -> dict:
    await facebook_mapping.seed_default_mapping(engine)
    rows = await FbFieldMappingManager(engine).fetch_all(filters={"scope": "default"}, limit=500)
    items = [_row_view(r) for r in rows.items]
    return {"marketing": [i for i in items if i["field_kind"] == "marketing"],
            "question": [i for i in items if i["field_kind"] == "question"]}


@mappings_router.get("/default")
async def get_default_mapping(_: str = Depends(require_roles(*ADMIN))):
    """The global Default Mapping (seeded on first read)."""
    return await _default_payload()


@mappings_router.put("/default")
async def put_default_mapping(body: MappingPut, _: str = Depends(require_roles(*ADMIN))):
    """Upsert global default mapping rows (by field_kind + meta_field)."""
    await _upsert_mapping("default", None, body.items)
    return await _default_payload()


@forms_router.get("")
async def list_forms(page_id: Optional[str] = Query(None), _: str = Depends(require_roles(*ADMIN))):
    f = {"page_id": page_id} if page_id else {}
    rows = await FbLeadgenFormManager(engine).fetch_all(filters=f, limit=500)
    return [r.model_dump() for r in rows.items]


@forms_router.post("/sync")
async def sync_forms(page_id: str = Query(...), _: str = Depends(require_roles(*ADMIN))):
    """Pull a page's leadgen forms + questions and upsert them."""
    return await facebook_service.sync_forms(engine, page_id)


@forms_router.patch("/{form_id}")
async def patch_form(form_id: str, patch: FormPatch, _: str = Depends(require_roles(*ADMIN))):
    """Activate/deactivate a form (ingestion gate) or set notes."""
    mgr = FbLeadgenFormManager(engine)
    rows = await mgr.fetch_all(filters={"form_id": form_id})
    if not rows.items:
        raise HTTPException(status_code=404, detail="Form not found")
    changes = {k: v for k, v in patch.model_dump().items() if v is not None}
    if changes.get("status") and changes["status"] not in ("active", "inactive"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'inactive'")
    res = await mgr.update(rows.items[0].uid, changes)
    facebook_mapping.clear_cache()
    return res.model_dump()


async def _form_mapping_payload(form_id: str) -> dict:
    await facebook_mapping.seed_default_mapping(engine)
    mgr = FbFieldMappingManager(engine)
    defaults = await mgr.fetch_all(filters={"scope": "default"}, limit=500)
    forms = await mgr.fetch_all(filters={"scope": "form", "form_id": form_id}, limit=500)

    eff: dict = {}
    for r in defaults.items:
        v = _row_view(r); v["source"] = "default"
        eff[(r.field_kind, r.meta_field)] = v
    for r in forms.items:                          # form rows overlay defaults
        v = _row_view(r); v["source"] = "form"
        eff[(r.field_kind, r.meta_field)] = v
    items = list(eff.values())

    form_row = await facebook_mapping._form_row(engine, form_id)
    # Merge the form's actual questions so untranslated ones surface (pending=True),
    # even when they have no mapping row yet.
    questions = facebook_mapping.merge_questions(
        form_row.questions if form_row else None,
        {i["meta_field"]: i for i in items if i["field_kind"] == "question"})
    return {
        "form": form_row.model_dump() if form_row else {"form_id": form_id},
        "marketing": [i for i in items if i["field_kind"] == "marketing"],
        "question": questions,
    }


@forms_router.get("/{form_id}/mapping")
async def get_form_mapping(form_id: str, _: str = Depends(require_roles(*ADMIN))):
    """Effective mapping for a form (default overlaid by per-form overrides) + questions."""
    return await _form_mapping_payload(form_id)


@forms_router.put("/{form_id}/mapping")
async def put_form_mapping(form_id: str, body: MappingPut, _: str = Depends(require_roles(*ADMIN))):
    """Upsert per-form mapping overrides (questions + marketing)."""
    await _upsert_mapping("form", form_id, body.items)
    return await _form_mapping_payload(form_id)


@forms_router.post("/{form_id}/test")
async def test_lead(form_id: str, body: dict = Body(...), _: str = Depends(require_roles(*ADMIN))):
    """Test Lead: run a sample leadgen object through the mapping WITHOUT saving.

    Body is a leadgen-like object, e.g.
    `{"field_data":[{"name":"phone_number","values":["98765..."]}], "campaign_name":"X"}`.
    """
    lead_json = {**body, "form_id": form_id}
    payload = await facebook_mapping.build_lead_request(engine, lead_json)
    return {"would_create": payload.model_dump(exclude_none=True)}


# --------------------------------------------------------------------------
# Pending translations — the ops flag/badge for untranslated questions
# --------------------------------------------------------------------------

translations_router = APIRouter(prefix="/facebook/translations", tags=["CRM - Facebook Translations"])


@translations_router.get("/pending")
async def pending_translations(_: str = Depends(require_roles(*ADMIN))):
    """Questions across active forms with no English label yet.

    Ops translate each by PUTting a per-form mapping with a `label`
    (POST /facebook/forms/{form_id}/mapping).
    """
    return await facebook_mapping.pending_translations(engine)
