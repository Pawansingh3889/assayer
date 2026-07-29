"""add password_hash to users

Revision ID: bbe01a8a9655
Revises: bff33e6e35e1
Create Date: 2026-07-29 01:00:17.708722

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'bbe01a8a9655'
down_revision: str | None = 'bff33e6e35e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, and it stays nullable. Existing rows have no password — they were
    # created when the caller simply asserted a user id — and an account backed by an
    # external identity provider never will. Those accounts cannot log in locally,
    # which is the correct outcome: whoever ran this migration gets to decide who is
    # allowed back in, rather than the column default deciding for them.
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Drops every stored credential. Reversible in schema only — going back down and
    # up again leaves an empty column and locks out every account.
    op.drop_column('users', 'password_hash')
