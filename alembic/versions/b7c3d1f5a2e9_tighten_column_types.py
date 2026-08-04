"""tighten money columns: Float->Numeric(12,2)

Revision ID: b7c3d1f5a2e9
Revises: a96bab9eb6b4
Create Date: 2026-08-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c3d1f5a2e9'
down_revision: Union[str, None] = 'a96bab9eb6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column) pairs where money was stored as float; now Numeric(12, 2).
_MONEY_COLUMNS = [
    ('infracost_estimates', 'monthly_cost_usd'),
    ('infracost_estimates', 'annual_cost_usd'),
    ('cost_alerts', 'threshold_usd'),
    ('cost_alerts', 'actual_cost_usd'),
    ('deployments', 'cost_total_monthly'),
]


def upgrade() -> None:
    # SQLite does not support in-place column type changes; the dev DB is
    # recreated from the models via create_all(). The type fix matters on
    # Postgres, so apply it only there.
    if op.get_bind().dialect.name != 'postgresql':
        return

    for table, column in _MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Float(),
            type_=sa.Numeric(12, 2),
            postgresql_using=f"{column}::numeric(12,2)",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return

    for table, column in reversed(_MONEY_COLUMNS):
        op.alter_column(
            table,
            column,
            existing_type=sa.Float(),
            type_=sa.Float(),
            postgresql_using=f"{column}::double precision",
        )