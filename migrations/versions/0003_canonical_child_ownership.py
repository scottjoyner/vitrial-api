"""canonical project-sector and child ownership

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


ITEM_CHILD_TYPES = (
    "item_audit_event",
    "measurement",
    "evidence",
    "customer_requirement",
    "configuration",
    "configuration_version",
    "blocker",
)


def upgrade():
    op.create_table(
        "canonical_project_sectors",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("project_sector_id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(256), nullable=False),
        sa.Column("sector_id", sa.String(256), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["canonical_projects.organization_id", "canonical_projects.project_id"],
            name="fk_canonical_project_sector_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id", "project_id", "project_sector_id",
            name="uq_canonical_project_sector_project_identity",
        ),
    )
    op.create_index(
        "ix_canonical_project_sectors_project_id",
        "canonical_project_sectors",
        ["project_id"],
    )

    op.add_column(
        "canonical_items",
        sa.Column("project_sector_id", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_canonical_items_project_sector_id",
        "canonical_items",
        ["project_sector_id"],
    )
    op.create_foreign_key(
        "fk_canonical_item_project_sector",
        "canonical_items",
        "canonical_project_sectors",
        ["organization_id", "project_id", "project_sector_id"],
        ["organization_id", "project_id", "project_sector_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "canonical_project_children",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("entity_type", sa.String(64), primary_key=True),
        sa.Column("entity_id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(256), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["canonical_projects.organization_id", "canonical_projects.project_id"],
            name="fk_canonical_project_child_project",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_canonical_project_children_project_id",
        "canonical_project_children",
        ["project_id"],
    )

    op.create_table(
        "canonical_item_children",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("entity_type", sa.String(64), primary_key=True),
        sa.Column("entity_id", sa.String(256), primary_key=True),
        sa.Column("item_id", sa.String(256), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "item_id"],
            ["canonical_items.organization_id", "canonical_items.item_id"],
            name="fk_canonical_item_child_item",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_canonical_item_children_item_id",
        "canonical_item_children",
        ["item_id"],
    )

    # Generic sync JSON is migration input, never authorization evidence by itself. Backfill only
    # complete same-organization chains that resolve through already-canonical parents.
    op.execute("""
        INSERT INTO canonical_project_sectors (
            organization_id, project_sector_id, project_id, sector_id, deleted_at
        )
        SELECT
            e.organization_id,
            e.entity_id,
            e.payload_json ->> 'projectID',
            e.payload_json ->> 'sectorID',
            e.deleted_at
        FROM sync_entities e
        JOIN canonical_projects p
          ON p.organization_id = e.organization_id
         AND p.project_id = e.payload_json ->> 'projectID'
        WHERE e.entity_type = 'project_sector'
          AND COALESCE(e.payload_json ->> 'projectID', '') <> ''
          AND COALESCE(e.payload_json ->> 'sectorID', '') <> ''
        ON CONFLICT DO NOTHING
    """)

    op.execute("""
        UPDATE canonical_items i
           SET project_sector_id = e.payload_json ->> 'projectSectorID'
          FROM sync_entities e
          JOIN canonical_project_sectors ps
            ON ps.organization_id = e.organization_id
           AND ps.project_sector_id = e.payload_json ->> 'projectSectorID'
           AND ps.project_id = e.payload_json ->> 'projectID'
         WHERE e.entity_type = 'item'
           AND i.organization_id = e.organization_id
           AND i.item_id = e.entity_id
           AND i.project_id = e.payload_json ->> 'projectID'
           AND COALESCE(e.payload_json ->> 'projectSectorID', '') <> ''
    """)

    op.execute("""
        INSERT INTO canonical_project_children (
            organization_id, entity_type, entity_id, project_id, deleted_at
        )
        SELECT
            e.organization_id,
            e.entity_type,
            e.entity_id,
            e.payload_json ->> 'projectID',
            e.deleted_at
        FROM sync_entities e
        JOIN canonical_projects p
          ON p.organization_id = e.organization_id
         AND p.project_id = e.payload_json ->> 'projectID'
         AND p.customer_id = e.payload_json ->> 'customerID'
        WHERE e.entity_type = 'quotation'
          AND COALESCE(e.payload_json ->> 'projectID', '') <> ''
          AND COALESCE(e.payload_json ->> 'customerID', '') <> ''
        ON CONFLICT DO NOTHING
    """)

    op.execute("""
        INSERT INTO canonical_item_children (
            organization_id, entity_type, entity_id, item_id, deleted_at
        )
        SELECT
            e.organization_id,
            e.entity_type,
            e.entity_id,
            CASE
                WHEN e.entity_type = 'configuration_version'
                    THEN e.payload_json -> 'configuration' ->> 'itemID'
                ELSE e.payload_json ->> 'itemID'
            END,
            e.deleted_at
        FROM sync_entities e
        JOIN canonical_items i
          ON i.organization_id = e.organization_id
         AND i.item_id = CASE
                WHEN e.entity_type = 'configuration_version'
                    THEN e.payload_json -> 'configuration' ->> 'itemID'
                ELSE e.payload_json ->> 'itemID'
            END
         AND i.project_sector_id IS NOT NULL
        WHERE e.entity_type IN (
            'item_audit_event', 'measurement', 'evidence', 'customer_requirement',
            'configuration', 'configuration_version', 'blocker'
        )
          AND COALESCE(
              CASE
                  WHEN e.entity_type = 'configuration_version'
                      THEN e.payload_json -> 'configuration' ->> 'itemID'
                  ELSE e.payload_json ->> 'itemID'
              END,
              ''
          ) <> ''
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    op.drop_table("canonical_item_children")
    op.drop_table("canonical_project_children")
    op.drop_constraint(
        "fk_canonical_item_project_sector",
        "canonical_items",
        type_="foreignkey",
    )
    op.drop_index("ix_canonical_items_project_sector_id", table_name="canonical_items")
    op.drop_column("canonical_items", "project_sector_id")
    op.drop_table("canonical_project_sectors")