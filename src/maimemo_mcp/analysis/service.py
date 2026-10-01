"""Transactional score persistence and user-facing explanations over local history."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maimemo_mcp.analysis.models import WeaknessFactor, WeaknessResult, WeakWordQuery
from maimemo_mcp.analysis.scoring import calculate_weakness
from maimemo_mcp.ingestion.normalizers import utc_instant
from maimemo_mcp.storage.repositories import StudyHistoryRepository

REASONS = {
    "RECENT_ERROR": "已观测的近期反馈存在忘记或模糊，影响随时间衰减。",
    "REPEATED_ERROR": "不同有效学习日出现失误，学习次数已按加入时长校正。",
    "STICKING": "墨墨学习记录明确标记为顽固词。",
    "INTERVAL_PRESSURE": "最近复习间隔较短或复习计划已经逾期。",
    "REPEATED_UNFINISHED": "至少两个观测学习日的列表状态显示未完成。",
    "NEW_WORD": "墨墨当日列表标记为新词，此标记本身不增加风险分。",
    "MISSING_FACTORS": "部分评分因子缺失，已按可用权重归一并降低置信度。",
    "UNKNOWN_FEEDBACK": "存在无法识别的反馈，该反馈未计入失误证据。",
    "DATA_QUALITY_PARTIAL": "历史采集证据不完整，置信度已降低。",
    "DATA_QUALITY_STALE": "历史采集证据已过期，置信度已降低。",
    "DATA_QUALITY_UNAVAILABLE": "采集健康信息不可用，不能确认评分证据质量。",
}


def explain_weakness(result: WeaknessResult) -> tuple[str, ...]:
    return tuple(REASONS[code] for code in result.reason_codes if code in REASONS)


class WeaknessService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        version: str = "weakness-v1",
    ) -> None:
        if version != "weakness-v1":
            raise ValueError("Unsupported algorithm version")
        self.session_factory = session_factory
        self.version = version

    async def recalculate(self, as_of: datetime) -> int:
        at = utc_instant(as_of)
        async with self.session_factory() as session, session.begin():
            repository = StudyHistoryRepository(session)
            evidence = await repository.weakness_evidence(at)
            for word in evidence:
                await repository.save_weakness(calculate_weakness(word, at, self.version))
            return len(evidence)

    async def list_weak_words(
        self,
        query: WeakWordQuery,
        *,
        vocabulary_ids: Sequence[UUID] | None = None,
    ) -> list[WeaknessResult]:
        async with self.session_factory() as session:
            scores = await StudyHistoryRepository(session).weak_words(
                query,
                self.version,
                vocabulary_ids=vocabulary_ids,
            )
            results = []
            for row in scores:
                payload = row.factors
                result = WeaknessResult(
                    row.vocabulary_id,
                    payload["spelling"],
                    row.computed_at,
                    row.algorithm_version,
                    row.score,
                    cast(Literal["low", "medium", "high"], row.risk_level),
                    row.confidence,
                    {
                        name: WeaknessFactor(value["value"], value["weight"])
                        for name, value in payload["values"].items()
                    },
                    tuple(payload["reason_codes"]),
                    row.evidence_from,
                    row.evidence_through,
                    row.latest_snapshot_id,
                    payload["is_new"],
                )
                results.append(replace(result, reasons=explain_weakness(result)))
            return results
