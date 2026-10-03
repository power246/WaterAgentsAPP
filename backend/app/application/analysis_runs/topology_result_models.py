from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    GraphCoverage,
    OutcomeCounts,
    PublicError,
    RunSummaryStatus,
    SessionResultReference,
    TopologyRunResultSummary,
    WaterbodySessionResultSummary,
)
from app.application.canonical_json import canonical_json_digest
from app.application.transcripts.models import TranscriptContentRef
from app.domain.common import NonEmptyString, UtcDatetime, ensure_unique_tuple
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel
from app.domain.inference.outcome_status import OutcomeStatus


class DependencyStatus(DomainEnum):
    USABLE = "usable"
    INSUFFICIENT = "insufficient"
    INVALID = "invalid"
    CANCELLED = "cancelled"


class WaterbodySchedulingStatus(DomainEnum):
    EXCLUDED = "excluded"
    WAITING = "waiting"
    READY = "ready"
    RUNNING = "running"
    TERMINAL = "terminal"


class StableSourceReference(DomainModel):
    run_id: UUID
    session_id: UUID | None = None
    item_id: UUID
    content_type: NonEmptyString
    content_schema_version: Annotated[StrictInt, Field(gt=0)]
    result_id: NonEmptyString | None = None
    cell_id: NonEmptyString | None = None
    window_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_reference_shape(self) -> Self:
        if self.result_id is not None and self.session_id is None:
            raise ValueError("result references require session_id")
        if self.cell_id is not None and (
            self.result_id is None or self.window_id is None
        ):
            raise ValueError("cell references require result_id and window_id")
        if self.window_id is not None and self.result_id is None:
            raise ValueError("window references require result_id")
        return self


class DependencyDischargeState(DomainModel):
    window_id: NonEmptyString
    outcome: OutcomeStatus


class DependencyOutcome(DomainModel):
    run_id: UUID
    target_comid: NonEmptyString
    session_id: UUID
    status: DependencyStatus
    result_ref: SessionResultReference | None = None
    result_summary: WaterbodySessionResultSummary | None = None
    usable_cell_refs: tuple[StableSourceReference, ...] = ()
    discharge_states: tuple[DependencyDischargeState, ...] = ()
    error_ref: SessionResultReference | None = None
    completed_at: UtcDatetime

    @field_validator("usable_cell_refs")
    @classmethod
    def canonicalize_usable_cells(
        cls,
        value: tuple[StableSourceReference, ...],
    ) -> tuple[StableSourceReference, ...]:
        ensure_unique_tuple(value, field_name="usable_cell_refs")
        return tuple(sorted(value, key=_source_reference_key))

    @field_validator("discharge_states")
    @classmethod
    def canonicalize_discharge_states(
        cls,
        value: tuple[DependencyDischargeState, ...],
    ) -> tuple[DependencyDischargeState, ...]:
        window_ids = tuple(item.window_id for item in value)
        ensure_unique_tuple(window_ids, field_name="discharge window IDs")
        return tuple(sorted(value, key=lambda item: item.window_id))

    @model_validator(mode="after")
    def validate_status_projection(self) -> Self:
        result_pair = (self.result_ref is not None, self.result_summary is not None)
        if result_pair[0] != result_pair[1]:
            raise ValueError("result_ref and result_summary must appear together")
        if self.result_ref is not None and self.result_ref.run_id != self.run_id:
            raise ValueError("dependency result must remain in the run")
        if self.error_ref is not None and self.error_ref.run_id != self.run_id:
            raise ValueError("dependency error must remain in the run")
        if self.result_summary is not None and (
            self.result_summary.target_comid != self.target_comid
        ):
            raise ValueError("dependency result must match target_comid")
        for reference in self.usable_cell_refs:
            if (
                reference.run_id != self.run_id
                or reference.session_id != self.session_id
                or reference.content_type != "inference_result"
                or reference.content_schema_version != 1
                or self.result_summary is None
                or reference.result_id != self.result_summary.result_id
                or reference.cell_id is None
            ):
                raise ValueError("usable cell reference is outside the dependency result")
        if self.status is DependencyStatus.USABLE:
            if not all(result_pair) or not self.usable_cell_refs or self.error_ref is not None:
                raise ValueError("usable dependency requires result and usable cells")
        elif self.status is DependencyStatus.INSUFFICIENT:
            if not all(result_pair) or self.usable_cell_refs or self.discharge_states:
                raise ValueError("insufficient dependency cannot expose evidence")
            if self.error_ref is not None:
                raise ValueError("insufficient dependency cannot expose an error")
        elif self.status is DependencyStatus.INVALID:
            if any(result_pair) or self.usable_cell_refs or self.discharge_states:
                raise ValueError("invalid dependency cannot expose result or evidence")
        elif any(result_pair) or self.usable_cell_refs or self.discharge_states or self.error_ref:
            raise ValueError("cancelled dependency cannot expose result, evidence, or error")
        return self


