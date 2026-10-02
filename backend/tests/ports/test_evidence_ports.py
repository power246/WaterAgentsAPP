import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.workflow import WorkflowStopped
from app.application.evidence_tools import (
    EvidenceAggregationNotAllowedError,
    EvidenceCollectionIncompleteError,
    EvidenceProducerUnavailableError,
    EvidenceScopeViolationError,
    EvidenceStationNotAuthorizedError,
    EvidenceTimeOutOfRangeError,
    EvidenceToolBudgetExceededError,
    EvidenceUnitUnresolvedError,
    WaterbodyGeometryUnavailableError,
    map_evidence_error,
)
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    ParameterKey,
    ParameterProvider,
    ProviderCollectionBatch,
    TimeRange,
    WaterObservationRequest,
)
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    WaterObservationPort,
)


START = datetime(2026, 9, 29, tzinfo=timezone.utc)
REQUEST_RANGE = TimeRange(start=START, end=START + timedelta(days=1))


def empty_batch() -> ProviderCollectionBatch:
    return ProviderCollectionBatch(
        operation_id="operation-1",
        records=(),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity="page-1",
                record_count=0,
                response_bytes=2,
                page_complete=True,
            ),
        ),
        completed_ranges=(REQUEST_RANGE,),
        counts=CollectionCounts(requests=1, pages=1, records=0, response_bytes=2),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=True,
            terminal_identity="page-1",
            digest="digest-1",
        ),
    )


def water_request() -> WaterObservationRequest:
    return WaterObservationRequest(
        operation_id="operation-1",
        provider=ParameterProvider.WDFN_TIME_SERIES,
        channel="continuous",
        station_id="USGS-123",
        series_refs=("series-123",),
        parameter_keys=(ParameterKey(code_system="usgs", code="00300"),),
        request_range=REQUEST_RANGE,
        affected_window_ids=("window-000",),
    )


def test_completed_empty_batch_and_unavailable_error_are_distinct() -> None:
    class EmptyProvider:
        async def collect(
            self,
            request: WaterObservationRequest,
            *,
            deadline: float | None = None,
        ) -> ProviderCollectionBatch:
            return empty_batch()

    class UnavailableProvider:
        async def collect(
            self,
            request: WaterObservationRequest,
            *,
            deadline: float | None = None,
        ) -> ProviderCollectionBatch:
            raise PortUnavailableError("raw provider detail must not escape")

    async def exercise() -> None:
        empty: WaterObservationPort = EmptyProvider()
        unavailable: WaterObservationPort = UnavailableProvider()
        result = await empty.collect(water_request())
        assert result.completion_proof.complete
        assert result.records == ()
        with pytest.raises(PortUnavailableError):
            await unavailable.collect(water_request())

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            PortUnavailableError("private URL https://secret.invalid?q=token"),
            "evidence_provider_unavailable",
            True,
        ),
        (
            InvalidPortResponseError("raw response"),
            "evidence_provider_response_invalid",
            False,
        ),
        (
            PortAuthenticationError("Authorization: secret"),
            "evidence_provider_authentication_failed",
            False,
        ),
        (
            PortRateLimitError("provider said too many requests"),
            "evidence_provider_rate_limited",
            True,
        ),
        (
            PortTimeoutError("socket timeout at private host"),
            "evidence_provider_timeout",
            True,
        ),
        (
            PortResponseLimitError("response had private bytes"),
            "evidence_provider_response_too_large",
            False,
        ),
        (
            PortConfigurationError("secret config"),
            "evidence_provider_unavailable",
            True,
        ),
        (
            PortHttpError("private response body", status_code=503),
            "evidence_provider_unavailable",
            True,
        ),
        (
            EvidenceCollectionIncompleteError("page failed"),
            "evidence_collection_incomplete",
            True,
        ),
        (
            EvidenceScopeViolationError(),
            "evidence_scope_violation",
            False,
        ),
        (
            EvidenceTimeOutOfRangeError(),
            "evidence_time_out_of_range",
            False,
        ),
        (
            EvidenceStationNotAuthorizedError(),
            "evidence_station_not_authorized",
            False,
        ),
        (
            EvidenceToolBudgetExceededError(),
            "evidence_tool_budget_exceeded",
            False,
        ),
        (
            EvidenceUnitUnresolvedError(),
            "evidence_unit_unresolved",
            False,
        ),
        (
            EvidenceAggregationNotAllowedError(),
            "evidence_aggregation_not_allowed",
            False,
        ),
        (
            WaterbodyGeometryUnavailableError(),
            "waterbody_geometry_unavailable",
            False,
        ),
        (
            EvidenceProducerUnavailableError(),
            "evidence_producer_unavailable",
            False,
        ),
    ],
)
def test_port_failures_map_to_distinct_fixed_safe_errors(
    error,
    code,
    retryable,
) -> None:
    safe = map_evidence_error(error)
    assert safe.code == code
    assert safe.retryable is retryable
    assert "secret" not in safe.message.lower()
    assert "http" not in safe.message.lower()
    assert "raw" not in safe.message.lower()


def test_cancel_is_not_rewritten_as_provider_failure() -> None:
    with pytest.raises(asyncio.CancelledError):
        map_evidence_error(asyncio.CancelledError())
    with pytest.raises(WorkflowStopped):
        map_evidence_error(WorkflowStopped())


def test_only_registered_completion_contracts_can_claim_completeness() -> None:
    payload = empty_batch().model_dump(mode="python")
    payload["completion_proof"]["contract"] = "guessed_pagination"
    with pytest.raises(ValidationError):
        ProviderCollectionBatch.model_validate(payload)

    payload = empty_batch().model_dump(mode="python")
    payload["completion_proof"]["complete"] = False
    payload["completion_proof"]["terminal_identity"] = None
    with pytest.raises(ValidationError, match="incomplete collection requires"):
        ProviderCollectionBatch.model_validate(payload)

    payload = empty_batch().model_dump(mode="python")
    payload["page_or_chunk_manifests"][0]["next_page_identity"] = "page-2"
    with pytest.raises(ValidationError, match="terminal page"):
        ProviderCollectionBatch.model_validate(payload)
