"""merge fb_question_label + agency_grouping heads

Unifies the two branches that both chained off rbac_roles_scope (the CRM/FB
leads_mobile_unique→fb_question_label chain and the agency_grouping chain).
Empty merge node — no schema change.

Revision ID: 20260625_01_merge_heads
Revises: 20260624_02_fb_question_label, 20260624_01_agency_grouping
Create Date: 2026-06-25 05:41:25.048498

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260625_01_merge_heads'
down_revision: Union[str, None] = ('20260624_02_fb_question_label', '20260624_01_agency_grouping')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
