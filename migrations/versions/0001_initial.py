"""initial connected operations schema

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("organizations",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("authorization_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("users",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("email", sa.String(320), unique=True))
    op.create_table("memberships",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("organization_id", sa.String(128), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(128), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("all_customers", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("all_projects", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("customer_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("project_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("roles", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"))
    op.create_table("auth_sessions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("organization_id", sa.String(128), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("membership_id", sa.String(128), sa.ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(128), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["access_token_hash"], unique=True)

    op.create_table("sync_entities",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("entity_type", sa.String(64), primary_key=True),
        sa.Column("entity_id", sa.String(256), primary_key=True),
        sa.Column("server_revision", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_table("sync_mutations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("client_mutation_id", sa.String(256), nullable=False),
        sa.Column("submitted_by_user_id", sa.String(128), nullable=False),
        sa.Column("membership_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("authorization_revision", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.String(256), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("base_server_revision", sa.BigInteger()),
        sa.Column("result_server_revision", sa.BigInteger()),
        sa.Column("result_status", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "client_mutation_id", name="uq_sync_mutation_org_client"))
    op.create_table("sync_change_log",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("server_revision", sa.BigInteger(), nullable=False),
        sa.Column("client_mutation_id", sa.String(256)),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_sync_change_log_org_sequence", "sync_change_log", ["organization_id", "sequence"])

    op.create_table("evidence_blobs",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(256), primary_key=True),
        sa.Column("item_id", sa.String(256), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))

def downgrade():
    for table in ("evidence_blobs", "sync_change_log", "sync_mutations", "sync_entities",
                  "auth_sessions", "memberships", "users", "organizations"):
        op.drop_table(table)
