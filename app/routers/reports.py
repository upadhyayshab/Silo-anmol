"""
Reports endpoint — mounted at the app root alongside /manure/report and /disease/report.

  - GET /reports — fetches unified reports history for a user, with filters:
      - x-upid (Header, required): User ID
      - cow_id (Query, optional): Filter by cow ID
      - scan_type (Query, default 'all'): 'all', 'disease_diagnosis', or 'cow_dung'
      - limit (Query, default 50): Number of records
      - offset (Query, default 0): Records to skip
  - GET /reports/{report_id} — fetches a single report by ID for the user.
"""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from models.reportModels import ReportsListResponse, ScanType, UnifiedReportItem
from services.reportService import report_service

router = APIRouter()


@router.get(
    "/reports",
    response_model=ReportsListResponse,
    summary="Get unified reports history",
    description=(
        "Returns reports from both disease screening (`disease_reports_v1`) and cow dung/manure "
        "analysis (`manure_reports_v1`) sorted in descending order of creation time. "
        "Supports filtering by cow_id and scan_type ('all', 'disease_diagnosis', 'cow_dung')."
    ),
)
async def get_reports(
    x_upid: str = Header(..., alias="x-upid", description="User ID"),
    cow_id: Optional[str] = Query(None, description="Optional cow ID to filter reports"),
    scan_type: ScanType = Query("all", description="Type of scan: 'all', 'disease_diagnosis', or 'cow_dung'"),
    limit: int = Query(50, ge=1, le=100, description="Max reports to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    user_id = x_upid.strip()
    clean_cow_id = (cow_id or "").strip() or None

    try:
        res = await report_service.get_reports(
            user_id=user_id,
            cow_id=clean_cow_id,
            scan_type=scan_type,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(content=jsonable_encoder(res, exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch reports: {e}")


@router.get(
    "/reports/{report_id}",
    response_model=UnifiedReportItem,
    summary="Get single report details",
    description="Fetches a specific disease or cow dung report by report_id for the given user.",
)
async def get_report_by_id(
    report_id: str,
    x_upid: str = Header(..., alias="x-upid", description="User ID"),
):
    user_id = x_upid.strip()
    report = await report_service.get_report_by_id(report_id=report_id, user_id=user_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    return JSONResponse(content=jsonable_encoder(report, exclude_unset=True))


__all__ = ["router"]
