"""smartping drip campaigns

Revision ID: 20260607_01_smartping_drips
Revises: None
Create Date: 2026-06-07 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260607_01_smartping_drips"
down_revision = None
branch_labels = None
depends_on = None


smartping_job_status = sa.Enum(
    "PENDING",
    "LOCKED",
    "SENT",
    "FAILED",
    "CANCELLED",
    name="smartping_job_status",
)


def upgrade() -> None:
    op.create_table(
        "smartping_campaign_registry",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("business_event_key", sa.String(length=255), nullable=False),
        sa.Column("campaign_name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default=sa.text("'smartping'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("trigger_delay_value", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("trigger_delay_unit", sa.String(length=16), nullable=False, server_default=sa.text("'minutes'")),
        sa.Column("template_param_keys", sa.JSON(), nullable=False),
        sa.Column("media", sa.JSON(), nullable=True),
        sa.Column("buttons", sa.JSON(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("params_fallback_value", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index(
        "ix_smartping_campaign_registry_event_key",
        "smartping_campaign_registry",
        ["event_key"],
        unique=True,
    )
    op.create_index(
        "ix_smartping_campaign_registry_business_event_key",
        "smartping_campaign_registry",
        ["business_event_key"],
        unique=False,
    )
    op.create_index(
        "ix_smartping_campaign_registry_is_active",
        "smartping_campaign_registry",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "smartping_message_jobs",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("business_event_key", sa.String(length=255), nullable=False),
        sa.Column("business_event_ref", sa.String(length=255), nullable=False),
        sa.Column("registry_uid", sa.String(), nullable=False),
        sa.Column("destination", sa.String(length=32), nullable=False),
        sa.Column("user_name", sa.String(length=255), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", smartping_job_status, nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("context_payload", sa.JSON(), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["registry_uid"], ["smartping_campaign_registry.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_smartping_message_jobs_event_key", "smartping_message_jobs", ["event_key"], unique=False)
    op.create_index("ix_smartping_message_jobs_business_event_key", "smartping_message_jobs", ["business_event_key"], unique=False)
    op.create_index("ix_smartping_message_jobs_send_at", "smartping_message_jobs", ["send_at"], unique=False)
    op.create_index("ix_smartping_message_jobs_status", "smartping_message_jobs", ["status"], unique=False)
    op.create_index("ix_smartping_message_jobs_idempotency_key", "smartping_message_jobs", ["idempotency_key"], unique=True)

    registry_seed = sa.table(
        "smartping_campaign_registry",
        sa.column("uid", sa.String()),
        sa.column("event_key", sa.String()),
        sa.column("business_event_key", sa.String()),
        sa.column("campaign_name", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("version", sa.Integer()),
        sa.column("trigger_delay_value", sa.Integer()),
        sa.column("trigger_delay_unit", sa.String()),
        sa.column("template_param_keys", sa.JSON()),
        sa.column("media", sa.JSON()),
        sa.column("buttons", sa.JSON()),
        sa.column("attributes", sa.JSON()),
        sa.column("tags", sa.JSON()),
        sa.column("params_fallback_value", sa.JSON()),
        sa.column("source", sa.String()),
        sa.column("notes", sa.Text()),
    )
    op.bulk_insert(
        registry_seed,
        [
            {
                "uid": "smartping_campaign_registry_cart_abandonment_nudge_1",
                "event_key": "cart_abandonment_nudge_1",
                "business_event_key": "cart_abandoned",
                "campaign_name": "Healthier Cows, Higher Profits",
                "provider": "smartping",
                "is_active": True,
                "version": 1,
                "trigger_delay_value": 30,
                "trigger_delay_unit": "minutes",
                "template_param_keys": ["first_name", "product_name", "savings_amount"],
                "media": None,
                "buttons": None,
                "attributes": None,
                "tags": None,
                "params_fallback_value": {
                    "first_name": "user",
                    "product_name": "product",
                    "savings_amount": "0",
                },
                "source": "website",
                "notes": "Current live abandoned cart campaign",
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_smartping_message_jobs_idempotency_key", table_name="smartping_message_jobs")
    op.drop_index("ix_smartping_message_jobs_status", table_name="smartping_message_jobs")
    op.drop_index("ix_smartping_message_jobs_send_at", table_name="smartping_message_jobs")
    op.drop_index("ix_smartping_message_jobs_business_event_key", table_name="smartping_message_jobs")
    op.drop_index("ix_smartping_message_jobs_event_key", table_name="smartping_message_jobs")
    op.drop_table("smartping_message_jobs")

    op.drop_index("ix_smartping_campaign_registry_is_active", table_name="smartping_campaign_registry")
    op.drop_index("ix_smartping_campaign_registry_business_event_key", table_name="smartping_campaign_registry")
    op.drop_index("ix_smartping_campaign_registry_event_key", table_name="smartping_campaign_registry")
    op.drop_table("smartping_campaign_registry")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        smartping_job_status.drop(bind, checkfirst=True)
