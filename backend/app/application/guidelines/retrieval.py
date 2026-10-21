import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.application.analysis_runs.cancellation import await_with_cancellation
from app.application.analysis_runs.workflow import CancellationToken, WorkflowStopped
from app.application.guidelines.embedding import guideline_embedding_failure, validated_guideline_texts
from app.domain.guidelines.canonical_query import CanonicalGuidelineQueryV1
from app.domain.guidelines.embedding_batch import GuidelineEmbeddingBatch
from app.domain.guidelines.policy import GuidelinePolicy
from app.domain.guidelines import GuidelineMatch, GuidelineRetrievalResult, GuidelineRunPin
from app.domain.guidelines.canonical import normalize_text
from app.ports.clock import ClockPort
from app.ports.errors import GuidelineConflictError, InvalidPortResponseError, PortError

if TYPE_CHECKING:
    from app.application.reasoning.task import ValidatedWaterbodyAgentTask
    from app.domain import HydrologyGraph, ParameterCapabilityRegistry
    from app.application.query_understanding import AnalysisContext
    from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
    from app.ports.guidelines import GuidelineEmbeddingPort, GuidelineRepositoryPort


def build_guideline_query(context: "AnalysisContext", topology: "HydrologyGraph", target_comid: str,
                          profile: "GuidelineEmbeddingProfileV1", registry: "ParameterCapabilityRegistry"):
    if target_comid not in {node.comid for node in topology.nodes}:
        raise ValueError("guideline task is outside the validated topology")
    fields = dict(profile_id=profile.profile_id, instructions=profile.query_instructions,
                  parameter_keys=tuple(intent.key for intent in context.params_used), mode=context.mode)
    query = CanonicalGuidelineQueryV1(**fields)
    query.require_registered_parameters(registry)
    validated_guideline_texts(profile, (query,))
    reduced = False
    facts = (f"agent_comid={target_comid}", f"requested_target_comid={topology.target_comid}",
             f"direct_upstream_count={sum(edge.downstream_comid == target_comid for edge in topology.edges)}")
    for name, value in (("scenario_tags", (context.mode.value,)), ("task_facts", facts), ("question", context.query)):
        try:
            candidate = CanonicalGuidelineQueryV1(**{**fields, name: normalize_text(value) if name == "question" else value,
                                                   "input_reduced": reduced})
            validated_guideline_texts(profile, (candidate,))
        except ValueError:
            reduced = True
        else:
            fields[name] = getattr(candidate, name)
    return CanonicalGuidelineQueryV1(**fields, input_reduced=reduced)


def fit_guideline_selection(result: GuidelineRetrievalResult,
                            fits: Callable[[tuple[GuidelineMatch, ...]], bool]) -> GuidelineRetrievalResult:
    if result.status != "retrieved":
        return result
    matches = result.matches
    while matches and not fits(matches):
        matches = matches[:-1]
    return GuidelineRetrievalResult(status="retrieved" if matches else "budget_limited", matches=matches,
        query_digest=result.query_digest, input_reduced=result.input_reduced)


class GuidelineRunContext:
    def __init__(self, pin: GuidelineRunPin, *, repository, embedding, registry, policy, clock):
        self.pin = pin
        self._repository = repository
        self._embedding = embedding
        self._registry = registry
        self._policy = policy
        self._clock = clock
        self._record_lock = asyncio.Lock()
        self._record_ref = None

    async def save(self, operations):
        from app.application.guidelines.transcript import write_run_context

        async with self._record_lock:
            if self._record_ref is None:
                self._record_ref = await write_run_context(operations, self.pin)
            return self._record_ref

    @classmethod
    async def prepare(cls, run_id: UUID, *, repository: "GuidelineRepositoryPort | None",
                      embedding: "GuidelineEmbeddingPort | None", profile: "GuidelineEmbeddingProfileV1 | None",
                      registry: "ParameterCapabilityRegistry", policy: GuidelinePolicy, clock: ClockPort,
                      cancellation: CancellationToken):
        cancellation.checkpoint()
        catalog = None
        code = "guideline_embedding_configuration_invalid"
        if repository is not None and embedding is not None and profile is not None:
            try:
                catalog = await await_with_cancellation(repository.read_catalog(), cancellation,
                    timeout=policy.online_timeout_seconds)
                if catalog.profile != profile:
                    code = "guideline_snapshot_unavailable"
                else:
                    code = None
            except (PortError, ValueError, TimeoutError):
                code = "guideline_snapshot_unavailable"
        cancellation.checkpoint()
        pin = GuidelineRunPin(run_id=run_id, status="ready" if code is None else "unavailable",
                              catalog=catalog, safe_error_code=code)
        return cls(pin, repository=repository, embedding=embedding, registry=registry, policy=policy, clock=clock)

    def session(self, task: "ValidatedWaterbodyAgentTask", *, deadline: datetime):
        if task.task.run_id != self.pin.run_id:
            raise ValueError("guideline session must belong to the pinned run")
        query = None
        code = self.pin.safe_error_code
        if self.pin.status == "ready":
            try:
                query = build_guideline_query(task.context, task.topology, task.task.target_comid,
                                             self.pin.catalog.profile, self._registry)
            except ValueError:
                code = "guideline_embedding_configuration_invalid"
        session = GuidelineSessionKnowledge(self.pin, task.task.session_id, query, self._repository, self._embedding,
            self._policy, self._clock, deadline, task.task.cancellation_token, code)
        session.run_context = self
        session.original_question = normalize_text(task.context.query)
        return session


