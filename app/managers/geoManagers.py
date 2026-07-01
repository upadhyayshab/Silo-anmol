"""Geographic cluster hierarchy — data layer.

Two tables form a reusable, analytics-friendly geography layer that sits **above**
the existing district-level geography (`outlet_mappings`, `customer_orders`, `leads`):

  - clusters           : a named group of districts within a state (e.g. "Central
                         Karnataka"). Hierarchy-capable (`parent_id`) and ownable
                         (`manager_id`) so it can act as an operational layer.
  - cluster_districts  : district -> cluster membership. Because every order / lead /
                         outlet_mapping row already carries a `(state, district)`, a
                         join against this table rolls *all* historical + future data
                         up to a cluster with no backfill.

Schemas extend the shared `BaseSchema` (uid / created_at / updated_at / deleted_at) and
managers extend `ERPGenericManager`, inheriting the generic create / fetch / fetch_all /
update / delete + filter/sort machinery.

This module MUST be re-exported from `managers/__init__.py` so Alembic's
`from managers import *` (migrations/env.py) registers the tables on
`BaseSchema.metadata`, and so the string-based relationships below resolve.

District values are stored **lowercase + canonical**, matching the convention already
used by `outlet_mappings` (see `utils/cluster_utils.py`), so the analytics join lines up.
"""
import sqlalchemy as db
from sqlalchemy.orm import relationship

from SharedBackend.managers import BaseSchema
from .erpManagers import ERPGenericManager


# ============================================================================
# CLUSTERS
# ============================================================================

class ClusterSchema(BaseSchema):
    """A named group of districts within a state (territory node)."""
    __tablename__ = "clusters"
    __table_args__ = (
        db.UniqueConstraint("state", "name", name="uq_clusters_state_name"),
    )

    name = db.Column(db.String(150), nullable=False)
    # Stable machine slug, e.g. "ka-central". Unique across states.
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    state = db.Column(db.String(100), nullable=False, index=True)
    # Self-FK for future multi-level hierarchy (Zone -> Cluster -> sub-cluster).
    # Column only for now; the Python relationship is intentionally omitted to keep
    # the auto-generated pydantic models simple — add it when sub-levels are needed.
    parent_id = db.Column(db.String, db.ForeignKey("clusters.uid"), nullable=True, index=True)
    # Cluster head (operational hierarchy). Backfilled / set via admin.
    manager_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    # Free-form extensibility (targets, colour, notes, ...) without schema churn.
    meta = db.Column(db.JSON, nullable=True)

    # Relationships
    manager = relationship("UserSchema", foreign_keys=[manager_id])
    districts = relationship(
        "ClusterDistrictSchema",
        back_populates="cluster",
        cascade="all, delete-orphan",
    )
    outlets = relationship(
        "OutletSchema",
        back_populates="cluster",
        foreign_keys="OutletSchema.cluster_id",
    )


class ClusterManager(ERPGenericManager[ClusterSchema]):
    pass


# ============================================================================
# CLUSTER <-> DISTRICT MEMBERSHIP
# ============================================================================

class ClusterDistrictSchema(BaseSchema):
    """Maps a district to exactly one cluster (the analytics backbone)."""
    __tablename__ = "cluster_districts"
    __table_args__ = (
        db.UniqueConstraint("state", "district", name="uq_cluster_districts_state_district"),
    )

    cluster_id = db.Column(db.String, db.ForeignKey("clusters.uid"), nullable=False, index=True)
    state = db.Column(db.String(100), nullable=False, index=True)
    # Lowercase canonical district name — MUST match the value stored in
    # outlet_mappings / customer_orders so cluster rollups line up.
    district = db.Column(db.String(100), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    cluster = relationship("ClusterSchema", back_populates="districts", foreign_keys=[cluster_id])


class ClusterDistrictManager(ERPGenericManager[ClusterDistrictSchema]):
    pass


# ============================================================================
# PINCODE REFERENCE (India Post / GeoNames extract — telecaller geo auto-fill)
# ============================================================================

class PincodeSchema(BaseSchema):
    """A single (pincode -> state/district/taluk) row from the GeoNames India
    extract. One pincode has many rows (multiple talukas / boundary districts);
    callers DISTINCT on read. Seeded by scripts/seed/seed_in_pincodes.py."""
    __tablename__ = "pincodes"

    pincode = db.Column(db.String(10), nullable=False, index=True)
    state = db.Column(db.String(100), nullable=False)
    district = db.Column(db.String(100), nullable=True)
    taluk = db.Column(db.String(100), nullable=True)


class PincodeManager(ERPGenericManager[PincodeSchema]):
    pass


__all__ = [
    "ClusterSchema",
    "ClusterManager",
    "ClusterDistrictSchema",
    "ClusterDistrictManager",
    "PincodeSchema",
    "PincodeManager",
]
