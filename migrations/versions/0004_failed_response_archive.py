"""Retain rejected Worker schema responses outside the successful history stream."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "failed_api_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("raw_response", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_failed_api_snapshot"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_run.id"],
                                name="fk_failed_api_snapshot_ingestion_run_id_ingestion_run"),
        sa.CheckConstraint("length(endpoint) > 0", name="ck_failed_api_snapshot_endpoint"),
        sa.CheckConstraint("length(request_hash) > 0 AND length(content_hash) > 0",
                           name="ck_failed_api_snapshot_hashes"),
    )
    op.create_index("ix_failed_api_snapshot_ingestion_run_id", "failed_api_snapshot",
                    ["ingestion_run_id"])
    op.execute("UPDATE schema_metadata SET schema_version='0004' WHERE schema_version='0003'")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM failed_api_snapshot) THEN
                RAISE EXCEPTION '0004 downgrade cannot preserve failure evidence'
                    USING ERRCODE = '22P02';
            END IF;
        END $$;
    """)
    op.drop_index("ix_failed_api_snapshot_ingestion_run_id", table_name="failed_api_snapshot")
    op.drop_table("failed_api_snapshot")
    op.execute("UPDATE schema_metadata SET schema_version='0003' WHERE schema_version='0004'")