class GuidelineSessionKnowledge:
    def __init__(self, pin, session_id, query, repository, embedding, policy, clock,
                 deadline: datetime, cancellation: CancellationToken, error_code=None):
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("guideline session deadline must be timezone-aware")
        self.pin = pin
        self.session_id = session_id
        self._query = query
        self._repository = repository
        self._embedding = embedding
        self._policy = policy
        self._clock = clock
        self._deadline = deadline
        self._cancellation = cancellation
        self._error_code = error_code
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def policy(self) -> GuidelinePolicy:
        return self._policy

    async def get_or_load(self) -> GuidelineRetrievalResult:
        if self._closed:
            return self._result("unavailable", "guideline_session_closed")
        if self._cancellation.is_requested():
            return self._result("cancelled", "guideline_retrieval_cancelled")
        if self._task is None:
            self._task = asyncio.create_task(self._load(), name="guideline-session-retrieval")
        return await asyncio.shield(self._task)

    async def availability(self) -> GuidelineRetrievalResult | None:
        """Check the pinned catalog without sending an embedding request."""
        return await self._load(check_only=True)

    async def search(self, question: str, *, deadline: datetime | None = None) -> GuidelineRetrievalResult:
        if deadline is not None:
            self._deadline = min(self._deadline, deadline)
        if self._task is not None:
            return await self.get_or_load()
        if self._query is None or self.pin.status != "ready":
            return self._result("unavailable", self._error_code)
        candidate = CanonicalGuidelineQueryV1.model_validate(self._query.model_dump() | {"question": question})
        validated_guideline_texts(self.pin.catalog.profile, (candidate,))
        self._query = candidate
        return await self.get_or_load()

    def _result(self, status, code=None, matches=()):
        return GuidelineRetrievalResult(status=status, safe_error_code=code, matches=matches,
            query_digest=self._query.canonical_digest if self._query is not None else None,
            input_reduced=self._query.input_reduced if self._query is not None else False)

    async def _load(self, *, check_only=False):
        if self._closed:
            return self._result("unavailable", "guideline_session_closed")
        if self._cancellation.is_requested():
            return self._result("cancelled", "guideline_retrieval_cancelled")
        remaining = min(self._policy.online_timeout_seconds, (self._deadline - self._clock.now()).total_seconds())
        if remaining <= 0:
            return self._result("unavailable", "guideline_retrieval_deadline_exceeded")
        if self.pin.status != "ready" or self._query is None:
            return self._result("unavailable", self._error_code)
        deadline = asyncio.get_running_loop().time() + remaining
        try:
            return await await_with_cancellation(self._retrieve(deadline, check_only=check_only), self._cancellation, timeout=remaining)
        except WorkflowStopped:
            return self._result("cancelled", "guideline_retrieval_cancelled")
        except TimeoutError:
            return self._result("unavailable", "guideline_retrieval_deadline_exceeded")
        except GuidelineConflictError:
            return self._result("unavailable", "guideline_snapshot_unavailable")
        except (PortError, ValueError) as error:
            return self._result("unavailable", guideline_embedding_failure(error)[0])

    def _checkpoint(self, deadline):
        self._cancellation.checkpoint()
        if self._clock.now() >= self._deadline or asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError()

    async def _retrieve(self, deadline, *, check_only=False):
        self._checkpoint(deadline)
        catalog, query = self.pin.catalog, self._query
        library, candidates = await self._repository.retrieval_counts(catalog, query.parameter_keys)
        self._checkpoint(deadline)
        if library == 0:
            return self._result("empty_library")
        if candidates == 0:
            return self._result("no_match")
        if check_only:
            return None
        batch = await self._embedding.embed(catalog.profile, (query,), deadline, self._cancellation)
        self._checkpoint(deadline)
        if batch.profile != catalog.profile or len(batch.vectors) != 1:
            raise InvalidPortResponseError("Guideline query embedding profile/count is invalid.")
        try:
            vector = GuidelineEmbeddingBatch.model_validate(batch.model_dump()).vectors[0]
        except ValueError:
            raise InvalidPortResponseError("Guideline query embedding vector is invalid.") from None
        matches = await self._repository.rank_guidelines(catalog, query.parameter_keys, vector,
            cutoff=self._policy.semantic_min_similarity, limit=self._policy.top_k)
        self._checkpoint(deadline)
        return self._result("retrieved" if matches else "no_match", matches=matches)

    async def aclose(self):
        self._closed = True
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._query = None
