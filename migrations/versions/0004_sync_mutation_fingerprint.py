"""bind client mutation ids to immutable request fingerprints

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_mutation_fingerprints",
        sa.Column("organization_id", sa.String(length=128), nullable=False),
        sa.Column("client_mutation_id", sa.String(length=256), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_sync_mutation_fingerprint_org",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "client_mutation_id",
            name="pk_sync_mutation_fingerprints",
        ),
    )


def downgrade() -> None:
    op.drop_table("sync_mutation_fingerprints")
