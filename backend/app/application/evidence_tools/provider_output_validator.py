from app.application.analysis_runs.models import AgentRole, AgentSessionStatus
from app.application.evidence_tools.errors import (
    EvidenceProviderOutputInvalidError,
    EvidenceScopeViolationError,
)
from app.application.evidence_tools.models import (
    EvidenceCollectionInput,
    EvidenceToolReader,
)
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import UpstreamResultRecord


class ProviderOutputValidator:
    def __init__(self, reader: EvidenceToolReader) -> None:
        self._reader = reader

    async def validate(
        self,
        scope: EvidenceSessionScope,
        collection: EvidenceCollectionInput,
        *,
        expected_operation_id: str,
    ) -> None:
        if collection.batch.operation_id != expected_operation_id:
            raise EvidenceProviderOutputInvalidError()

        records = collection.batch.records
        is_upstream = (
            collection.provider == "analysis_run"
            and collection.channel == "upstream_result"
        )
        if is_upstream:
            if any(not isinstance(record, UpstreamResultRecord) for record in records):
                raise EvidenceProviderOutputInvalidError()
            await self._validate_upstream_records(
                scope,
                collection,
                records,
                expected_operation_id=expected_operation_id,
            )
        elif any(isinstance(record, UpstreamResultRecord) for record in records):
            raise EvidenceProviderOutputInvalidError()

    async def _validate_upstream_records(
        self,
        scope: EvidenceSessionScope,
        collection: EvidenceCollectionInput,
        records: tuple[UpstreamResultRecord, ...],
        *,
        expected_operation_id: str,
    ) -> None:
        allowed_comids = set(scope.direct_upstream_comids)
        allowed_parameters = set(collection.parameter_keys)
        windows = {item.window_id: item.time_range for item in scope.time_windows}
        allowed_windows = set(collection.affected_window_ids)
        session_ids: set[str] = set()
        reference_sessions: dict[str, str] = {}

        for record in records:
            if (
                record.provider != collection.provider
                or record.channel != collection.channel
                or record.provenance.operation_id != expected_operation_id
                or record.location.comid not in allowed_comids
                or record.parameter not in allowed_parameters
                or record.window_id not in allowed_windows
                or record.window_id not in windows
                or record.valid_time_range != windows[record.window_id]
            ):
                raise EvidenceProviderOutputInvalidError()
            session_ids.add(record.upstream_session_id)
            item_ids = (
                record.result_ref.item_id,
                *(reference.item_id for reference in record.evidence_closure_refs),
            )
            if len(item_ids) != len(set(item_ids)):
                raise EvidenceProviderOutputInvalidError()
            for item_id in item_ids:
                previous = reference_sessions.setdefault(
                    item_id,
                    record.upstream_session_id,
                )
                if previous != record.upstream_session_id:
                    raise EvidenceProviderOutputInvalidError()

        sessions = {}
        for session_id in sorted(session_ids):
            try:
                sessions[session_id] = await self._reader.read_agent_session(
                    scope,
                    session_id,
                )
            except (EvidenceScopeViolationError, ValueError) as error:
                raise EvidenceProviderOutputInvalidError() from error

        for record in records:
            session = sessions[record.upstream_session_id]
            if (
                str(session.session_id) != record.upstream_session_id
                or session.agent_role is not AgentRole.WATERBODY
                or session.target_comid != record.location.comid
                or session.status is not AgentSessionStatus.COMPLETED
            ):
                raise EvidenceProviderOutputInvalidError()

        item_ids = tuple(sorted(reference_sessions))
        try:
            resolved = await self._reader.resolve_reference_scopes(scope, item_ids)
        except EvidenceScopeViolationError as error:
            raise EvidenceProviderOutputInvalidError() from error
        if (
            {item.item_id for item in resolved} != set(item_ids)
            or any(
                item.run_id != scope.run_id
                or item.session_id != reference_sessions[item.item_id]
                for item in resolved
            )
        ):
            raise EvidenceProviderOutputInvalidError()
