"""0003 add deployment monitoring snapshots

Revision ID: c9e1f4a7b3d6
Revises: b7f3a9c1d2e4
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e1f4a7b3d6'
down_revision: Union[str, None] = 'b7f3a9c1d2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deployment_monitoring_snapshots',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('deployment_id', sa.String(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('desired_count', sa.Integer(), nullable=True),
        sa.Column('running_count', sa.Integer(), nullable=True),
        sa.Column('pending_count', sa.Integer(), nullable=True),
        sa.Column('healthy_targets', sa.Integer(), nullable=True),
        sa.Column('unhealthy_targets', sa.Integer(), nullable=True),
        sa.Column('estimated_monthly_cost_usd', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_monitoring_snapshots_deployment_id',
        'deployment_monitoring_snapshots',
        ['deployment_id'],
        unique=False,
    )
    op.create_index(
        'idx_monitoring_snapshots_checked_at',
        'deployment_monitoring_snapshots',
        ['checked_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_monitoring_snapshots_checked_at', table_name='deployment_monitoring_snapshots')
    op.drop_index('idx_monitoring_snapshots_deployment_id', table_name='deployment_monitoring_snapshots')
    op.drop_table('deployment_monitoring_snapshots')