"""track follow-up probes asked per question

Revision ID: 05acfaf695ee
Revises: 2a34f8914837
Create Date: 2026-07-21 15:40:14.238163

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '05acfaf695ee'
down_revision: str | None = '2a34f8914837'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing runs have asked no probes, so they backfill to an empty object. The
    # server default also keeps the column safe for inserts made outside the ORM.
    op.add_column(
        'survey_runs',
        sa.Column(
            'probes_asked',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column('survey_runs', 'probes_asked')
