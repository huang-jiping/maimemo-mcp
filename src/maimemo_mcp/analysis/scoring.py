"""Approved weakness-v1: explicit time, evidence coverage and five fixed weights."""

from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from maimemo_mcp.analysis.models import (
    DailyEvidence,
    WeaknessEvidence,
    WeaknessFactor,
    WeaknessResult,
)

WEIGHTS = {
    "recent_response": 35,
    "repeated_error": 25,
    "sticking": 15,
    "interval_pressure": 15,
    "unfinished_difficulty": 10,
}
SEVERITY = {
    "FORGET": 1.0,
    "VAGUE": 0.5,
    "FAMILIAR": 0.0,
    "WELL_FAMILIAR": 0.0,
    "CANCEL_WELL_FAMILIAR": 0.5,
}
QUALITY = {"complete": 1.0, "partial": 0.5, "stale": 0.5, "unavailable": 0.0}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Scoring requires timezone-aware datetime")
    return at.astimezone(UTC)


def _days(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def calculate_weakness(
    evidence: WeaknessEvidence,
    as_of: datetime,
    version: str = "weakness-v1",
) -> WeaknessResult:
    at = _aware(as_of)
    if version != "weakness-v1":
        raise ValueError("Unsupported algorithm version")
    if evidence.quality not in QUALITY:
        raise ValueError("Unsupported evidence quality")
    daily: dict[date, DailyEvidence] = {}
    for observation in evidence.observations:
        seen = _aware(observation.observed_at)
        if observation.feedback_at is not None:
            _aware(observation.feedback_at)
        if seen > at or observation.study_date > at.astimezone(SHANGHAI).date():
            continue
        previous = daily.get(observation.study_date)
        if previous is None or seen >= previous.observed_at:
            daily[observation.study_date] = observation
    rows = sorted(daily.values(), key=lambda row: (row.study_date, row.observed_at))
    record_visible = (
        evidence.record_observed_at is not None and _aware(evidence.record_observed_at) <= at
    )
    for instant in (evidence.added_at, evidence.last_studied_at, evidence.next_study_at):
        if instant is not None:
            _aware(instant)
    factors: dict[str, WeaknessFactor] = {}
    codes: list[str] = []

    def factor(name: str, value: float, code: str) -> None:
        factors[name] = WeaknessFactor(_clamp(value), WEIGHTS[name])
        if value > 0:
            codes.append(code)

    responses: list[tuple[str, datetime]] = []
    historical_instants: list[datetime] = []
    distinct_responses: dict[tuple[datetime, str], datetime] = {}
    for response in evidence.recent_responses:
        observed = _aware(response.observed_at)
        responded = _aware(response.responded_at)
        if observed <= at and responded <= at:
            key = (responded, response.feedback)
            distinct_responses[key] = min(observed, distinct_responses.get(key, observed))
    for (responded, feedback), observed in sorted(distinct_responses.items()):
        historical_instants.append(observed)
        if feedback in SEVERITY:
            responses.append((feedback, responded))
        else:
            codes.append("UNKNOWN_FEEDBACK")
    for row in rows:
        if row.feedback in SEVERITY:
            feedback_at = _aware(row.feedback_at or row.observed_at)
            if feedback_at <= at:
                assert row.feedback is not None
                responses.append((row.feedback, feedback_at))
        elif row.feedback is not None:
            codes.append("UNKNOWN_FEEDBACK")
    if record_visible and evidence.last_studied_at is not None:
        if evidence.last_feedback in SEVERITY and evidence.last_studied_at <= at:
            assert evidence.last_feedback is not None
            responses.append((evidence.last_feedback, _aware(evidence.last_studied_at)))
        elif evidence.last_feedback is not None and evidence.last_feedback not in SEVERITY:
            codes.append("UNKNOWN_FEEDBACK")
    if responses:
        recent = max(
            SEVERITY[response] * 2 ** (-_days(at, when) / 7) for response, when in responses
        )
        factor("recent_response", recent, "RECENT_ERROR")
    errors = [
        1.0 if row.feedback == "FORGET" else 0.5 if row.feedback == "VAGUE" else 0.0
        for row in rows
        if row.feedback in SEVERITY and _aware(row.feedback_at or row.observed_at) <= at
    ]
    if len(errors) >= 2:
        multiplier = 1.0
        if (
            record_visible
            and evidence.study_count is not None
            and evidence.added_at is not None
            and evidence.added_at <= at
        ):
            multiplier += _clamp(
                max(evidence.study_count, 0) / max(_days(at, evidence.added_at), 1)
            )
        factor("repeated_error", sum(errors) / len(errors) * multiplier, "REPEATED_ERROR")
    if record_visible and evidence.tags is not None:
        factor("sticking", float("STICKING" in evidence.tags), "STICKING")
    if (
        record_visible
        and evidence.last_studied_at is not None
        and evidence.next_study_at is not None
        and evidence.last_studied_at <= at
    ):
        interval = _days(evidence.next_study_at, evidence.last_studied_at)
        if interval >= 0:
            factor(
                "interval_pressure",
                max(_clamp((7 - interval) / 7), _clamp(_days(at, evidence.next_study_at) / 7)),
                "INTERVAL_PRESSURE",
            )
    completion = [row.is_complete for row in rows if row.is_complete is not None]
    if len(completion) >= 2:
        factor(
            "unfinished_difficulty",
            completion.count(False) / len(completion),
            "REPEATED_UNFINISHED",
        )
    is_new = next((row.is_new for row in reversed(rows) if row.is_new is not None), None)
    if is_new:
        codes.append("NEW_WORD")
    if len(factors) < len(WEIGHTS):
        codes.append("MISSING_FACTORS")
    if evidence.quality != "complete":
        codes.append("DATA_QUALITY_" + evidence.quality.upper())
    weight = sum(item.weight for item in factors.values())
    score = (
        100 * sum(item.weight * item.value for item in factors.values()) / weight if weight else 0.0
    )
    score = min(max(score, 0.0), 100.0)
    days = {row.study_date for row in rows}
    instants = [_aware(row.observed_at) for row in rows]
    instants.extend(
        _aware(row.feedback_at)
        for row in rows
        if row.feedback_at is not None and row.feedback_at <= at
    )
    instants.extend(historical_instants)
    days.update(seen.astimezone(SHANGHAI).date() for seen in historical_instants)
    if record_visible:
        assert evidence.record_observed_at is not None
        seen = _aware(evidence.record_observed_at)
        days.add(seen.astimezone(SHANGHAI).date())
        instants.append(seen)
    confidence = weight / 100 * min(len(days) / 7, 1.0) * QUALITY[evidence.quality]
    risk: Literal["low", "medium", "high"] = (
        "high" if score >= 70 else "medium" if score >= 40 else "low"
    )
    return WeaknessResult(
        evidence.vocabulary_id,
        evidence.spelling,
        at,
        version,
        score,
        risk,
        confidence,
        factors,
        tuple(dict.fromkeys(codes)),
        min(instants, default=None),
        max(instants, default=None),
        evidence.latest_snapshot_id,
        is_new,
    )
