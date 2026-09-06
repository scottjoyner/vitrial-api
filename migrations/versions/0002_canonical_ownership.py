"""canonical customer project item ownership

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "canonical_customers",
        sa.Column("organization_id", sa.String(128), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("customer_id", sa.String(256), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "canonical_projects",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("customer_id", sa.String(256), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["canonical_customers.organization_id", "canonical_customers.customer_id"],
            name="fk_canonical_project_customer",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_canonical_projects_customer_id", "canonical_projects", ["customer_id"])
    op.create_table(
        "canonical_items",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("item_id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(256), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["canonical_projects.organization_id", "canonical_projects.project_id"],
            name="fk_canonical_item_project",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_canonical_items_project_id", "canonical_items", ["project_id"])

    # Existing generic sync rows are not trusted as authorization evidence. Backfill only records
    # whose complete parent chain can be proven from canonical rows in this same organization.
    op.execute("""
        INSERT INTO canonical_customers (organization_id, customer_id, deleted_at)
        SELECT organization_id, entity_id, deleted_at
        FROM sync_entities
        WHERE entity_type = 'customer'
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO canonical_projects (organization_id, project_id, customer_id, deleted_at)
        SELECT e.organization_id, e.entity_id, e.payload_json ->> 'customerID', e.deleted_at
        FROM sync_entities e
        JOIN canonical_customers c
          ON c.organization_id = e.organization_id
         AND c.customer_id = e.payload_json ->> 'customerID'
        WHERE e.entity_type = 'project'
          AND COALESCE(e.payload_json ->> 'customerID', '') <> ''
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO canonical_items (organization_id, item_id, project_id, deleted_at)
        SELECT e.organization_id, e.entity_id, e.payload_json ->> 'projectID', e.deleted_at
        FROM sync_entities e
        JOIN canonical_projects p
          ON p.organization_id = e.organization_id
         AND p.project_id = e.payload_json ->> 'projectID'
        WHERE e.entity_type = 'item'
          AND COALESCE(e.payload_json ->> 'projectID', '') <> ''
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    op.drop_table("canonical_items")
    op.drop_table("canonical_projects")
    op.drop_table("canonical_customers")
