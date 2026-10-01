"""Real SDK and PostgreSQL catch authorization, direction and append-only replay errors."""

from typing import Any

import pytest
from mcp.client import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app


def record(**updates: Any) -> dict[str, Any]:
    return {
        "request": {
            "word_a_spelling": " Alpha ",
            "word_b_spelling": "Beta",
            "relation_type": "FORM_SIMILAR",
            "direction": "B_TO_A",
            "evidence_type": "USER_CONFIRMED",
            "confirmed_by_user": True,
            "idempotency_key": "record-one",
            "source_agent": "sdk-test",
            **updates,
        }
    }


@pytest.mark.parametrize(
    "evidence,confirmed",
    [
        ("USER_CONFIRMED", False),
        ("QUIZ_OBSERVED", True),
        ("SYSTEM_INFERRED", False),
    ],
)
async def test_feedback_rejects_inference_and_mislabeled_evidence(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    evidence: str,
    confirmed: bool,
) -> None:
    async with Client(create_mcp_app(workflow_settings).sdk) as client:
        assert "record_confusion_feedback" in {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool(
            "record_confusion_feedback",
            record(
                evidence_type=evidence,
                confirmed_by_user=confirmed,
            ),
        )
        assert result.is_error is True
    async with workflow_database.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM learning_feedback_event")) == 0


@pytest.mark.parametrize("evidence,confirmed", [("USER_CONFIRMED", True), ("QUIZ_OBSERVED", False)])
async def test_feedback_keeps_direction_unresolved_spelling_and_append_only_retraction(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    evidence: str,
    confirmed: bool,
) -> None:
    async with Client(create_mcp_app(workflow_settings).sdk) as client:
        args = record(evidence_type=evidence, confirmed_by_user=confirmed)
        first = await client.call_tool("record_confusion_feedback", args)
        assert first.is_error is False, first.content
        assert first.structured_content is not None
        data = first.structured_content["data"]
        assert data["word_a_spelling"] == " Alpha "
        assert data["direction"] == "B_TO_A"
        assert data["evidence_type"] == evidence
        assert data["word_a_status"] == data["word_b_status"] == "UNRESOLVED"
        assert data["word_a_id"] is None
        assert first.structured_content["meta"]["source"] == ["local_feedback"]
        replay = await client.call_tool("record_confusion_feedback", args)
        assert replay.structured_content == first.structured_content
        conflict = await client.call_tool(
            "record_confusion_feedback",
            record(
                evidence_type=evidence,
                confirmed_by_user=confirmed,
                direction="A_TO_B",
            ),
        )
        assert conflict.is_error is True
        retract_args = {
            "request": {
                "event_id": data["id"],
                "idempotency_key": "retract-one",
                "source_agent": "sdk-test",
            }
        }
        retraction = await client.call_tool("retract_feedback", retract_args)
        assert retraction.is_error is False, retraction.content
        assert retraction.structured_content is not None
        assert retraction.structured_content["data"]["retracted_event_id"] == data["id"]
        assert (
            await client.call_tool("retract_feedback", retract_args)
        ).structured_content == retraction.structured_content
    async with workflow_database.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT event_type, direction, retracted_event_id "
                    "FROM learning_feedback_event ORDER BY created_at"
                )
            )
        ).all()
        assert len(rows) == 2
        assert rows[0][:2] == ("CONFUSION", "B_TO_A")
        assert rows[1][0] == "RETRACTION"
        for table in (
            "vocabulary",
            "daily_progress",
            "daily_word_observation",
            "study_record_snapshot",
            "weakness_score",
            "api_snapshot",
            "ingestion_run",
        ):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_feedback_requires_idempotency_key_and_closed_world_annotations(
    workflow_settings: Settings,
) -> None:
    async with Client(create_mcp_app(workflow_settings).sdk) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"record_confusion_feedback", "retract_feedback"} <= tools.keys()
        for name in ("record_confusion_feedback", "retract_feedback"):
            tool = tools[name]
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            assert (
                "idempotency_key"
                in tool.input_schema["$defs"][
                    "RecordConfusionRequest"
                    if name == "record_confusion_feedback"
                    else "RetractFeedbackCommand"
                ]["required"]
            )
        args = record()
        del args["request"]["idempotency_key"]
        assert (await client.call_tool("record_confusion_feedback", args)).is_error is True
