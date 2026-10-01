"""Hand-derived fixtures catch weight, normalization and evidence correction mistakes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from maimemo_mcp.analysis.models import (
    DailyEvidence,
    RecentResponse,
    WeaknessEvidence,
    WeakWordQuery,
)
from maimemo_mcp.analysis.scoring import calculate_weakness

AT = datetime(2030, 10, 2, 12, tzinfo=UTC)
WORD = UUID("00000000-0000-0000-0000-000000000001")


def day(
    ago: int,
    feedback: str | None = "FAMILIAR",
    complete: bool | None = True,
    *,
    new: bool | None = False,
) -> DailyEvidence:
    at = AT - timedelta(days=ago)
    return DailyEvidence(at.date(), at, feedback, complete, new)


def full() -> WeaknessEvidence:
    return WeaknessEvidence(
        vocabulary_id=WORD,
        spelling="apple",
        observations=(day(1), day(0)),
        record_observed_at=AT,
        last_feedback="FAMILIAR",
        last_studied_at=AT,
        next_study_at=AT + timedelta(days=7),
        added_at=AT - timedelta(days=100),
        study_count=0,
        tags=(),
        quality="complete",
    )


@pytest.mark.parametrize(
    ("changed", "factor", "want"),
    [
        ({"last_feedback": "FORGET"}, "recent_response", 35.0),
        ({"observations": (day(14, "FORGET"), day(0))}, "repeated_error", 21.25),
        ({"tags": ("STICKING",)}, "sticking", 15.0),
        ({"next_study_at": AT}, "interval_pressure", 15.0),
        (
            {"observations": (day(1, complete=False), day(0, complete=False))},
            "unfinished_difficulty",
            10.0,
        ),
    ],
)
def test_each_fixed_weight_changes_total_by_hand_calculated_amount(
    changed: dict[str, Any],
    factor: str,
    want: float,
) -> None:
    # Error fixture: 14-day-old forget contributes .25*35 + .5*25 = 21.25.
    result = calculate_weakness(replace(full(), **changed), AT)
    assert result.score == pytest.approx(want)
    assert result.factors[factor].value > 0
    assert result.confidence == pytest.approx(2 / 7)


@pytest.mark.parametrize(
    ("feedback", "want"),
    [
        ("FORGET", 100),
        ("VAGUE", 50),
        ("FAMILIAR", 0),
        ("WELL_FAMILIAR", 0),
        ("CANCEL_WELL_FAMILIAR", 50),
    ],
)
def test_feedback_severity_with_only_recent_factor(feedback: str, want: float) -> None:
    result = calculate_weakness(
        WeaknessEvidence(WORD, "apple", observations=(day(0, feedback, None, new=None),)), AT
    )
    assert result.score == want
    assert result.confidence == 0  # Missing health is unknown, not complete.


def test_recent_feedback_has_seven_day_half_life_without_hard_cutoff() -> None:
    result = calculate_weakness(
        WeaknessEvidence(WORD, "apple", observations=(day(14, "FORGET", None),)), AT
    )
    assert result.score == 25


def test_missing_factors_lower_confidence_instead_of_scoring_zero() -> None:
    result = calculate_weakness(
        WeaknessEvidence(WORD, "apple", observations=(day(0, "FORGET", None),), quality="complete"),
        AT,
    )
    assert result.score == 100
    assert result.confidence == pytest.approx(0.35 / 7)
    assert list(result.factors) == ["recent_response"]
    assert "MISSING_FACTORS" in result.reason_codes


@pytest.mark.parametrize(
    ("quality", "want"),
    [("complete", 2 / 7), ("partial", 1 / 7), ("stale", 1 / 7), ("unavailable", 0)],
)
def test_confidence_reflects_data_quality(quality: str, want: float) -> None:
    result = calculate_weakness(replace(full(), quality=quality), AT)
    assert result.confidence == pytest.approx(want)


def test_new_words_are_marked_and_not_automatically_high_risk() -> None:
    result = calculate_weakness(replace(full(), observations=(day(1), day(0, new=True))), AT)
    assert result.score == 0
    assert result.is_new is True
    assert "NEW_WORD" in result.reason_codes
    assert result.risk_level == "low"


def test_old_word_study_count_is_normalized_by_age_and_weighted_error_rate() -> None:
    observations = (day(14, "VAGUE"), day(0))
    old = calculate_weakness(replace(full(), observations=observations, study_count=10), AT)
    young = calculate_weakness(
        replace(
            full(), observations=observations, study_count=10, added_at=AT - timedelta(days=10)
        ),
        AT,
    )
    # Recent .5*.25=.125. Weighted error .5/2=.25; old multiplier1.1, young2.
    assert old.score == pytest.approx(11.25)
    assert young.score == pytest.approx(16.875)
    assert calculate_weakness(replace(full(), study_count=10000), AT).score == 0


def test_one_forget_does_not_create_sticking_classification() -> None:
    result = calculate_weakness(replace(full(), observations=(day(0, "FORGET"),)), AT)
    assert result.factors["sticking"].value == 0
    assert "STICKING" not in result.reason_codes
    assert "repeated_error" not in result.factors


def test_consecutive_familiar_responses_reduce_score_gradually() -> None:
    evidence = WeaknessEvidence(WORD, "apple", observations=(day(0, "FORGET", None),))
    first = calculate_weakness(evidence, AT)
    second = calculate_weakness(
        replace(
            evidence,
            observations=(
                day(0, "FORGET", None),
                DailyEvidence(
                    (AT + timedelta(days=1)).date(), AT + timedelta(days=1), "FAMILIAR", None, False
                ),
            ),
        ),
        AT + timedelta(days=1),
    )
    third = calculate_weakness(
        replace(
            evidence,
            observations=(
                day(0, "FORGET", None),
                DailyEvidence(
                    (AT + timedelta(days=1)).date(), AT + timedelta(days=1), "FAMILIAR", None, False
                ),
                DailyEvidence(
                    (AT + timedelta(days=2)).date(), AT + timedelta(days=2), "FAMILIAR", None, False
                ),
            ),
        ),
        AT + timedelta(days=2),
    )
    assert 0 < third.score < second.score < first.score
    assert second.factors["recent_response"].value > 0.9


def test_extreme_counts_and_overdue_pressure_clamp_all_factors_and_score() -> None:
    result = calculate_weakness(
        replace(
            full(),
            observations=(day(1, "FORGET", False), day(0, "FORGET", False)),
            tags=("STICKING",),
            study_count=100000,
            last_studied_at=AT - timedelta(days=100),
            next_study_at=AT - timedelta(days=100),
        ),
        AT,
    )
    assert result.score == 100
    assert all(0 <= factor.value <= 1 for factor in result.factors.values())
    assert result.risk_level == "high"


def test_future_and_unknown_observations_cannot_become_feedback_evidence() -> None:
    result = calculate_weakness(
        replace(full(), observations=(day(-1, "FORGET"), day(0, "FUTURE_FEEDBACK"))), AT
    )
    assert result.score == 0
    assert "repeated_error" not in result.factors
    assert "UNKNOWN_FEEDBACK" in result.reason_codes


def test_duplicate_learning_day_does_not_become_repeated_error() -> None:
    result = calculate_weakness(
        WeaknessEvidence(WORD, "apple", observations=(day(0, "FORGET"), day(0, "FORGET"))), AT
    )
    assert "repeated_error" not in result.factors
    assert "unfinished_difficulty" not in result.factors


def test_as_of_requires_aware_datetime_and_known_algorithm() -> None:
    with pytest.raises(ValueError, match="timezone"):
        calculate_weakness(full(), AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="version"):
        calculate_weakness(full(), AT, "unknown")


def test_weak_word_query_rejects_naive_and_reversed_ranges() -> None:
    with pytest.raises(ValueError):
        WeakWordQuery(as_of=AT.replace(tzinfo=None))
    with pytest.raises(ValueError):
        WeakWordQuery(as_of=AT, start=AT, end=AT - timedelta(days=1))
    with pytest.raises(ValueError):
        WeakWordQuery(as_of=AT, limit=0)


def test_cancel_well_familiar_only_affects_recent_not_repeated_error() -> None:
    result = calculate_weakness(
        replace(full(), observations=(day(1, "CANCEL_WELL_FAMILIAR"), day(0))), AT
    )
    assert result.factors["repeated_error"].value == 0


def test_record_feedback_history_decays_without_creating_error_days() -> None:
    evidence = replace(
        full(),
        observations=(),
        recent_responses=(
            RecentResponse("FORGET", AT - timedelta(days=7), AT - timedelta(days=7)),
            RecentResponse("FAMILIAR", AT, AT),
        ),
    )
    result = calculate_weakness(evidence, AT)
    # .5*35/(35+15+15)*100. Historical records do not enable25/10 factors.
    assert result.score == pytest.approx(26.923076923076923)
    assert "repeated_error" not in result.factors


def test_equivalent_timezone_and_overdue_interval_have_identical_output() -> None:
    from zoneinfo import ZoneInfo

    evidence = replace(
        full(), last_studied_at=AT - timedelta(days=10), next_study_at=AT - timedelta(days=3.5)
    )
    result = calculate_weakness(evidence, AT.astimezone(ZoneInfo("Asia/Shanghai")))
    assert result.score == 7.5  # overdue3.5/7 wins over short interval .5/7.
    assert result == calculate_weakness(evidence, AT)


def test_duplicate_record_polling_does_not_raise_evidence_coverage() -> None:
    result = calculate_weakness(
        WeaknessEvidence(
            WORD,
            "apple",
            quality="complete",
            recent_responses=(
                RecentResponse("FORGET", AT - timedelta(days=7), AT - timedelta(days=7)),
                RecentResponse("FORGET", AT - timedelta(days=7), AT),
            ),
        ),
        AT,
    )
    assert result.score == 50
    assert result.confidence == pytest.approx(0.35 / 7)


@pytest.mark.parametrize(
    ("tags", "interval", "want", "risk"), [((), 7, 40, "medium"), (("STICKING",), 0, 70, "high")]
)
def test_risk_level_boundaries_use_inclusive_thresholds(
    tags: tuple[str, ...],
    interval: int,
    want: float,
    risk: str,
) -> None:
    result = calculate_weakness(
        replace(
            full(),
            last_feedback="FORGET",
            tags=tags,
            next_study_at=AT + timedelta(days=interval),
            observations=(day(1, complete=False), day(0)),
        ),
        AT,
    )
    assert result.score == want
    assert result.risk_level == risk


def test_empty_evidence_reports_missing_factors_and_zero_confidence() -> None:
    result = calculate_weakness(WeaknessEvidence(WORD, "apple"), AT)
    assert result.score == 0
    assert result.confidence == 0
    assert result.evidence_from is None
    assert "MISSING_FACTORS" in result.reason_codes


def test_observed_period_saturates_confidence_at_seven_distinct_days() -> None:
    result = calculate_weakness(
        replace(full(), observations=tuple(day(ago) for ago in range(10))), AT
    )
    assert result.confidence == 1


def test_future_feedback_timestamp_is_not_an_error_day() -> None:
    future_feedback = replace(day(1, "FORGET"), feedback_at=AT + timedelta(days=1))
    result = calculate_weakness(replace(full(), observations=(future_feedback, day(0))), AT)
    assert result.score == 0
    assert "repeated_error" not in result.factors
