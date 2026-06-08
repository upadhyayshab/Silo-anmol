from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional

from models import (
    ListResponse,
    SmartpingCampaignRegistryCreateRequest,
    SmartpingCampaignRegistryResponse,
    SmartpingCampaignRegistryUpdateRequest,
    SmartpingDispatchResponse,
    SmartpingEnqueueResponse,
    SmartpingEventEnqueueRequest,
    SmartpingJobResponse,
)
from services.smartping_job_service import smartping_job_service
from utils import dependencies as D

router = APIRouter(
    prefix="/smartping",
    tags=["SmartPing"],
)


def _registry_response(record) -> SmartpingCampaignRegistryResponse:
    return SmartpingCampaignRegistryResponse.model_validate(record)


def _job_response(record) -> SmartpingJobResponse:
    return SmartpingJobResponse.model_validate(record)


@router.get("/campaigns", response_model=ListResponse[SmartpingCampaignRegistryResponse])
async def list_campaigns(
    business_event_key: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
):
    records = await smartping_job_service.list_campaign_registries(
        business_event_key=business_event_key,
        is_active=is_active,
    )
    return ListResponse(
        items=[_registry_response(record) for record in records],
        count=len(records),
    )


@router.get("/campaigns/{event_key}", response_model=SmartpingCampaignRegistryResponse)
async def get_campaign(event_key: str):
    try:
        record = await smartping_job_service.get_campaign_registry(event_key)
        return _registry_response(record)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.put("/campaigns/{event_key}", response_model=SmartpingCampaignRegistryResponse)
async def upsert_campaign(event_key: str, payload: SmartpingCampaignRegistryCreateRequest):
    try:
        record = await smartping_job_service.upsert_campaign_registry(payload, event_key=event_key)
        return _registry_response(record)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.patch("/campaigns/{event_key}", response_model=SmartpingCampaignRegistryResponse)
async def update_campaign(event_key: str, payload: SmartpingCampaignRegistryUpdateRequest):
    try:
        await smartping_job_service.get_campaign_registry(event_key)
        record = await smartping_job_service.upsert_campaign_registry(payload, event_key=event_key)
        return _registry_response(record)
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message)


@router.post("/events/{business_event_key}/enqueue", response_model=SmartpingEnqueueResponse)
async def enqueue_business_event(business_event_key: str, payload: SmartpingEventEnqueueRequest):
    try:
        return await smartping_job_service.enqueue_business_event(business_event_key, payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/single-event/{event_key}/enqueue", response_model=SmartpingEnqueueResponse)
async def enqueue_single_event(event_key: str, payload: SmartpingEventEnqueueRequest):
    try:
        return await smartping_job_service.enqueue_single_event(event_key, payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/jobs/dispatch-due", response_model=SmartpingDispatchResponse)
async def dispatch_due_jobs(limit: int = Query(default=100, ge=1, le=500)):
    try:
        return await smartping_job_service.dispatch_due_jobs(limit=limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/jobs", response_model=ListResponse[SmartpingJobResponse])
async def list_jobs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    event_key: Optional[str] = Query(default=None),
    business_event_key: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    filters = {}
    if status_filter:
        filters["status"] = status_filter
    if event_key:
        filters["event_key"] = event_key
    if business_event_key:
        filters["business_event_key"] = business_event_key

    records = await smartping_job_service.job_manager.fetch_all(
        limit=limit,
        filters=filters or None,
        sorts=["-created_at"],
    )
    return ListResponse(
        items=[_job_response(record) for record in records.items],
        count=records.count,
    )
