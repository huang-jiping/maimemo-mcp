"""Persist logical slots independently of actual collection timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingestion_run", sa.Column("scheduled_at", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_ingestion_run_successful_slot",
        "ingestion_run",
        ["task_type", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("scheduled_at IS NOT NULL AND status IN ('complete', 'partial')"),
    )
    op.execute("UPDATE schema_metadata SET schema_version='0003' WHERE schema_version='0002'")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM ingestion_run WHERE scheduled_at IS NOT NULL) THEN
                RAISE EXCEPTION '0003 downgrade cannot preserve scheduled job identity'
                    USING ERRCODE = '22P02';
            END IF;
        END $$;
    """)
    op.drop_index("uq_ingestion_run_successful_slot", table_name="ingestion_run")
    op.drop_column("ingestion_run", "scheduled_at")
    op.execute("UPDATE schema_metadata SET schema_version='0002' WHERE schema_version='0003'")
