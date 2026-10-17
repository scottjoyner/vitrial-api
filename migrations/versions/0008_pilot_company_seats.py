"""add tenant-scoped configurable active-seat allowance

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("seat_limit", sa.Integer(), nullable=False, server_default="4"),
    )
    op.create_check_constraint(
        "ck_organizations_seat_limit", "organizations", "seat_limit >= 1 AND seat_limit <= 10000"
    )
    # Do not strand existing organizations that already legitimately have more
    # than four active staff. The pilot default applies to new organizations.
    op.execute(
        """
        UPDATE organizations AS organization
        SET seat_limit = GREATEST(
            4,
            (SELECT COUNT(*) FROM memberships AS membership
             WHERE membership.organization_id = organization.id
               AND membership.active IS TRUE)
        )
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_organizations_seat_limit", "organizations", type_="check")
    op.drop_column("organizations", "seat_limit")
