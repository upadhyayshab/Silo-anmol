"""Native CRM data layer — Stage 1 (Data Foundation).

Three tables:
  - leads             : the core lead record
  - lead_activities   : the lead's activity timeline (notes, stage changes, calls, ...)
  - lead_assignments  : assignment history (round-robin + manual reassignments)

Schemas extend the shared `BaseSchema` (uid / created_at / updated_at / deleted_at)
and managers extend `ERPGenericManager` so they inherit the generic
create / fetch / fetch_all / update / delete + filter/sort machinery.

This module MUST be re-exported from `managers/__init__.py` so Alembic's
`from managers import *` (migrations/env.py) registers the tables on
`BaseSchema.metadata`.
"""
import sqlalchemy as db
from sqlalchemy.orm import relationship

from SharedBackend.managers import BaseSchema
from SharedBackend.managers.base import NESTED_FILTERS
from .erpManagers import ERPGenericManager
from utils.crm_enums import LeadStage, LeadActivityType, CallOutcome, AssignmentReason
from utils.crm_constants import LeadSource


def _enum_values(enum_cls):
    """Persist enum `.value` (not member name) so DB labels match the API/filters."""
    return [member.value for member in enum_cls]


# ============================================================================
# LEADS
# ============================================================================

class LeadSchema(BaseSchema):
    """A CRM lead (prospective / existing customer in the sales pipeline)."""
    __tablename__ = "leads"

    # --- Contact ---
    first_name = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(255), nullable=True)
    mobile = db.Column(db.String(20), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(255), nullable=True, index=True)
    address_line = db.Column(db.Text, nullable=True)
    address_line_2 = db.Column(db.Text, nullable=True)
    city = db.Column(db.String(100), nullable=True)
    district = db.Column(db.String(100), nullable=True, index=True)
    state = db.Column(db.String(100), nullable=True)
    pincode = db.Column(db.String(10), nullable=True, index=True)
    country = db.Column(db.String(100), nullable=True, server_default="India")

    # --- Metadata ---
    lead_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    stage = db.Column(db.Enum(LeadStage, name="lead_stage", values_callable=_enum_values),
                      nullable=False, default=LeadStage.NEW_LEAD,
                      server_default=LeadStage.NEW_LEAD.value, index=True)
    source = db.Column(db.Enum(LeadSource, name="lead_source", values_callable=_enum_values),
                       nullable=True, index=True)
    owner_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=True, index=True)
    lead_score = db.Column(db.Integer, nullable=True)

    # --- Workflow ---
    follow_up_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    last_activity_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # --- Business ---
    order_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    order_value = db.Column(db.Numeric(12, 2), nullable=False, default=0, server_default="0")

    # --- Compliance ---
    do_not_call = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    do_not_sms = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    do_not_email = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    # --- Flexible ---
    custom_fields = db.Column(db.JSON, nullable=True)   # animal-husbandry domain fields
    campaign_data = db.Column(db.JSON, nullable=True)   # UTM / campaign attribution
    notes = db.Column(db.Text, nullable=True)           # quick scratch note

    # --- Relationships ---
    owner = relationship("UserSchema", foreign_keys=[owner_id])
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])
    activities = relationship(
        "LeadActivitySchema", back_populates="lead",
        cascade="all, delete-orphan", order_by="desc(LeadActivitySchema.created_at)",
    )
    assignments = relationship(
        "LeadAssignmentSchema", back_populates="lead",
        cascade="all, delete-orphan",
    )


