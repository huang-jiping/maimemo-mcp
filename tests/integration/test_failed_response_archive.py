"""Schema failures retain Worker evidence without contaminating successful history."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from maimemo.api_client.errors import UpstreamSchemaError
from maimemo.api_client.study import StudyClient
from maimemo.api_client.transport import MaimemoTransport
from maimemo.ingestion.service import StudyIngestionService
from maimemo.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo.storage.models.learning import DailyProgress, DailyWordObservation
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tests.integration.test_collection_midnight import PROGRESS, TODAY

AT = datetime(2030, 10, 2, 12, tzinfo=UTC)
PRIVATE = "SYNTHETIC_PRIVATE_RESPONSE"


class Limiter:
    async def acquire(self, fingerprint: str) -> None:
        pass


@pytest.mark.parametrize("failure_endpoint", ["progress", "today"])
async def test_schema_archive_rollback_preserves_baseline_and_hides_raw(
    database: AsyncEngine, caplog: pytest.LogCaptureFixture, failure_endpoint: str,
) -> None:
    caplog.set_level(logging.INFO)
    malformed = {"progress": {"finished": 1, "future_field": PRIVATE}} if (
        failure_endpoint == "progress"
    ) else {"today_items": [{"voc_spelling": PRIVATE}]}
    malformed["credential_echo"] = {"value": "Bearer archive-token"}
    broken = True

    def respond(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        progress = operation == "get_study_progress"
        payload = malformed if broken and (progress == (failure_endpoint == "progress")) else (
            {**PROGRESS, "optional": PRIVATE} if progress else TODAY
        )
        return httpx.Response(200, json=payload)

    factory = async_sessionmaker(database, expire_on_commit=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        transport = MaimemoTransport(SecretStr("archive-token"), SecretStr("archive-key"),
                                    Limiter(), client=http)
        client = StudyClient(transport)
        service = StudyIngestionService(factory, client)
        failure = await service.collect_today(AT)
        assert failure.status == "failed"
        async with factory() as session:
            # Read SQL so the RED catches missing persistent archive rather than missing imports.
            exists = await session.scalar(text("SELECT to_regclass('failed_api_snapshot')"))
            assert exists is not None, "Missing isolated Worker failure archive"
            raw = await session.scalar(text("SELECT raw_response FROM failed_api_snapshot"))
            assert raw == {**malformed, "credential_echo": {"value": "Bearer [REDACTED]"}}
            assert "archive-token" not in json.dumps(raw)
            assert await session.scalar(select(func.count()).select_from(ApiSnapshot)) == 0
            assert await session.scalar(select(func.count()).select_from(DailyProgress)) == 0
            assert await session.scalar(select(func.count()).select_from(DailyWordObservation)) == 0
        assert PRIVATE not in caplog.text
        assert PRIVATE not in repr(failure)
        broken = False
        assert (await service.collect_today(AT + timedelta(seconds=1))).status == "complete"
        async with factory() as session:
            assert set(await session.scalars(select(ApiSnapshot.observation_kind))) == {"BASELINE"}
            progress_raw = await session.scalar(select(ApiSnapshot.raw_response).where(
                ApiSnapshot.endpoint.endswith("get_study_progress")
            ))
            assert progress_raw["optional"] == PRIVATE
            before = await session.scalar(select(DailyProgress.total_count))
        broken = True
        failure = await service.collect_today(AT + timedelta(seconds=2))
        assert failure.status == "failed"
        async with factory() as session:
            assert await session.scalar(select(DailyProgress.total_count)) == before == 1
            retained = await session.scalar(text("SELECT count(*) FROM failed_api_snapshot"))
            assert retained == 2
        # Live calls use the same real transport after collection scope has exited.
        with pytest.raises(UpstreamSchemaError) as caught:
            if failure_endpoint == "progress":
                await client.get_progress()
            else:
                from maimemo.api_client.study import TodayItemsRequest
                await client.get_today_items(TodayItemsRequest())
        assert PRIVATE not in str(caught.value)
        assert caught.value.__context__ is None
        async with factory() as session:
            assert await session.scalar(
                text("SELECT count(*) FROM failed_api_snapshot")
            ) == retained
            safe_runs = list(await session.scalars(select(IngestionRun.error_summary)))
            assert PRIVATE not in json.dumps(safe_runs)


async def test_archive_migration_downgrade_refuses_losing_failure_evidence(
    database: AsyncEngine, alembic_config: Config,
) -> None:
    async with database.begin() as connection:
        exists = await connection.scalar(text("SELECT to_regclass('failed_api_snapshot')"))
        assert exists is not None, "Missing isolated Worker failure archive"
        await connection.execute(text(
            "WITH run AS (INSERT INTO ingestion_run(id, task_type, status) "
            "VALUES(gen_random_uuid(), 'today', 'failed') RETURNING id) "
            "INSERT INTO failed_api_snapshot(id, endpoint, request_hash, content_hash, "
            "raw_response, fetched_at, ingestion_run_id) "
            "SELECT gen_random_uuid(), 'today', 'request', 'content', '{}'::jsonb, "
            "now(), id FROM run"
        ))
    with pytest.raises(DataError, match="failure evidence"):
        await asyncio.to_thread(command.downgrade, alembic_config, "0003")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
        assert await connection.scalar(text("SELECT count(*) FROM failed_api_snapshot")) == 1


async def test_archive_database_rejection_does_not_escape_with_raw_parameters(
    database: AsyncEngine,
) -> None:
    import traceback

    async with database.begin() as connection:
        await connection.execute(text(
            "ALTER TABLE failed_api_snapshot ADD CONSTRAINT reject_archive "
            "CHECK (length(endpoint)=0)"
        ))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"progress": {"finished": 1, "private": PRIVATE}}
        ))) as http:
            transport = MaimemoTransport(SecretStr("archive-token"), SecretStr("archive-key"),
                                        Limiter(), client=http)
            service = StudyIngestionService(async_sessionmaker(database), StudyClient(transport))
            with pytest.raises(Exception) as caught:
                await service.collect_today(AT)
            assert PRIVATE not in "".join(traceback.format_exception(caught.value))
            error = caught.value
            while error is not None:
                assert PRIVATE not in repr(error)
                error = error.__context__
        async with database.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM failed_api_snapshot")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM ingestion_run")) == 0
    finally:
        async with database.begin() as connection:
            await connection.execute(text(
                "ALTER TABLE failed_api_snapshot DROP CONSTRAINT reject_archive"
            ))
