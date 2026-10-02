"""Feedback inputs reject inferred facts and preserve the user's spelling/direction."""

import pytest
from pydantic import ValidationError

from maimemo_mcp.feedback.models import (
    FeedbackQuery,
    RecordFeedbackCommand,
    RetractFeedbackCommand,
)


def command(**changes: object) -> RecordFeedbackCommand:
    values: dict[str, object] = {
        "word_a_spelling": " affect ",
        "word_b_spelling": "Effect",
        "relation_type": "FORM_SIMILAR",
        "direction": "A_TO_B",
        "evidence_type": "USER_CONFIRMED",
        "explicit_confirmation": True,
        "source_agent": "synthetic-agent",
        "idempotency_key": "synthetic-key",
    }
    return RecordFeedbackCommand.model_validate(values | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"word_a_spelling": "  "},
        {"word_b_spelling": "\t"},
        {"word_b_spelling": " AFFECT "},
        {"evidence_type": "SYSTEM_INFERRED"},
        {"relation_type": "USER_CONFIRMED"},
        {"direction": "UNKNOWN"},
        {"explicit_confirmation": False},
        {"explicit_confirmation": "true"},
        {"source_agent": " "},
        {"idempotency_key": " "},
    ],
)
def test_invalid_feedback_cannot_become_a_personal_fact(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        command(**changes)


def test_raw_spelling_and_direction_are_preserved() -> None:
    result = command()
    assert result.word_a_spelling == " affect "
    assert result.word_b_spelling == "Effect"
    assert result.direction == "A_TO_B"


def test_quiz_observation_does_not_require_user_confirmation() -> None:
    assert command(evidence_type="QUIZ_OBSERVED", explicit_confirmation=False).evidence_type == (
        "QUIZ_OBSERVED"
    )


def test_user_confirmation_cannot_be_omitted() -> None:
    values = command().model_dump(exclude={"explicit_confirmation"})
    with pytest.raises(ValidationError):
        RecordFeedbackCommand.model_validate(values)


def test_quiz_observation_cannot_claim_user_confirmation() -> None:
    with pytest.raises(ValidationError):
        command(evidence_type="QUIZ_OBSERVED", explicit_confirmation=True)


@pytest.mark.parametrize("limit", [0, 1001])
def test_query_rejects_unbounded_result_limits(limit: int) -> None:
    with pytest.raises(ValidationError):
        FeedbackQuery(limit=limit)


def test_retract_requires_target_identity() -> None:
    with pytest.raises(ValidationError):
        RetractFeedbackCommand.model_validate({"idempotency_key": "retract-key"})