class WaterbodyDependency(DomainModel):
    upstream_comid: NonEmptyString
    upstream_session_id: UUID | None = None
    dependency_status: DependencyStatus
    result_ref: SessionResultReference | None = None
    usable_cell_refs: tuple[StableSourceReference, ...] = ()
    discharge_states: tuple[DependencyDischargeState, ...] = ()

    @field_validator("usable_cell_refs")
    @classmethod
    def canonicalize_usable_cells(
        cls,
        value: tuple[StableSourceReference, ...],
    ) -> tuple[StableSourceReference, ...]:
        ensure_unique_tuple(value, field_name="usable_cell_refs")
        return tuple(sorted(value, key=_source_reference_key))

    @field_validator("discharge_states")
    @classmethod
    def canonicalize_discharge_states(
        cls,
        value: tuple[DependencyDischargeState, ...],
    ) -> tuple[DependencyDischargeState, ...]:
        ensure_unique_tuple(
            tuple(item.window_id for item in value),
            field_name="discharge window IDs",
        )
        return tuple(sorted(value, key=lambda item: item.window_id))

    @model_validator(mode="after")
    def validate_status_projection(self) -> Self:
        if self.dependency_status is DependencyStatus.USABLE:
            if (
                self.upstream_session_id is None
                or self.result_ref is None
                or not self.usable_cell_refs
            ):
                raise ValueError("usable manifest dependency requires result evidence")
        elif self.result_ref is not None or self.usable_cell_refs or self.discharge_states:
            raise ValueError("non-usable manifest dependency cannot expose evidence")
        if self.dependency_status is DependencyStatus.INSUFFICIENT and (
            self.upstream_session_id is None
        ):
            raise ValueError("insufficient dependency requires its completed session")
        if any(
            reference.content_type != "inference_result"
            or reference.content_schema_version != 1
            or reference.result_id is None
            or reference.cell_id is None
            for reference in self.usable_cell_refs
        ):
            raise ValueError("manifest evidence must reference inference_result v1 cells")
        return self


class WaterbodyDependencyManifest(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    topology_ref: TranscriptContentRef
    dependencies: tuple[WaterbodyDependency, ...] = ()
    manifest_digest: NonEmptyString

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("dependencies")
    @classmethod
    def canonicalize_dependencies(
        cls,
        value: tuple[WaterbodyDependency, ...],
    ) -> tuple[WaterbodyDependency, ...]:
        ensure_unique_tuple(
            tuple(item.upstream_comid for item in value),
            field_name="dependency COMIDs",
        )
        return tuple(sorted(value, key=lambda item: item.upstream_comid))

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.topology_ref.run_id != self.run_id:
            raise ValueError("topology_ref must remain in the run")
        for dependency in self.dependencies:
            if dependency.upstream_comid == self.target_comid:
                raise ValueError("waterbody cannot depend on itself")
            if dependency.result_ref is not None and (
                dependency.result_ref.run_id != self.run_id
            ):
                raise ValueError("dependency result must remain in the run")
            if any(
                reference.run_id != self.run_id
                or reference.session_id != dependency.upstream_session_id
                for reference in dependency.usable_cell_refs
            ):
                raise ValueError("dependency evidence must remain in its session")
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"manifest_digest"})
        )
        if self.manifest_digest != expected:
            raise ValueError("manifest_digest must match the canonical manifest")
        return self


