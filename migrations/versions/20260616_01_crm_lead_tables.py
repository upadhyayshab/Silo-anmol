"""CRM Stage 1 — lead, lead_activity and lead_assignment tables

Revision ID: 20260616_01_crm_lead_tables
Revises: 692d425f260e
Create Date: 2026-06-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260616_01_crm_lead_tables"
down_revision = "692d425f260e"
branch_labels = None
depends_on = None


lead_stage = sa.Enum(
    "New Lead", "Engaged", "Lapsed", "RTU", "FTU", "Not Qualified", "Not Reachable",
    name="lead_stage",
)
lead_source = sa.Enum(
    "Organic Search", "Referral Sites", "Direct Traffic", "Social Media",
    "Inbound Email", "Inbound Phone call", "Outbound Phone call", "Pay per Click Ads",
    "FB Lead Ads", "web visit/Login", "WhatsApp Inbound", "App sign up",
    "Gau swasth Subscriber", "add to cart", "browsed 3 pages",
    name="lead_source",
)
lead_activity_type = sa.Enum(
    "created", "assignment", "field_update", "stage_change", "note", "call_log",
    name="lead_activity_type",
)


def _base_columns():
    return [
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    # Enum types are created implicitly by the first create_table that uses them.

    # ---- leads ----
    op.create_table(
        "leads",
        *_base_columns(),
        sa.Column("first_name", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("mobile", sa.String(length=20), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address_line", sa.Text(), nullable=True),
        sa.Column("address_line_2", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("district", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("pincode", sa.String(length=10), nullable=True),
        sa.Column("country", sa.String(length=100), server_default="India", nullable=True),
        sa.Column("lead_number", sa.String(length=50), nullable=False),
        sa.Column("stage", lead_stage, server_default="New Lead", nullable=False),
        sa.Column("source", lead_source, nullable=True),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("outlet_id", sa.String(), nullable=True),
        sa.Column("lead_score", sa.Integer(), nullable=True),
        sa.Column("follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("order_value", sa.Numeric(precision=12, scale=2), server_default="0", nullable=False),
        sa.Column("do_not_call", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("do_not_sms", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("do_not_email", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("custom_fields", sa.JSON(), nullable=True),
        sa.Column("campaign_data", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.uid"]),
        sa.ForeignKeyConstraint(["outlet_id"], ["outlets.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("lead_number"),
    )
    op.create_index("ix_leads_mobile", "leads", ["mobile"])
    op.create_index("ix_leads_email", "leads", ["email"])
    op.create_index("ix_leads_district", "leads", ["district"])
    op.create_index("ix_leads_pincode", "leads", ["pincode"])
    op.create_index("ix_leads_lead_number", "leads", ["lead_number"], unique=True)
    op.create_index("ix_leads_stage", "leads", ["stage"])
    op.create_index("ix_leads_source", "leads", ["source"])
    op.create_index("ix_leads_owner_id", "leads", ["owner_id"])
    op.create_index("ix_leads_outlet_id", "leads", ["outlet_id"])
    op.create_index("ix_leads_follow_up_at", "leads", ["follow_up_at"])

    # ---- lead_activities ----
    op.create_table(
        "lead_activities",
        *_base_columns(),
        sa.Column("lead_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("activity_type", lead_activity_type, nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=50), nullable=True),
        sa.Column("from_stage", sa.String(length=50), nullable=True),
        sa.Column("to_stage", sa.String(length=50), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.uid"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_lead_activities_lead_id", "lead_activities", ["lead_id"])
    op.create_index("ix_lead_activities_activity_type", "lead_activities", ["activity_type"])

    # ---- lead_assignments ----
    op.create_table(
        "lead_assignments",
        *_base_columns(),
        sa.Column("lead_id", sa.String(), nullable=False),
        sa.Column("telecaller_id", sa.String(), nullable=False),
        sa.Column("assigned_by", sa.String(), nullable=True),
        sa.Column("reason", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.uid"]),
        sa.ForeignKeyConstraint(["telecaller_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_lead_assignments_lead_id", "lead_assignments", ["lead_id"])
    op.create_index("ix_lead_assignments_telecaller_id", "lead_assignments", ["telecaller_id"])
    op.create_index("ix_lead_assignments_is_active", "lead_assignments", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_lead_assignments_is_active", table_name="lead_assignments")
    op.drop_index("ix_lead_assignments_telecaller_id", table_name="lead_assignments")
    op.drop_index("ix_lead_assignments_lead_id", table_name="lead_assignments")
    op.drop_table("lead_assignments")

    op.drop_index("ix_lead_activities_activity_type", table_name="lead_activities")
    op.drop_index("ix_lead_activities_lead_id", table_name="lead_activities")
    op.drop_table("lead_activities")

    for ix in (
        "ix_leads_follow_up_at", "ix_leads_outlet_id", "ix_leads_owner_id",
        "ix_leads_source", "ix_leads_stage", "ix_leads_lead_number",
        "ix_leads_pincode", "ix_leads_district", "ix_leads_email", "ix_leads_mobile",
    ):
        op.drop_index(ix, table_name="leads")
    op.drop_table("leads")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        lead_activity_type.drop(bind, checkfirst=True)
        lead_source.drop(bind, checkfirst=True)
        lead_stage.drop(bind, checkfirst=True)
