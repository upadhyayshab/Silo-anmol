"""CRUD + analytics API for the geographic cluster hierarchy.

A cluster is a named group of districts within a state (e.g. "Central Karnataka").
Districts are stored lowercase + canonical so they line up with outlet_mappings /
customer_orders for reporting rollups.

Routes:
- GET    /clusters                       List clusters (+ district & outlet counts)
- GET    /clusters/hierarchy             state -> clusters -> districts -> outlets
- GET    /clusters/{uid}                 Cluster detail (districts + outlets + manager)
- POST   /clusters                       Create a cluster
- PUT    /clusters/{uid}                 Update a cluster
- DELETE /clusters/{uid}                 Delete a cluster (unassigns its outlets first)
- POST   /clusters/{uid}/districts       Add district(s) to a cluster
- DELETE /clusters/{uid}/districts/{d}   Remove a district from a cluster
- POST   /clusters/{uid}/outlets         Assign outlet(s) to a cluster
- DELETE /clusters/{uid}/outlets/{oid}   Unassign an outlet
- POST   /clusters/bulk                  Bulk import (replace-per-state)
"""
from typing import Any, Dict, List, Optional

import sqlalchemy as db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from config import get_settings, get_engine
from managers import (
    ClusterManager, ClusterDistrictManager, ClusterSchema, ClusterDistrictSchema,
    OutletManager, OutletSchema, UserManager,
)
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission
from utils.constants import OutletType
from utils.cluster_utils import canonical_district

settings = get_settings()
engine = get_engine(settings.name)
cluster_manager = ClusterManager(engine)
cluster_district_manager = ClusterDistrictManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/clusters", tags=["Clusters"])


# ---------------------------------------------------------------------------
# pypinindia state validation (lazy, mirrors outlet_mappings)
# ---------------------------------------------------------------------------
_pinindia = None


def _get_pinindia():
    global _pinindia
    if _pinindia is None:
        from pypinindia import PincodeData
        _pinindia = PincodeData()
    return _pinindia


def _validate_state(state: str) -> Optional[Dict[str, Any]]:
    pd = _get_pinindia()
    valid = {s.upper() for s in pd.get_states()}
    if state.upper() not in valid:
        return {
            "field": "state",
            "provided": state,
            "message": f"'{state}' is not a recognised state.",
            "suggestions": pd.suggest_states(state, n=5),
        }
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ClusterCreate(BaseModel):
    name: str = Field(..., description="Cluster name, e.g. 'Central Karnataka'")
    state: str = Field(..., description="State name, e.g. 'Karnataka'")
    code: Optional[str] = Field(None, description="Stable slug; auto-generated if omitted")
    parent_id: Optional[str] = Field(None, description="Parent cluster uid (hierarchy)")
    manager_id: Optional[str] = Field(None, description="Cluster head user uid")
    is_active: bool = True
    meta: Optional[dict] = None
    districts: Optional[List[str]] = Field(None, description="District names to attach")


class ClusterUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    parent_id: Optional[str] = None
    manager_id: Optional[str] = None
    is_active: Optional[bool] = None
    meta: Optional[dict] = None


class DistrictsPayload(BaseModel):
    districts: List[str] = Field(..., description="District names to add")


class OutletsPayload(BaseModel):
    outlet_ids: List[str] = Field(..., description="Outlet uids to assign to this cluster")


class BulkClusterRow(BaseModel):
    state: str
    cluster: str
    district: str


class BulkImportRequest(BaseModel):
    rows: List[BulkClusterRow]


def _slugify(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "cluster"


# ---------------------------------------------------------------------------
# Count helpers (avoid N+1 in list/hierarchy)
# ---------------------------------------------------------------------------
async def _district_counts() -> Dict[str, int]:
    async with engine.connect() as conn:
        q = db.select(ClusterDistrictSchema.cluster_id, db.func.count()).group_by(
            ClusterDistrictSchema.cluster_id
        )
        return {cid: n for cid, n in (await conn.execute(q)).all()}


async def _outlet_counts() -> Dict[str, int]:
    async with engine.connect() as conn:
        q = db.select(OutletSchema.cluster_id, db.func.count()).where(
            OutletSchema.cluster_id.isnot(None)
        ).group_by(OutletSchema.cluster_id)
        return {cid: n for cid, n in (await conn.execute(q)).all()}


def _cluster_dict(c: ClusterSchema, d_counts, o_counts) -> dict:
    return {
        "uid": c.uid,
        "name": c.name,
        "code": c.code,
        "state": c.state,
        "parent_id": c.parent_id,
        "manager_id": c.manager_id,
        "is_active": c.is_active,
        "meta": c.meta,
        "district_count": d_counts.get(c.uid, 0),
        "outlet_count": o_counts.get(c.uid, 0),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("")
async def list_clusters(
    state: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(200, ge=0, le=1000),
    offset: int = Query(0, ge=0),
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_READ)),
):
    filters: Dict[str, Any] = {}
    if state:
        filters["state"] = state.lower()
    if is_active is not None:
        filters["is_active"] = is_active

    clusters = await cluster_manager.fetch_all(filters=filters, limit=limit, offset=offset,
                                               sorts=["-state", "-name"])
    d_counts = await _district_counts()
    o_counts = await _outlet_counts()
    return {
        "items": [_cluster_dict(c, d_counts, o_counts) for c in clusters.items],
        "count": clusters.count,
    }


