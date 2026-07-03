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
    taluk = db.Column(db.String(100), nullable=True, index=True)
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

    # Dedup safety net: one live lead per mobile. Dedup in create_lead is a
    # check-then-insert, so concurrent creates (FB webhook + backfill firing the
    # same lead) could both miss and insert. This partial unique index makes the
    # DB reject the loser, which create_lead catches and merges instead.
    # Excludes soft-deleted rows (deleted_at set) and blank mobiles.
    # ponytail: indexes the normalized-stored mobile string; the +91/0 prefix
    # variants are still reconciled on the read side by find_duplicate.
    __table_args__ = (
        db.Index("uq_leads_mobile_active", "mobile", unique=True,
                 postgresql_where=db.text("deleted_at IS NULL AND mobile <> ''")),
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
                           sorts: list = None, limit: int = 25, offset: int = 0,
                           scope_owner_id: str = None, scope_uids: list = None,
                           fb_page_id: str = None, extra_clause=None):
        """Paginated list with optional free-text OR-search across name/mobile/email/lead_number.

        Returns (items, total). `filters` are ANDed (stage, deleted_at, etc.);
        `q` is ORed across the contact columns. Default sort: newest first.
        Sort grammar: "+field" ascending, "-field" descending.

        Row scope: when `scope_owner_id` is given, results are limited to leads that user
        owns OR (if `scope_uids` is given) any of those lead uids — this is how a telecaller
        sees leads they were granted call-access to alongside the leads they own.

        `fb_page_id`: FB leads carry their originating page inside the `campaign_data`
        JSON blob (`{"page_id": "..."}`), not a plain column, so this is a JSON-path
        filter rather than a generic `field:eq`. Uses the cross-dialect
        `campaign_data['page_id'].as_string()` comparator (compiles to `->>'page_id'`
        on Postgres) so it also works against sqlite in tests. Leads with NULL
        campaign_data simply don't match — no error.
        """
        async with self.session_factory() as session:
            base = db.select(self.Schema)
            count_q = db.select(db.func.count()).select_from(self.Schema)

            if filters:
                base = await self._filter(base, dict(filters), self.Schema)
                count_q = await self._filter(count_q, dict(filters), self.Schema)

            if fb_page_id is not None:
                fb_cond = self.Schema.campaign_data["page_id"].as_string() == fb_page_id
                base = base.where(fb_cond)
                count_q = count_q.where(fb_cond)

            # Advanced query-builder tree, pre-compiled to one boolean clause.
            if extra_clause is not None:
                base = base.where(extra_clause)
                count_q = count_q.where(extra_clause)

            if scope_owner_id is not None:
                conds = [self.Schema.owner_id == scope_owner_id]
                if scope_uids:
                    conds.append(self.Schema.uid.in_(scope_uids))
                scope_cond = db.or_(*conds)
                base = base.where(scope_cond)
                count_q = count_q.where(scope_cond)

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


# ============================================================================
# TELECALLER PRESENCE (Feature 3.3 — drives inbound routing 3.2)
# ============================================================================

class TelecallerStatusSchema(BaseSchema):
    """Live presence for inbound routing. One row per telecaller, upserted by the
    frontend heartbeat. A telecaller is "available" for an inbound call when their
    `status` is 'available' AND `last_seen_at` is fresh — that freshness check is
    done at read time (presenceService), so there's no background sweep marking
    people offline; a missed heartbeat simply ages out."""
    __tablename__ = "telecaller_status"

    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"),
                              nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="available",
                       server_default="available")   # available | on_call | away
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=True)

    telecaller = relationship("UserSchema", foreign_keys=[telecaller_id])


class TelecallerStatusManager(ERPGenericManager[TelecallerStatusSchema]):
    pass


# ============================================================================
# FACEBOOK LEAD ADS — leadgen forms catalog + field mapping (Default Mapping)
# ============================================================================

