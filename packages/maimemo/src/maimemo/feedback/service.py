"""Transactional append-only feedback; only explicit evidence can be recorded."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maimemo.api_client.models import Vocabulary as UpstreamVocabulary
from maimemo.feedback.models import (
    FeedbackEventView,
    FeedbackQuery,
    RecordFeedbackCommand,
    RetractFeedbackCommand,
    normalize_spelling,
)
from maimemo.storage.models.feedback import LearningFeedbackEvent
from maimemo.storage.repositories import FeedbackRepository

VocabularyResolver = Callable[[str], Awaitable[UpstreamVocabulary | None]]


class FeedbackConflictError(ValueError):
    """An idempotency key or retraction conflicts with retained evidence."""


class FeedbackService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: VocabularyResolver | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver

    @staticmethod
    def _replay(
        event: LearningFeedbackEvent,
        command: RecordFeedbackCommand | RetractFeedbackCommand,
    ) -> FeedbackEventView:
        payload = command.model_dump(exclude={"explicit_confirmation", "event_id"})
        payload["event_type"] = (
            "CONFUSION" if isinstance(command, RecordFeedbackCommand) else "RETRACTION"
        )
        payload["retracted_event_id"] = (
            command.event_id if isinstance(command, RetractFeedbackCommand) else None
        )
        if any(getattr(event, key) != value for key, value in payload.items()):
            raise FeedbackConflictError("Idempotency key was already used for a different payload")
        return FeedbackEventView.model_validate(event)

    async def _resolve(self, repository: FeedbackRepository, spelling: str) -> UUID | None:
        try:
            async with repository.session.begin_nested():
                local = await repository.find_word(normalize_spelling(spelling))
        except Exception:
            # Recover failed optional lookup without poisoning the event transaction.
            return None
        if local is not None:
            return local
        if self.resolver is None:
            return None
        try:
            remote = await self.resolver(spelling)
        except Exception:
            # Optional upstream resolution must not erase explicit local evidence.
            return None
        if (
            remote is None
            or not remote.id.strip()
            or normalize_spelling(remote.spelling) != normalize_spelling(spelling)
        ):
            return None
        try:
            async with repository.session.begin_nested():
                return await repository.retain_word(remote)
        except Exception:
            return None

    async def record(self, command: RecordFeedbackCommand) -> FeedbackEventView:
        command = RecordFeedbackCommand.model_validate(command.model_dump())
        async with self.session_factory() as session, session.begin():
            repository = FeedbackRepository(session)
            await repository.lock(f"key:{command.idempotency_key}")
            existing = await repository.by_key(command.idempotency_key)
            if existing is not None:
                return self._replay(existing, command)
            word_a = await self._resolve(repository, command.word_a_spelling)
            word_b = await self._resolve(repository, command.word_b_spelling)
            if word_a is not None and word_a == word_b:
                raise FeedbackConflictError(
                    "Both endpoints resolve to the same vocabulary identity"
                )
            event = await repository.append(
                **command.model_dump(exclude={"explicit_confirmation"}),
                event_type="CONFUSION",
                word_a_id=word_a,
                word_b_id=word_b,
                word_a_status="RESOLVED" if word_a is not None else "UNRESOLVED",
                word_b_status="RESOLVED" if word_b is not None else "UNRESOLVED",
            )
            return self._replay(event, command)

    async def retract(self, command: RetractFeedbackCommand) -> FeedbackEventView:
        command = RetractFeedbackCommand.model_validate(command.model_dump())
        async with self.session_factory() as session, session.begin():
            repository = FeedbackRepository(session)
            await repository.lock(f"key:{command.idempotency_key}")
            existing = await repository.by_key(command.idempotency_key)
            if existing is not None:
                return self._replay(existing, command)
            await repository.lock(f"target:{command.event_id}")
            target = await repository.get(command.event_id)
            if target is None or target.event_type != "CONFUSION":
                raise FeedbackConflictError("Retraction target must be an existing confusion event")
            if await repository.is_retracted(command.event_id):
                raise FeedbackConflictError("Feedback event was already retracted")
            event = await repository.append(
                **command.model_dump(exclude={"event_id"}),
                event_type="RETRACTION",
                retracted_event_id=command.event_id,
            )
            return self._replay(event, command)

    async def list_active(self, query: FeedbackQuery) -> list[FeedbackEventView]:
        query = FeedbackQuery.model_validate(query.model_dump())
        async with self.session_factory() as session:
            return [
                FeedbackEventView.model_validate(event)
                for event in await FeedbackRepository(session).active(query)
            ]