class LeadManager(ERPGenericManager[LeadSchema]):
    async def count_all(self, filters: NESTED_FILTERS = None) -> int:
        """Total rows matching `filters` (the real total, not page length)."""
        async with self.session_factory() as session:
            query = db.select(db.func.count()).select_from(self.Schema)
            if filters:
                query = await self._filter(query, dict(filters), self.Schema)
            result = await session.execute(query)
            return int(result.scalar_one())

    async def search_leads(self, *, q: str = None, filters: NESTED_FILTERS = None,
                           sorts: list = None, limit: int = 25, offset: int = 0):
        """Paginated list with optional free-text OR-search across name/mobile/email/lead_number.

        Returns (items, total). `filters` are ANDed (role scope, stage, etc.);
        `q` is ORed across the contact columns. Default sort: newest first.
        Sort grammar: "+field" ascending, "-field" descending.
        """
        async with self.session_factory() as session:
            base = db.select(self.Schema)
            count_q = db.select(db.func.count()).select_from(self.Schema)

            if filters:
                base = await self._filter(base, dict(filters), self.Schema)
                count_q = await self._filter(count_q, dict(filters), self.Schema)

            if q:
                like = f"%{q}%"
                cond = db.or_(
                    self.Schema.first_name.ilike(like),
                    self.Schema.last_name.ilike(like),
                    self.Schema.mobile.ilike(like),
                    self.Schema.email.ilike(like),
                    self.Schema.lead_number.ilike(like),
                )
                base = base.where(cond)
                count_q = count_q.where(cond)

            if sorts:
                for sort in sorts:
                    descending = sort.startswith("-")
                    field = sort.lstrip("+-")
                    col = getattr(self.Schema, field, None)
                    if col is not None:
                        base = base.order_by(col.desc() if descending else col.asc())
            else:
                base = base.order_by(self.Schema.created_at.desc())

            total = int((await session.execute(count_q)).scalar_one())

            base = base.offset(offset)
            if limit:
                base = base.limit(limit)
            rows = list((await session.execute(base)).unique().scalars().all())
            return rows, total


# ============================================================================
# LEAD ACTIVITIES (timeline)
# ============================================================================

class LeadActivitySchema(BaseSchema):
    """A single entry in a lead's activity timeline."""
    __tablename__ = "lead_activities"

    lead_id = db.Column(db.String, db.ForeignKey("leads.uid"), nullable=False, index=True)
    user_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)  # null = system
    activity_type = db.Column(db.Enum(LeadActivityType, name="lead_activity_type",
                                      values_callable=_enum_values),
                              nullable=False, index=True)
    body = db.Column(db.Text, nullable=True)            # note text / human description
    outcome = db.Column(db.String(50), nullable=True)   # CallOutcome value
    from_stage = db.Column(db.String(50), nullable=True)
    to_stage = db.Column(db.String(50), nullable=True)
    details = db.Column(db.JSON, nullable=True)         # structured payload (changed fields, ...)

    lead = relationship("LeadSchema", back_populates="activities", foreign_keys=[lead_id])
    user = relationship("UserSchema", foreign_keys=[user_id])


class LeadActivityManager(ERPGenericManager[LeadActivitySchema]):
    pass


# ============================================================================
# LEAD ASSIGNMENTS (history)
# ============================================================================

class LeadAssignmentSchema(BaseSchema):
    """One assignment of a lead to a telecaller. `is_active` marks the current one."""
    __tablename__ = "lead_assignments"

    lead_id = db.Column(db.String, db.ForeignKey("leads.uid"), nullable=False, index=True)
    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    assigned_by = db.Column(db.String, nullable=True)   # user uid or "system"
    reason = db.Column(db.String(50), nullable=True)    # AssignmentReason value
    is_active = db.Column(db.Boolean, nullable=False, default=True,
                          server_default=db.true(), index=True)

    lead = relationship("LeadSchema", back_populates="assignments", foreign_keys=[lead_id])
    telecaller = relationship("UserSchema", foreign_keys=[telecaller_id])


class LeadAssignmentManager(ERPGenericManager[LeadAssignmentSchema]):
    pass


__all__ = [
    "LeadSchema", "LeadManager",
    "LeadActivitySchema", "LeadActivityManager",
    "LeadAssignmentSchema", "LeadAssignmentManager",
]
