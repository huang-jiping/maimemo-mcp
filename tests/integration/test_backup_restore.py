"""Exercise a real custom-format PostgreSQL backup and guarded empty-db restore."""

from __future__ import annotations

import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.test.yaml"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        shell=False,
        check=True,
    )


def _postgres_container() -> str:
    result = _run(
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "ps",
        "-q",
        "postgres",
    )
    container = result.stdout.strip()
    if not container:
        raise RuntimeError("Start the disposable postgres service before this test")
    return container


async def _seed_critical_rows(database: AsyncEngine) -> dict[str, object]:
    run_id = uuid4()
    snapshot_id = uuid4()
    word_id = uuid4()
    feedback_id = uuid4()
    retraction_id = uuid4()
    request_hash = "r" * 64
    content_hash = "c" * 64
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ingestion_run "
                "(id, task_type, status, request_count, result_count) "
                "VALUES (:id, 'today', 'complete', 1, 1)"
            ),
            {"id": run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO api_snapshot "
                "(id, endpoint, request_hash, content_hash, raw_response, ingestion_run_id) "
                "VALUES (:id, 'today', :request_hash, :content_hash, "
                "'{\"study\":\"synthetic\"}'::jsonb, :run_id)"
            ),
            {
                "id": snapshot_id,
                "request_hash": request_hash,
                "content_hash": content_hash,
                "run_id": run_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO vocabulary "
                "(id, maimemo_id, normalized_spelling, spelling) "
                "VALUES (:id, 'backup-test-word', 'affect', 'affect')"
            ),
            {"id": word_id},
        )
        await connection.execute(
            text(
                "INSERT INTO daily_progress "
                "(id, study_date, completed_count, total_count, study_seconds, completeness) "
                "VALUES (:id, '2026-10-02', 1, 1, 60, 'complete')"
            ),
            {"id": uuid4()},
        )
        await connection.execute(
            text(
                "INSERT INTO daily_word_observation "
                "(id, study_date, vocabulary_id, first_feedback, is_new, is_complete, "
                "source_snapshot_id) "
                "VALUES (:id, '2026-10-02', :word_id, 'FORGET', false, true, :snapshot_id)"
            ),
            {"id": uuid4(), "word_id": word_id, "snapshot_id": snapshot_id},
        )
        await connection.execute(
            text(
                "INSERT INTO study_record_snapshot "
                "(id, vocabulary_id, last_feedback, study_count, tags, source_snapshot_id) "
                "VALUES (:id, :word_id, 'FORGET', 3, '[\"synthetic\"]'::jsonb, :snapshot_id)"
            ),
            {"id": uuid4(), "word_id": word_id, "snapshot_id": snapshot_id},
        )
        await connection.execute(
            text(
                "INSERT INTO weakness_score "
                "(id, vocabulary_id, algorithm_version, score, risk_level, confidence, "
                "factors, evidence_from, evidence_through, latest_snapshot_id) "
                "VALUES (:id, :word_id, 'backup-test', 72, 'high', 0.8, "
                "CAST(:factors AS jsonb), '2026-10-01T00:00:00Z', "
                "'2026-10-02T00:00:00Z', :snapshot_id)"
            ),
            {
                "id": uuid4(),
                "word_id": word_id,
                "snapshot_id": snapshot_id,
                "factors": '{"severity":1}',
            },
        )
        await connection.execute(
            text(
                "INSERT INTO learning_feedback_event "
                "(id, event_type, word_a_id, word_b_spelling, word_a_status, word_b_status, "
                "relation_type, direction, evidence_type, source_agent, idempotency_key) "
                "VALUES (:id, 'CONFUSION', :word_id, 'effect', 'RESOLVED', 'UNRESOLVED', "
                "'CONFUSED_WITH', 'A_TO_B', 'USER_CONFIRMED', 'backup-test', "
                "'backup-test-feedback')"
            ),
            {"id": feedback_id, "word_id": word_id},
        )
        await connection.execute(
            text(
                "INSERT INTO learning_feedback_event "
                "(id, event_type, word_a_status, word_b_status, idempotency_key, "
                "retracted_event_id) "
                "VALUES (:id, 'RETRACTION', 'UNRESOLVED', 'UNRESOLVED', "
                "'backup-test-retraction', :feedback_id)"
            ),
            {"id": retraction_id, "feedback_id": feedback_id},
        )
        await connection.execute(
            text(
                "INSERT INTO api_rate_limit_window "
                "(id, token_hash, window_type, window_start, used_requests) "
                "VALUES (:id, :token_hash, '10s', '2026-10-02T00:00:00Z', 1)"
            ),
            {"id": uuid4(), "token_hash": "f" * 64},
        )
    return {
        "request_hash": request_hash,
        "content_hash": content_hash,
        "feedback_id": feedback_id,
        "retraction_id": retraction_id,
    }


