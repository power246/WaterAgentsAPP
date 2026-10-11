from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import (
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.evidence.collection_summary import CollectionSummary
from app.domain.evidence.evidence_cell import EvidenceCell
from app.domain.evidence.evidence_cell_role import EvidenceCellRole
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.item_reference import ItemReference
from app.domain.evidence.quality_summary import QualitySummary
from app.domain.evidence.reference_scope import ReferenceScope
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.time.timezone import get_iana_timezone


class EvidenceBundle(DomainModel):
    schema_version: Literal[1] = 1
    bundle_id: NonEmptyString
    agent_comid: NonEmptyString
    context_ref: ItemReference
    topology_ref: ItemReference
    time_range: TimeRange
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    requested_parameters: tuple[ParameterKey, ...]
    supporting_parameters: tuple[ParameterKey, ...]
    window_refs: tuple[NonEmptyString, ...]
    cells: tuple[EvidenceCell, ...]
    fragment_refs: tuple[EvidenceFragmentReference, ...]
    source_record_item_refs: tuple[NonEmptyString, ...] = ()
    closure_refs: tuple[ItemReference, ...] = ()
    tool_result_refs: tuple[ItemReference, ...] = ()
    quality_summary: QualitySummary
    collection_summary: CollectionSummary
    created_at: UtcDatetime

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @field_validator("requested_parameters")
    @classmethod
    def validate_requested_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        if not value:
            raise ValueError("bundle requested parameters must not be empty")
        return ensure_unique_tuple(value, field_name="bundle parameters")

    @field_validator("supporting_parameters")
    @classmethod
    def validate_supporting_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        return ensure_unique_tuple(value, field_name="bundle parameters")

    @field_validator("window_refs")
    @classmethod
    def validate_window_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("window_refs must not be empty")
        return ensure_unique_tuple(value, field_name="window_refs")

    @field_validator("source_record_item_refs", "closure_refs", "tool_result_refs")
    @classmethod
    def validate_unique_references(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="bundle references")

    @model_validator(mode="after")
    def validate_reference_closure(self) -> Self:
        if not self.window_refs:
            raise ValueError("window_refs must not be empty")
        overlap = set(self.requested_parameters) & set(self.supporting_parameters)
        if overlap:
            raise ValueError("requested and supporting parameters must be disjoint")

        reference_roles = (
            ("context", {self.context_ref.item_id}),
            ("topology", {self.topology_ref.item_id}),
            ("fragment", {item.item_id for item in self.fragment_refs}),
            ("source_record", set(self.source_record_item_refs)),
            ("closure", {item.item_id for item in self.closure_refs}),
            ("tool_result", {item.item_id for item in self.tool_result_refs}),
        )
        for index, (left_name, left_items) in enumerate(reference_roles):
            for right_name, right_items in reference_roles[index + 1 :]:
                if left_items & right_items:
                    raise ValueError(
                        f"bundle item refs cannot have both {left_name} and {right_name} roles"
                    )

        expected_cells = {
            (parameter, window_id, EvidenceCellRole.TARGET)
            for parameter in self.requested_parameters
            for window_id in self.window_refs
        } | {
            (parameter, window_id, EvidenceCellRole.REQUIRED_SUPPORT)
            for parameter in self.supporting_parameters
            for window_id in self.window_refs
        }
        actual_cells = tuple(
            (cell.parameter, cell.window_id, cell.role) for cell in self.cells
        )
        ensure_unique_tuple(actual_cells, field_name="bundle cell identities")
        if set(actual_cells) != expected_cells:
            raise ValueError(
                "cells must contain exactly one parameter/window/role entry"
            )

        fragment_ids = tuple(reference.evidence_id for reference in self.fragment_refs)
        ensure_unique_tuple(fragment_ids, field_name="fragment evidence IDs")
        cell_fragment_ids = {
            evidence_id for cell in self.cells for evidence_id in cell.fragment_ids()
        }
        if cell_fragment_ids != set(fragment_ids):
            raise ValueError("bundle fragment reference closure is incomplete")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        item_ids = [self.context_ref.item_id, self.topology_ref.item_id]
        item_ids.extend(reference.item_id for reference in self.fragment_refs)
        item_ids.extend(self.source_record_item_refs)
        item_ids.extend(reference.item_id for reference in self.closure_refs)
        item_ids.extend(reference.item_id for reference in self.tool_result_refs)
        return tuple(dict.fromkeys(item_ids))

    def validate_reference_scope(
        self,
        *,
        run_id: str,
        session_id: str,
        resolved_scopes: tuple[ReferenceScope, ...],
        same_run_session_item_ids: tuple[str, ...] = (),
    ) -> None:
        scopes_by_item = {scope.item_id: scope for scope in resolved_scopes}
        if len(scopes_by_item) != len(resolved_scopes):
            raise ValueError("resolved reference scopes must be unique by item_id")
        expected_items = set(self.referenced_item_ids())
        if set(scopes_by_item) != expected_items:
            raise ValueError("resolved reference scopes must cover bundle refs exactly")
        same_run_items = set(same_run_session_item_ids)
        if not same_run_items <= expected_items:
            raise ValueError("same-run session refs must belong to the bundle closure")
        run_global_items = {self.context_ref.item_id, self.topology_ref.item_id}
        for item_id, scope in scopes_by_item.items():
            if scope.run_id != run_id:
                raise ValueError("bundle references must belong to the same run")
            if item_id in run_global_items:
                if scope.session_id not in {None, session_id}:
                    raise ValueError("run-global reference has incompatible session scope")
            elif item_id in same_run_items:
                if scope.session_id is None:
                    raise ValueError("same-run related reference must be session-owned")
            elif scope.session_id != session_id:
                raise ValueError("bundle references must belong to the same session")
