"""Cluster hierarchy — clusters, cluster_districts tables + outlets.cluster_id

Revision ID: 20260617_01_cluster_hierarchy
Revises: 20260616_02_user_state, 20260616_01_audit_week_start
Create Date: 2026-06-17 00:00:00.000000

NOTE: the migration graph branches after 692d425f260e, leaving two heads
(`20260616_02_user_state` and `20260616_01_audit_week_start`). This revision lists
both as `down_revision`, so it doubles as the merge point. Confirm with
`alembic heads` before applying; adjust the tuple if the live graph differs.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260617_01_cluster_hierarchy"
down_revision = ("20260616_02_user_state", "20260616_01_audit_week_start")
branch_labels = None
depends_on = None


def _base_columns():
    return [
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    # ---- clusters ----
    op.create_table(
        "clusters",
        *_base_columns(),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("manager_id", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["clusters.uid"]),
        sa.ForeignKeyConstraint(["manager_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("state", "name", name="uq_clusters_state_name"),
    )
    op.create_index("ix_clusters_code", "clusters", ["code"], unique=True)
    op.create_index("ix_clusters_state", "clusters", ["state"])
    op.create_index("ix_clusters_parent_id", "clusters", ["parent_id"])
    op.create_index("ix_clusters_manager_id", "clusters", ["manager_id"])

    # ---- cluster_districts ----
    op.create_table(
        "cluster_districts",
        *_base_columns(),
        sa.Column("cluster_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("district", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("state", "district", name="uq_cluster_districts_state_district"),
    )
    op.create_index("ix_cluster_districts_cluster_id", "cluster_districts", ["cluster_id"])
    op.create_index("ix_cluster_districts_state", "cluster_districts", ["state"])
    op.create_index("ix_cluster_districts_district", "cluster_districts", ["district"])

    # ---- outlets.cluster_id ----
    op.add_column("outlets", sa.Column("cluster_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_outlets_cluster_id", "outlets", "clusters", ["cluster_id"], ["uid"]
    )
    op.create_index("ix_outlets_cluster_id", "outlets", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_outlets_cluster_id", table_name="outlets")
    op.drop_constraint("fk_outlets_cluster_id", "outlets", type_="foreignkey")
    op.drop_column("outlets", "cluster_id")

    op.drop_index("ix_cluster_districts_district", table_name="cluster_districts")
    op.drop_index("ix_cluster_districts_state", table_name="cluster_districts")
    op.drop_index("ix_cluster_districts_cluster_id", table_name="cluster_districts")
    op.drop_table("cluster_districts")

    op.drop_index("ix_clusters_manager_id", table_name="clusters")
    op.drop_index("ix_clusters_parent_id", table_name="clusters")
    op.drop_index("ix_clusters_state", table_name="clusters")
    op.drop_index("ix_clusters_code", table_name="clusters")
    op.drop_table("clusters")