async def test_custom_dump_restores_critical_rows_and_links(
    database: AsyncEngine,
    tmp_path: Path,
) -> None:
    expected = await _seed_critical_rows(database)
    container = _postgres_container()
    target = f"maimemo_restore_{uuid4().hex}"
    backup = tmp_path / "maimemo-test.dump"
    env = dict(os.environ)
    env.update(
        {
            "MAIMEMO_BACKUP_DATABASE_URL": (
                "postgresql://maimemo_test:test_only@127.0.0.1:5432/maimemo_test"
            ),
            "MAIMEMO_RESTORE_ADMIN_URL": (
                "postgresql://maimemo_test:test_only@127.0.0.1:5432/postgres"
            ),
        }
    )

    try:
        _run(
            sys.executable,
            "scripts/backup_postgres.py",
            "--output",
            str(backup),
            "--docker-container",
            container,
            env=env,
        )
        assert backup.is_file()
        assert backup.stat().st_size > 0
        original_dump_hash = sha256(backup.read_bytes()).hexdigest()
        with pytest.raises(subprocess.CalledProcessError):
            _run(
                sys.executable,
                "scripts/backup_postgres.py",
                "--output",
                str(backup),
                "--docker-container",
                container,
                env=env,
            )
        assert sha256(backup.read_bytes()).hexdigest() == original_dump_hash

        _run(
            sys.executable,
            "scripts/restore_postgres.py",
            "--backup",
            str(backup),
            "--target-database",
            target,
            "--confirm-disposable-target",
            target,
            "--docker-container",
            container,
            env=env,
        )
        with pytest.raises(subprocess.CalledProcessError):
            _run(
                sys.executable,
                "scripts/restore_postgres.py",
                "--backup",
                str(backup),
                "--target-database",
                target,
                "--confirm-disposable-target",
                target,
                "--docker-container",
                container,
                env=env,
            )

        restored_url = (
            f"postgresql+psycopg://maimemo_test:test_only@127.0.0.1:55432/{target}"
        )
        restored = create_async_engine(restored_url)
        try:
            async with restored.connect() as connection:
                counts = {
                    table: await connection.scalar(text(f'SELECT count(*) FROM "{table}"'))
                    for table in (
                        "ingestion_run",
                        "api_snapshot",
                        "vocabulary",
                        "daily_progress",
                        "daily_word_observation",
                        "study_record_snapshot",
                        "weakness_score",
                        "learning_feedback_event",
                        "api_rate_limit_window",
                    )
                }
                assert counts == {
                    "ingestion_run": 1,
                    "api_snapshot": 1,
                    "vocabulary": 1,
                    "daily_progress": 1,
                    "daily_word_observation": 1,
                    "study_record_snapshot": 1,
                    "weakness_score": 1,
                    "learning_feedback_event": 2,
                    "api_rate_limit_window": 1,
                }
                assert await connection.scalar(
                    text("SELECT request_hash FROM api_snapshot")
                ) == expected["request_hash"]
                assert await connection.scalar(
                    text("SELECT content_hash FROM api_snapshot")
                ) == expected["content_hash"]
                assert await connection.scalar(
                    text(
                        "SELECT retracted_event_id FROM learning_feedback_event "
                        "WHERE id = :id"
                    ),
                    {"id": expected["retraction_id"]},
                ) == expected["feedback_id"]
                assert await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == "0003"
                assert await connection.scalar(
                    text("SELECT schema_version FROM schema_metadata")
                ) == "0003"
        finally:
            await restored.dispose()
    finally:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "dropdb",
                "--force",
                "--if-exists",
                "--username=maimemo_test",
                target,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            shell=False,
            check=False,
        )


def test_restore_rejects_target_that_postgres_would_truncate(tmp_path: Path) -> None:
    backup = tmp_path / "synthetic.dump"
    backup.write_bytes(b"not inspected before target validation")
    overlong_target = f"maimemo_restore_{'a' * 48}"
    env = dict(os.environ)
    env["MAIMEMO_RESTORE_ADMIN_URL"] = "postgresql://admin:secret@db.invalid/postgres"

    with pytest.raises(subprocess.CalledProcessError) as caught:
        _run(
            sys.executable,
            "scripts/restore_postgres.py",
            "--backup",
            str(backup),
            "--target-database",
            overlong_target,
            "--confirm-disposable-target",
            overlong_target,
            env=env,
        )

    assert (
        "Target must match maimemo_restore_[a-z0-9_]{8,47}" in caught.value.stderr
    )


def test_failed_restore_removes_only_its_new_disposable_database(tmp_path: Path) -> None:
    container = _postgres_container()
    target = f"maimemo_restore_{uuid4().hex}"
    backup = tmp_path / "corrupt.dump"
    backup.write_bytes(b"not a PostgreSQL custom-format dump")
    env = dict(os.environ)
    env["MAIMEMO_RESTORE_ADMIN_URL"] = (
        "postgresql://maimemo_test:test_only@127.0.0.1:5432/postgres"
    )

    with pytest.raises(subprocess.CalledProcessError):
        _run(
            sys.executable,
            "scripts/restore_postgres.py",
            "--backup",
            str(backup),
            "--target-database",
            target,
            "--confirm-disposable-target",
            target,
            "--docker-container",
            container,
            env=env,
        )

    exists = _run(
        "docker",
        "exec",
        container,
        "psql",
        "--username=maimemo_test",
        "--dbname=postgres",
        "--tuples-only",
        "--no-align",
        "--command",
        f"SELECT 1 FROM pg_database WHERE datname = '{target}';",
    )
    assert exists.stdout.strip() == ""