@router.get("/hierarchy")
async def cluster_hierarchy(
    state: Optional[str] = Query(None),
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_READ)),
):
    """state -> clusters -> districts (+ outlet count per cluster)."""
    filters: Dict[str, Any] = {}
    if state:
        filters["state"] = state.lower()
    clusters = await cluster_manager.fetch_all(
        filters=filters, joins=[ClusterSchema.districts], sorts=["-state", "-name"]
    )
    o_counts = await _outlet_counts()

    by_state: Dict[str, list] = {}
    for c in clusters.items:
        by_state.setdefault(c.state, []).append({
            "uid": c.uid,
            "name": c.name,
            "code": c.code,
            "manager_id": c.manager_id,
            "is_active": c.is_active,
            "outlet_count": o_counts.get(c.uid, 0),
            "districts": sorted(d.district for d in c.districts),
        })
    return {
        "states": [
            {"state": s, "clusters": sorted(cl, key=lambda x: x["name"])}
            for s, cl in sorted(by_state.items())
        ]
    }


@router.get("/{uid}")
async def get_cluster(
    uid: str,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_READ)),
):
    cluster = await cluster_manager.fetch(uid, joins=[ClusterSchema.districts, ClusterSchema.outlets])
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    manager = None
    if cluster.manager_id:
        try:
            u = await user_manager.fetch(cluster.manager_id)
            manager = {"uid": u.uid, "full_name": u.full_name, "email": u.email}
        except Exception:
            manager = None
    return {
        "uid": cluster.uid,
        "name": cluster.name,
        "code": cluster.code,
        "state": cluster.state,
        "parent_id": cluster.parent_id,
        "manager_id": cluster.manager_id,
        "manager": manager,
        "is_active": cluster.is_active,
        "meta": cluster.meta,
        "districts": sorted(d.district for d in cluster.districts),
        "outlets": [
            {"uid": o.uid, "outlet_name": o.outlet_name, "outlet_code": o.outlet_code}
            for o in cluster.outlets
        ],
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    payload: ClusterCreate,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    err = _validate_state(payload.state)
    if err:
        raise HTTPException(422, detail=err)
    if payload.manager_id:
        if not await _user_exists(payload.manager_id):
            raise HTTPException(400, "manager_id user not found")

    cluster_obj = ClusterSchema(
        name=payload.name,
        code=payload.code or _slugify(payload.name),
        state=payload.state.lower(),
        parent_id=payload.parent_id,
        manager_id=payload.manager_id,
        is_active=payload.is_active,
        meta=payload.meta,
    )
    cluster = await cluster_manager.create(cluster_obj)

    if payload.districts:
        await _attach_districts(cluster, payload.districts)

    return await get_cluster(cluster.uid)


@router.put("/{uid}")
async def update_cluster(
    uid: str,
    payload: ClusterUpdate,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    cluster = await cluster_manager.fetch(uid)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("manager_id"):
        if not await _user_exists(updates["manager_id"]):
            raise HTTPException(400, "manager_id user not found")
    await cluster_manager.update(uid, updates)
    return await get_cluster(uid)


@router.delete("/{uid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cluster(
    uid: str,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    cluster = await cluster_manager.fetch(uid)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    # Unassign outlets (FK), then memberships, then the cluster.
    outlets = await outlet_manager.fetch_all(filters={"cluster_id": uid})
    for o in outlets.items:
        await outlet_manager.update(o.uid, {"cluster_id": None})
    memberships = await cluster_district_manager.fetch_all(filters={"cluster_id": uid})
    for m in memberships.items:
        await cluster_district_manager.delete(m.uid)
    await cluster_manager.delete(uid)
    return None


@router.post("/{uid}/districts")
async def add_districts(
    uid: str,
    payload: DistrictsPayload,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    cluster = await cluster_manager.fetch(uid)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    added, skipped = await _attach_districts(cluster, payload.districts)
    return {"added": added, "skipped": skipped}


@router.delete("/{uid}/districts/{district}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_district(
    uid: str,
    district: str,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    canon = canonical_district(district)
    memberships = await cluster_district_manager.fetch_all(
        filters={"cluster_id": uid, "district": canon}
    )
    if not memberships.items:
        raise HTTPException(404, "District not in this cluster")
    for m in memberships.items:
        await cluster_district_manager.delete(m.uid)
    return None


@router.post("/{uid}/outlets")
async def assign_outlets(
    uid: str,
    payload: OutletsPayload,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    cluster = await cluster_manager.fetch(uid)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    assigned = 0
    skipped = []
    for oid in payload.outlet_ids:
        try:
            outlet = await outlet_manager.fetch(oid)
        except Exception:
            skipped.append({"outlet_id": oid, "reason": "not found"})
            continue
        # Only retail outlets join clusters; warehouses/factories are infrastructure.
        otype = getattr(outlet.outlet_type, "value", outlet.outlet_type)
        if str(otype).lower() != OutletType.OUTLET.value:
            skipped.append({"outlet_id": oid, "reason": f"outlet_type '{otype}' cannot join a cluster"})
            continue
        await outlet_manager.update(oid, {"cluster_id": uid})
        assigned += 1
    return {"assigned": assigned, "skipped": skipped}


@router.delete("/{uid}/outlets/{outlet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unassign_outlet(
    uid: str,
    outlet_id: str,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    outlet = await outlet_manager.fetch(outlet_id)
    if not outlet or outlet.cluster_id != uid:
        raise HTTPException(404, "Outlet not assigned to this cluster")
    await outlet_manager.update(outlet_id, {"cluster_id": None})
    return None


@router.post("/bulk")
async def bulk_import(
    payload: BulkImportRequest,
    _: AuthContext = Depends(require_permission(Permission.CLUSTERS_WRITE)),
):
    """Replace-per-state bulk import from State/Cluster/District rows."""
    from collections import defaultdict
    grouped: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    states = set()
    for row in payload.rows:
        s = row.state.lower()
        states.add(s)
        grouped[s][row.cluster.strip()].add(canonical_district(row.district))

    # Clear existing clusters for these states (unassign outlets first).
    for s in states:
        existing = await cluster_manager.fetch_all(filters={"state": s})
        ids = [c.uid for c in existing.items]
        if ids:
            outlets = await outlet_manager.fetch_all(filters={"cluster_id": ids})
            for o in outlets.items:
                await outlet_manager.update(o.uid, {"cluster_id": None})
            mems = await cluster_district_manager.fetch_all(filters={"cluster_id": ids})
            for m in mems.items:
                await cluster_district_manager.delete(m.uid)
            for c in existing.items:
                await cluster_manager.delete(c.uid)

    created_clusters = created_districts = 0
    for s, clusters in grouped.items():
        for name, districts in clusters.items():
            cluster = await cluster_manager.create(ClusterSchema(
                name=name, code=_slugify(name), state=s, is_active=True
            ))
            created_clusters += 1
            for d in sorted(districts):
                await cluster_district_manager.create(ClusterDistrictSchema(
                    cluster_id=cluster.uid, state=s, district=d, is_active=True
                ))
                created_districts += 1

    return {"clusters_created": created_clusters, "districts_created": created_districts}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _user_exists(uid: str) -> bool:
    try:
        return bool(await user_manager.fetch(uid))
    except Exception:
        return False


async def _attach_districts(cluster: ClusterSchema, districts: List[str]):
    """Create membership rows for each district, skipping ones already present."""
    existing = await cluster_district_manager.fetch_all(filters={"cluster_id": cluster.uid})
    have = {m.district for m in existing.items}
    added, skipped = [], []
    for raw in districts:
        canon = canonical_district(raw)
        if not canon or canon in have:
            skipped.append(raw)
            continue
        try:
            await cluster_district_manager.create(ClusterDistrictSchema(
                cluster_id=cluster.uid, state=cluster.state, district=canon, is_active=True
            ))
            have.add(canon)
            added.append(canon)
        except Exception:
            # unique (state, district) violation -> district already in another cluster
            skipped.append(raw)
    return added, skipped