class WaterbodyRunEntry(DomainModel):
    graph_position: Annotated[StrictInt, Field(ge=0)]
    target_comid: NonEmptyString
    session_id: UUID
    dependency_status: DependencyStatus
    session_status: Literal[
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.CANCELLED,
    ]
    result_ref: SessionResultReference | None = None
    result_summary: WaterbodySessionResultSummary | None = None

    @model_validator(mode="after")
    def validate_result_projection(self) -> Self:
        result_pair = (self.result_ref is not None, self.result_summary is not None)
        if result_pair[0] != result_pair[1]:
            raise ValueError("result_ref and result_summary must appear together")
        has_result = self.dependency_status in {
            DependencyStatus.USABLE,
            DependencyStatus.INSUFFICIENT,
        }
        if has_result != all(result_pair):
            raise ValueError("dependency status must match result availability")
        if has_result and self.session_status is not AgentSessionStatus.COMPLETED:
            raise ValueError("waterbody results require a completed session")
        if self.dependency_status is DependencyStatus.INVALID and (
            self.session_status is not AgentSessionStatus.FAILED
        ):
            raise ValueError("invalid dependency requires a failed session")
        if self.dependency_status is DependencyStatus.CANCELLED and (
            self.session_status is not AgentSessionStatus.CANCELLED
        ):
            raise ValueError("cancelled dependency requires a cancelled session")
        if self.result_summary is not None and (
            self.result_summary.target_comid != self.target_comid
        ):
            raise ValueError("waterbody result summary must match target_comid")
        return self


class TopologySummaryResult(DomainModel):
    status: Literal[RunSummaryStatus.COMPLETED, RunSummaryStatus.FAILED]
    session_id: UUID
    result_ref: SessionResultReference | None = None
    public_error: PublicError | None = None

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        if self.status is RunSummaryStatus.COMPLETED:
            if self.result_ref is None or self.public_error is not None:
                raise ValueError("completed summary requires only result_ref")
        elif self.result_ref is not None or self.public_error is None:
            raise ValueError("failed summary requires only public_error")
        return self


