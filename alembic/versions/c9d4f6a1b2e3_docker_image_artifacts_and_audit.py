"""docker-image artifacts and edit audit fields

Revision ID: c9d4f6a1b2e3
Revises: e5a1f8c2b9d4
Create Date: 2026-08-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d4f6a1b2e3'
down_revision: Union[str, None] = 'e5a1f8c2b9d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('terraform_artifacts') as batch_op:
        batch_op.add_column(sa.Column('edited_by', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('edited_at', sa.DateTime(), nullable=True))
        batch_op.drop_constraint('ck_terraform_artifacts_artifact_type', type_='check')
        batch_op.create_check_constraint(
            'ck_terraform_artifacts_artifact_type',
            "artifact_type IN ('terraform', 'dockerfile', 'docker-compose', 'docker-image', 'cloudformation', 'helm', 'kubernetes', 'ansible', 'pulumi', 'bicep')",
        )


def downgrade() -> None:
    with op.batch_alter_table('terraform_artifacts') as batch_op:
        batch_op.drop_constraint('ck_terraform_artifacts_artifact_type', type_='check')
        batch_op.create_check_constraint(
            'ck_terraform_artifacts_artifact_type',
            "artifact_type IN ('terraform', 'dockerfile', 'docker-compose', 'cloudformation', 'helm', 'kubernetes', 'ansible', 'pulumi', 'bicep')",
        )
        batch_op.drop_column('edited_at')
        batch_op.drop_column('edited_by')