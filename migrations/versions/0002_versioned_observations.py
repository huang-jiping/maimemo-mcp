"""String upstream IDs and explicitly versioned formal observations.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_vocabulary_maimemo_id"), "vocabulary", type_="check")
    op.alter_column(
        "vocabulary",
        "maimemo_id",
        type_=sa.Text(),
        existing_type=sa.BigInteger(),
        postgresql_using="maimemo_id::text",
    )
    op.create_check_constraint("maimemo_id", "vocabulary", "length(maimemo_id) > 0")
    op.add_column(
        "api_snapshot",
        sa.Column("observation_kind", sa.Text(), nullable=False, server_default="OBSERVATION"),
    )
    op.create_check_constraint(
        "observation_kind", "api_snapshot", "observation_kind IN ('BASELINE', 'OBSERVATION')"
    )
    op.drop_constraint(
        "uq_daily_word_observation_day_word", "daily_word_observation", type_="unique"
    )
    op.create_unique_constraint(
        "uq_daily_word_observation_day_word_source",
        "daily_word_observation",
        ["study_date", "vocabulary_id", "source_snapshot_id"],
    )
    op.execute("UPDATE schema_metadata SET schema_version = '0002' WHERE schema_version = '0001'")


def downgrade() -> None:
    # A downgrade cannot losslessly represent opaque IDs or multiple observations.
    # PostgreSQL constraints/casts reject incompatible data instead of silently deleting it.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM vocabulary WHERE maimemo_id !~ '^[1-9][0-9]*$') THEN
                RAISE EXCEPTION '0002 downgrade requires canonical positive integer IDs'
                    USING ERRCODE = '22P02';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "uq_daily_word_observation_day_word_source", "daily_word_observation", type_="unique"
    )
    op.create_unique_constraint(
        "uq_daily_word_observation_day_word",
        "daily_word_observation",
        ["study_date", "vocabulary_id"],
    )
    op.drop_constraint(op.f("ck_api_snapshot_observation_kind"), "api_snapshot", type_="check")
    op.drop_column("api_snapshot", "observation_kind")
    op.drop_constraint(op.f("ck_vocabulary_maimemo_id"), "vocabulary", type_="check")
    op.alter_column(
        "vocabulary",
        "maimemo_id",
        type_=sa.BigInteger(),
        existing_type=sa.Text(),
        postgresql_using="maimemo_id::bigint",
    )
    op.create_check_constraint("maimemo_id", "vocabulary", "maimemo_id > 0")
    op.execute("UPDATE schema_metadata SET schema_version = '0001' WHERE schema_version = '0002'")
