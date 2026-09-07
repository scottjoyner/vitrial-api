"""add evidence object storage provider and durable GC queue

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_blobs",
        sa.Column(
            "storage_provider",
            sa.String(length=32),
            nullable=False,
            server_default="local",
        ),
    )
    op.create_table(
        "evidence_object_gc",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("storage_provider", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_object_gc_organization_id", "evidence_object_gc", ["organization_id"])
    op.create_index("ix_evidence_object_gc_document_id", "evidence_object_gc", ["document_id"])
    op.create_index("ix_evidence_object_gc_not_before", "evidence_object_gc", ["not_before"])


def downgrade() -> None:
    op.drop_index("ix_evidence_object_gc_not_before", table_name="evidence_object_gc")
    op.drop_index("ix_evidence_object_gc_document_id", table_name="evidence_object_gc")
    op.drop_index("ix_evidence_object_gc_organization_id", table_name="evidence_object_gc")
    op.drop_table("evidence_object_gc")
    op.drop_column("evidence_blobs", "storage_provider")
