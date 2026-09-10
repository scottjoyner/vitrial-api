"""add immutable organization-scoped reference publications

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_publications",
        sa.Column("organization_id", sa.String(length=128), nullable=False),
        sa.Column("publication_id", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("version_id", sa.String(length=256), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_publication_id", sa.String(length=256), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("published_by_user_id", sa.String(length=128), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "publication_id"),
        sa.UniqueConstraint(
            "organization_id", "kind", "version_id",
            name="uq_reference_publication_org_kind_version",
        ),
    )
    op.create_index(
        "ix_reference_publications_kind",
        "reference_publications",
        ["kind"],
    )
    op.create_index(
        "ix_reference_publications_effective_from",
        "reference_publications",
        ["effective_from"],
    )
    op.create_index(
        "ix_reference_publications_effective_until",
        "reference_publications",
        ["effective_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_reference_publications_effective_until", table_name="reference_publications")
    op.drop_index("ix_reference_publications_effective_from", table_name="reference_publications")
    op.drop_index("ix_reference_publications_kind", table_name="reference_publications")
    op.drop_table("reference_publications")
