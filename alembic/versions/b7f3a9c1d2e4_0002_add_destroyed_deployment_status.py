"""0002 add destroyed deployment status

Revision ID: b7f3a9c1d2e4
Revises: 848480a9e6e6
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f3a9c1d2e4'
down_revision: Union[str, None] = '848480a9e6e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 'destroyed' is distinct from 'rolled_back': a rollback leaves a
    # previous ECS task-definition revision running, a destroy leaves
    # nothing -- feature/destroy-deployment needs its own terminal status
    # rather than overloading 'rolled_back'.
    op.drop_constraint('ck_deployments_status', 'deployments', type_='check')
    op.create_check_constraint(
        'ck_deployments_status',
        'deployments',
        "status IN ('pending', 'applying', 'succeeded', 'failed', 'rolled_back', 'destroyed')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_deployments_status', 'deployments', type_='check')
    op.create_check_constraint(
        'ck_deployments_status',
        'deployments',
        "status IN ('pending', 'applying', 'succeeded', 'failed', 'rolled_back')",
    )