class TopologyRunResult(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    analysis_context_ref: TranscriptContentRef
    topology_ref: TranscriptContentRef
    target_comid: NonEmptyString
    target_result_ref: SessionResultReference
    target_result_summary: WaterbodySessionResultSummary
    graph_coverage: GraphCoverage
    node_outcomes: Annotated[tuple[WaterbodyRunEntry, ...], Field(min_length=1)]
    outcome_counts: OutcomeCounts
    summary: TopologySummaryResult
    generated_at: UtcDatetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("node_outcomes")
    @classmethod
    def canonicalize_node_outcomes(
        cls,
        value: tuple[WaterbodyRunEntry, ...],
    ) -> tuple[WaterbodyRunEntry, ...]:
        ensure_unique_tuple(
            tuple(item.target_comid for item in value),
            field_name="active COMIDs",
        )
        ensure_unique_tuple(
            tuple(item.graph_position for item in value),
            field_name="graph positions",
        )
        ordered = tuple(sorted(value, key=lambda item: item.graph_position))
        if tuple(item.graph_position for item in ordered) != tuple(range(len(ordered))):
            raise ValueError("graph positions must be contiguous from zero")
        return ordered

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        references = (
            self.analysis_context_ref,
            self.topology_ref,
        )
        if any(reference.run_id != self.run_id for reference in references):
            raise ValueError("topology result references must remain in the run")
        if self.analysis_context_ref.item_id == self.topology_ref.item_id:
            raise ValueError("analysis context and topology refs must be distinct")
        if self.target_result_ref.run_id != self.run_id:
            raise ValueError("target result must remain in the run")
        target_entries = tuple(
            item for item in self.node_outcomes if item.target_comid == self.target_comid
        )
        if len(target_entries) != 1:
            raise ValueError("node outcomes require exactly one target")
        target = target_entries[0]
        if target.graph_position != len(self.node_outcomes) - 1:
            raise ValueError("target must be the final stable graph entry")
        if (
            target.result_ref != self.target_result_ref
            or target.result_summary != self.target_result_summary
        ):
            raise ValueError("target projection must match its node outcome")
        for entry in self.node_outcomes:
            if entry.result_ref is not None and entry.result_ref.run_id != self.run_id:
                raise ValueError("node result must remain in the run")
        actual_counts = OutcomeCounts(
            usable=sum(
                item.dependency_status is DependencyStatus.USABLE
                for item in self.node_outcomes
            ),
            insufficient=sum(
                item.dependency_status is DependencyStatus.INSUFFICIENT
                for item in self.node_outcomes
            ),
            invalid=sum(
                item.dependency_status is DependencyStatus.INVALID
                for item in self.node_outcomes
            ),
            cancelled=sum(
                item.dependency_status is DependencyStatus.CANCELLED
                for item in self.node_outcomes
            ),
        )
        if self.outcome_counts != actual_counts:
            raise ValueError("outcome_counts must match node_outcomes")
        expected_coverage = (
            GraphCoverage.PARTIAL
            if actual_counts.invalid or actual_counts.cancelled
            else GraphCoverage.COMPLETE
        )
        if self.graph_coverage is not expected_coverage:
            raise ValueError("graph_coverage must match terminal node outcomes")
        if self.summary.result_ref is not None and (
            self.summary.result_ref.run_id != self.run_id
        ):
            raise ValueError("summary result must remain in the run")
        return self


class RunSummaryProjection(DomainModel):
    status: RunSummaryStatus = RunSummaryStatus.NOT_STARTED
    session_id: UUID | None = None
    result_ref: SessionResultReference | None = None
    error: PublicError | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status is RunSummaryStatus.NOT_STARTED:
            if self.session_id is not None or self.result_ref is not None or self.error:
                raise ValueError("not-started summary cannot have session or result")
        elif self.status is RunSummaryStatus.RUNNING:
            if self.session_id is None or self.result_ref is not None or self.error:
                raise ValueError("running summary requires only session_id")
        elif self.status is RunSummaryStatus.COMPLETED:
            if self.session_id is None or self.result_ref is None or self.error:
                raise ValueError("completed summary requires session and result")
        elif self.status is RunSummaryStatus.FAILED:
            if self.session_id is None or self.result_ref is not None or self.error is None:
                raise ValueError("failed summary requires session and public error")
        elif self.result_ref is not None or self.error is not None:
            raise ValueError("cancelled summary cannot have result or error")
        return self


class GraphOutcomeProjection(DomainModel):
    coverage: GraphCoverage | None = None
    usable_nodes: Annotated[StrictInt, Field(ge=0)] = 0
    insufficient_nodes: Annotated[StrictInt, Field(ge=0)] = 0
    invalid_nodes: Annotated[StrictInt, Field(ge=0)] = 0
    cancelled_nodes: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        total = (
            self.usable_nodes
            + self.insufficient_nodes
            + self.invalid_nodes
            + self.cancelled_nodes
        )
        if (self.coverage is None) != (total == 0):
            raise ValueError("graph coverage must appear exactly when outcomes exist")
        return self


def _source_reference_key(reference: StableSourceReference) -> tuple[str, ...]:
    return (
        str(reference.run_id),
        str(reference.session_id or ""),
        str(reference.item_id),
        reference.content_type,
        str(reference.content_schema_version),
        reference.result_id or "",
        reference.cell_id or "",
        reference.window_id or "",
    )
