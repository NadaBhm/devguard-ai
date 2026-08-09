"""rename password to hashed_password in users table

Revision ID: f2e82c473f18
Revises: b7c3d1f5a2e9
Create Date: 2026-08-06 21:21:42.969250

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2e82c473f18'
down_revision: Union[str, None] = 'b7c3d1f5a2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename password column to hashed_password
    op.alter_column('users', 'password', new_column_name='hashed_password')


def downgrade() -> None:
    # Revert: rename hashed_password back to password
    op.alter_column('users', 'hashed_password', new_column_name='password')