class FbLeadgenFormSchema(BaseSchema):
    """A synced Facebook LeadGen form. `status` gates ingestion (deactivate to stop)."""
    __tablename__ = "fb_leadgen_forms"

    page_id = db.Column(db.String(64), nullable=False, index=True)
    form_id = db.Column(db.String(64), nullable=False, unique=True)
    form_name = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="active",
                       server_default="active", index=True)  # active | inactive
    questions = db.Column(db.JSON, nullable=True)   # snapshot: [{name, type, label}]
    fb_created_time = db.Column(db.DateTime(timezone=True), nullable=True)
    last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class FbLeadgenFormManager(ERPGenericManager[FbLeadgenFormSchema]):
    pass


class FbFieldMappingSchema(BaseSchema):
    """One Meta-field -> lead-target mapping row.

    Holds both the global default (`scope="default"`, `form_id=None`) and per-form
    overrides (`scope="form"`). `field_kind` separates marketing/attribution fields
    from form question fields. `target=None` means "ignore" (the LSQ -Select Field-).
    """
    __tablename__ = "fb_field_mappings"

    scope = db.Column(db.String(16), nullable=False, default="default",
                      server_default="default", index=True)   # default | form
    form_id = db.Column(db.String(64), nullable=True, index=True)  # null when scope=default
    field_kind = db.Column(db.String(16), nullable=False,
                           default="marketing", server_default="marketing")  # marketing | question
    meta_field = db.Column(db.String(255), nullable=False)     # incoming field key
    target = db.Column(db.String(255), nullable=True)          # destination key; null = ignore
    target_kind = db.Column(db.String(16), nullable=False, default="campaign_data",
                            server_default="campaign_data")    # lead | campaign_data | custom
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    label = db.Column(db.String(255), nullable=True)  # ops English label; NULL = untranslated

    # Postgres treats NULLs as distinct, so a plain UNIQUE(scope, form_id, ...)
    # would NOT dedup default rows (form_id IS NULL). Two partial unique indexes
    # enforce one row per key for both default (NULL form_id) and per-form rows.
    __table_args__ = (
        db.Index("uq_fb_mapping_default", "scope", "field_kind", "meta_field",
                 unique=True, postgresql_where=db.text("form_id IS NULL")),
        db.Index("uq_fb_mapping_form", "scope", "form_id", "field_kind", "meta_field",
                 unique=True, postgresql_where=db.text("form_id IS NOT NULL")),
    )


class FbFieldMappingManager(ERPGenericManager[FbFieldMappingSchema]):
    pass


# ============================================================================
# SAVED SEGMENTS — named, reusable advanced-filter trees
# ============================================================================

class LeadSegmentSchema(BaseSchema):
    """A saved advanced-filter tree ("segment") — a named, reusable query the CRM
    can re-run. Owned by the user who created it (`owner_user_id`); `is_shared`
    exposes it to everyone. `surface` marks where it applies (e.g. the leads list,
    the report, or "both")."""
    __tablename__ = "lead_segments"

    name = db.Column(db.String(255), nullable=False)
    filter = db.Column(db.JSON, nullable=False)   # the advanced-filter tree
    owner_user_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True, index=True)
    is_shared = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    surface = db.Column(db.String(16), nullable=False, default="both", server_default="both")


class LeadSegmentManager(ERPGenericManager[LeadSegmentSchema]):
    async def list_visible(self, user_id: str):
        """Live segments visible to `user_id`: their own OR any shared segment.
        Soft-deleted rows are excluded. Newest first."""
        async with self.session_factory() as session:
            query = (
                db.select(self.Schema)
                .where(self.Schema.deleted_at.is_(None))
                .where(db.or_(self.Schema.owner_user_id == user_id,
                              self.Schema.is_shared.is_(True)))
                .order_by(self.Schema.created_at.desc())
            )
            rows = list((await session.execute(query)).unique().scalars().all())
            return rows


__all__ = [
    "LeadSchema", "LeadManager",
    "LeadActivitySchema", "LeadActivityManager",
    "LeadAssignmentSchema", "LeadAssignmentManager",
    "TelecallerStatusSchema", "TelecallerStatusManager",
    "FbLeadgenFormSchema", "FbLeadgenFormManager",
    "FbFieldMappingSchema", "FbFieldMappingManager",
    "LeadSegmentSchema", "LeadSegmentManager",
]
