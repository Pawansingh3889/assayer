"""add show_when to survey questions

Revision ID: bff33e6e35e1
Revises: 05acfaf695ee
Create Date: 2026-07-27 22:49:15.771871

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'bff33e6e35e1'
down_revision: str | None = '05acfaf695ee'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, and NULL means "always visible" — existing questions carry no condition,
    # so the column needs no backfill and old rows keep behaving exactly as before.
    op.add_column(
        "survey_questions",
        sa.Column("show_when", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("survey_questions", "show_when")
