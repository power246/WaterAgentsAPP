from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
    WaterbodyDependency,
    WaterbodyDependencyManifest,
)
from app.application.canonical_json import canonical_json_digest
from app.application.evidence_tools.errors import (
    EvidenceProducerUnavailableError,
    EvidenceScopeViolationError,
)
from app.application.evidence_tools.upstream_projection import (
    ResolvedInferenceResult,
    UpstreamInferenceResultReader,
    project_upstream_results,
    validate_resolved_outcome,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    HydrologyGraph,
    HydrologyParticipation,
    ProviderCollectionBatch,
    UpstreamResultRecord,
    UpstreamResultRequest,
)


class OutcomeAlreadyPublishedError(RuntimeError):
    pass


class OutcomeIdentityError(EvidenceScopeViolationError):
    pass


class OutcomesNotReadyError(EvidenceProducerUnavailableError):
    pass


@dataclass(frozen=True, slots=True)
class _PublishedOutcome:
    outcome: DependencyOutcome
    resolved: ResolvedInferenceResult | None


class RunLocalUpstreamResultStore:
    """Immutable terminal outcomes and the M5 direct-upstream provider seam."""

    def __init__(
        self,
        *,
        run_id: UUID,
        topology: HydrologyGraph,
        topology_ref: TranscriptContentRef,
        result_reader: UpstreamInferenceResultReader,
    ) -> None:
        if topology_ref.run_id != run_id:
            raise OutcomeIdentityError()
        self._run_id = run_id
        self._topology_ref = topology_ref
        self._result_reader = result_reader
        self._active_comids = frozenset(
            node.comid
            for node in topology.nodes
            if node.participation is HydrologyParticipation.ACTIVE
        )
        self._direct_upstreams = {
            comid: tuple(sorted(
                edge.upstream_comid
                for edge in topology.edges
                if edge.participating and edge.downstream_comid == comid
            ))
            for comid in self._active_comids
        }
        self._outcomes: dict[str, _PublishedOutcome] = {}
        self._session_owners: dict[UUID, str] = {}
        self._result_owners: dict[UUID, str] = {}
        self._closed = False
        self._lock = asyncio.Lock()

    async def publish(self, outcome: DependencyOutcome) -> None:
        async with self._lock:
            self._ensure_open()
            if outcome.run_id != self._run_id or outcome.target_comid not in (
                self._active_comids
            ):
                raise OutcomeIdentityError()
            if outcome.target_comid in self._outcomes:
                raise OutcomeAlreadyPublishedError(
                    f"outcome already published for COMID {outcome.target_comid}"
                )
            if owner := self._session_owners.get(outcome.session_id):
                raise OutcomeIdentityError(
                    f"session is already owned by COMID {owner}"
                )
            if outcome.result_ref is not None:
                if owner := self._result_owners.get(outcome.result_ref.item_id):
                    raise OutcomeIdentityError(
                        f"result is already owned by COMID {owner}"
                    )

            resolved = None
            if outcome.status in {
                DependencyStatus.USABLE,
                DependencyStatus.INSUFFICIENT,
            }:
                try:
                    resolved = await self._result_reader.resolve(outcome)
                    validate_resolved_outcome(outcome, resolved)
                except (Exception, asyncio.CancelledError) as error:
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise OutcomeIdentityError() from error

            self._outcomes[outcome.target_comid] = _PublishedOutcome(
                outcome=outcome,
                resolved=resolved,
            )
            self._session_owners[outcome.session_id] = outcome.target_comid
            if outcome.result_ref is not None:
                self._result_owners[outcome.result_ref.item_id] = outcome.target_comid

    async def get(self, target_comid: str) -> DependencyOutcome | None:
        async with self._lock:
            self._ensure_open()
            published = self._outcomes.get(target_comid)
            return published.outcome if published is not None else None

    async def build_manifest(
        self,
        *,
        session_id: UUID,
        target_comid: str,
    ) -> WaterbodyDependencyManifest:
        async with self._lock:
            self._ensure_open()
            dependencies = self._required_dependencies(target_comid)
            published = self._published_dependencies(dependencies)
            values = {
                "schema_version": 1,
                "run_id": self._run_id,
                "session_id": session_id,
                "target_comid": target_comid,
                "topology_ref": self._topology_ref,
                "dependencies": tuple(
                    _manifest_dependency(item.outcome) for item in published
                ),
            }
            digest = canonical_json_digest(values)
            return WaterbodyDependencyManifest(
                **values,
                manifest_digest=digest,
            )

    async def collect(
        self,
        request: UpstreamResultRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[UpstreamResultRecord]:
        del deadline
        async with self._lock:
            self._ensure_open()
            if request.run_id != str(self._run_id):
                raise OutcomeIdentityError()
            dependencies = self._required_dependencies(request.agent_comid)
            if request.direct_upstream_comids != dependencies:
                raise OutcomeIdentityError()
            published = self._published_dependencies(dependencies)
            usable = tuple(
                (item.outcome, item.resolved)
                for item in published
                if item.outcome.status is DependencyStatus.USABLE
                and item.resolved is not None
            )
            return project_upstream_results(request, usable)

    async def cleanup_run(self, run_id: UUID | str) -> None:
        async with self._lock:
            if str(run_id) != str(self._run_id):
                raise OutcomeIdentityError()
            self._outcomes.clear()
            self._session_owners.clear()
            self._result_owners.clear()
            self._closed = True

    def _required_dependencies(self, target_comid: str) -> tuple[str, ...]:
        if target_comid not in self._active_comids:
            raise OutcomeIdentityError()
        return self._direct_upstreams[target_comid]

    def _published_dependencies(
        self,
        dependencies: tuple[str, ...],
    ) -> tuple[_PublishedOutcome, ...]:
        missing = tuple(
            comid for comid in dependencies if comid not in self._outcomes
        )
        if missing:
            raise OutcomesNotReadyError()
        return tuple(self._outcomes[comid] for comid in dependencies)

    def _ensure_open(self) -> None:
        if self._closed:
            raise OutcomesNotReadyError()


def _manifest_dependency(outcome: DependencyOutcome) -> WaterbodyDependency:
    usable = outcome.status is DependencyStatus.USABLE
    return WaterbodyDependency(
        upstream_comid=outcome.target_comid,
        upstream_session_id=outcome.session_id,
        dependency_status=outcome.status,
        result_ref=outcome.result_ref if usable else None,
        usable_cell_refs=outcome.usable_cell_refs if usable else (),
        discharge_states=outcome.discharge_states if usable else (),
    )
