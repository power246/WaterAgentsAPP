import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.application.analysis_runs.models import AgentSessionStatus, RunStatus
from app.application.query_understanding import (
    AnalysisContext,
    AnalysisContextResolution,
    AnalysisContextResolutionStatus,
    ContextDependencyError,
    DateResolver,
    ParameterIntentSource,
    ParserFinishReason,
    ParsedIntentV1,
    QueryParserInputV1,
    QueryParserPromptBuilder,
    QueryParserResponse,
    QueryParserWorkflow,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
)
from app.application.transcripts.models import GenerationStatus, MessageEndReason
from app.domain import GeoPoint, ParameterKey
from app.infrastructure.config import (
    AppSettings,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.persistence.memory_settings import InMemorySettingsRepository
from app.ports import InvalidPortResponseError, PortUnavailableError
from tests.m4_support import create_request_bytes, create_test_app


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


BLOCK = object()


def parser_output(
    *,
    ambiguities: list[dict[str, str]] | None = None,
    rationale: str = "Dissolved oxygen is explicitly requested.",
) -> str:
    return json.dumps(
        {
            "location_mention": None,
            "coordinate_mention": {"latitude": 38.9, "longitude": -77.0},
            "start_date": "2026-09-25",
            "end_date": "2026-09-28",
            "parameters": [
                {
                    "concept": "dissolved oxygen",
                    "rationale": rationale,
                    "explicitly_named": True,
                }
            ],
            "ambiguities": ambiguities or [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class FakeParser:
    def __init__(self, *results: object) -> None:
        self.results = deque(results)
        self.calls: list[QueryParserInputV1] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate(self, request, on_delta):
        self.calls.append(request)
        self.started.set()
        result = self.results.popleft()
        try:
            if result is BLOCK:
                await asyncio.Event().wait()
                raise AssertionError("blocked parser unexpectedly resumed")
            if isinstance(result, BaseException):
                await on_delta("partial provider output")
                raise result
            assert isinstance(result, QueryParserResponse)
            midpoint = len(result.text) // 2
            await on_delta(result.text[:midpoint])
            await on_delta(result.text[midpoint:])
            return result
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class FakeContextResolver:
    def __init__(
        self,
        status: AnalysisContextResolutionStatus = AnalysisContextResolutionStatus.SUCCESS,
    ) -> None:
        self.status = status
        self.calls: list[ParsedIntentV1] = []

    async def resolve(
        self,
        request,
        parsed_intent,
        *,
        analysis_timezone,
        decision_instant,
    ) -> AnalysisContextResolution:
        self.calls.append(parsed_intent)
        if self.status is not AnalysisContextResolutionStatus.SUCCESS:
            return AnalysisContextResolution(status=self.status)
        configuration = request.to_configuration_snapshot(analysis_timezone)
        temporal = DateResolver().resolve(
            request.date_range,
            parsed_intent,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            resolution_preference=configuration.time_resolution_preference,
            resolution_preference_source=(
                configuration.time_resolution_preference_source
            ),
        ).temporal
        assert temporal is not None
        concept = parsed_intent.parameters[0]
        context = AnalysisContext(
            query=request.query,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            target=ResolvedTarget(
                comid="12345678",
                point=GeoPoint(latitude=38.9, longitude=-77.0),
                display_name="Potomac reach",
                country_code="US",
                subdivision_code="US-DC",
                source=TargetSource.QUERY,
            ),
            requested_date_range=temporal.requested_date_range,
            time_range=temporal.time_range,
            time_segments=temporal.time_segments,
            mode=temporal.mode,
            time_resolution=temporal.time_resolution,
            time_resolution_source=temporal.time_resolution_source,
            params_used=(
                ResolvedParameterIntent(
                    key=ParameterKey(code_system="usgs", code="00300"),
                    canonical_name="Dissolved oxygen",
                    canonical_unit="mg/l",
                    source=ParameterIntentSource.EXPLICIT,
                    rationale=concept.rationale,
                ),
            ),
            supporting_params=(
                SupportingParameterIntent(
                    key=ParameterKey(code_system="usgs", code="00060"),
                    canonical_name="Discharge",
                    canonical_unit="ft3/s",
                    reason=SupportingIntentReason.CAPABILITY_REQUIRED,
                ),
            ),
            parser_model=request.parser_model,
            waterbody_model=request.waterbody_model,
            memory_mode=request.memory_mode,
        )
        context.validate_configuration(configuration)
        return AnalysisContextResolution(
            status=AnalysisContextResolutionStatus.SUCCESS,
            context=context,
        )


class FailingContextResolver:
    async def resolve(self, *args, **kwargs):
        del args, kwargs
        raise ContextDependencyError(
            code="location_resolution_unavailable",
            message="Location resolution services are temporarily unavailable.",
            failed_stage="location_resolution",
            retryable=True,
        )


class FailingParameterContextResolver:
    async def resolve(self, *args, **kwargs):
        del args, kwargs
        raise ContextDependencyError(
            code="parameter_catalog_unavailable",
            message="The parameter catalog is temporarily unavailable.",
            failed_stage="parameter_resolution",
            retryable=True,
        )


@asynccontextmanager
async def parser_runtime(
    parser: FakeParser,
    resolver: FakeContextResolver,
    *,
    timeout: float = 0.2,
    private_values: tuple[str, ...] = (),
    unit_policy_store=None,
    yield_app: bool = False,
):
    registry = load_parameter_capability_registry()
    workflow = QueryParserWorkflow(
        parser,
        resolver,
        QueryParserPromptBuilder(registry),
        error_id_factory=uuid4,
        timeout_seconds=timeout,
        private_values=private_values,
        capability_registry=registry if unit_policy_store is not None else None,
        unit_policy_store=unit_policy_store,
    )
    application = create_test_app(
        settings=AppSettings(
            analysis_timezone="America/New_York",
            query_parser_timeout_seconds=timeout,
        ),
        clock=FixedClock(),
        workflow=workflow,
        transcript_store=InMemoryTranscriptStore(),
        settings_store=unit_policy_store,
    )
    async with application.router.lifespan_context(application):
        yield application if yield_app else application.state.run_coordinator


async def wait_quiesced(coordinator, run_id) -> None:
    await asyncio.wait_for(
        coordinator._runs[run_id].workflow_quiesced.wait(),
        timeout=2,
    )


async def create_and_wait(coordinator, query: str = "Check dissolved oxygen"):
    created = await coordinator.create_run(create_request_bytes(query))
    await wait_quiesced(coordinator, created.run_id)
    return created, await coordinator.get_snapshot(created.run_id)


async def session_content(coordinator, run_id):
    sessions = await coordinator.list_sessions(run_id)
    assert len(sessions) == 1
    session = sessions[0]
    live = await coordinator.get_live_read(run_id)
    assert live is not None
    items = tuple(item for item in live.items if item.session_id == session.session_id)
    return session, items


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise, timeout=5))


def successful_response(text: str | None = None) -> QueryParserResponse:
    return QueryParserResponse(
        text=text or parser_output(),
        finish_reason=ParserFinishReason.STOP,
    )


def test_one_successful_call_persists_real_parser_session_and_unique_context() -> None:
    async def exercise() -> None:
        parser = FakeParser(successful_response())
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            session, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.COMPLETED
            assert session.status is AgentSessionStatus.COMPLETED
            assert session.target_comid is None
            assert len(parser.calls) == 1
            assert len(resolver.calls) == 1
            assert parser.calls[0].tools == ()
            assert parser.calls[0].model == created.configuration.parser_model
            assert parser.calls[0].model != created.configuration.waterbody_model
            assert tuple(item.content.content_type for item in items) == (
                "lifecycle_event",
                "query_parser_input",
                "query_parser_message",
                "query_understood",
                "lifecycle_event",
            )
            assert [item.transcript_sequence for item in items] == sorted(
                item.transcript_sequence for item in items
            )
            assert items[1].operation_id == items[2].operation_id == items[3].operation_id
            assert items[1].operation_id is not None
            message = json.loads(items[2].content.safe_body)
            assert message["generation_status"] == "completed"
            assert message["end_reason"] == "completed"
            understood = json.loads(items[3].content.safe_body)
            assert understood["schema_version"] == 1
            assert understood["target"]["comid"] == "12345678"
            process_names = tuple(
                type(record).__name__
                for record in coordinator._runs[created.run_id]
                .agent_sessions[session.session_id]
                .process_events
            )
            assert process_names[:3] == (
                "TranscriptItemProcessRecord",
                "TranscriptItemProcessRecord",
                "MessageStartedProcessRecord",
            )
            assert "MessageDeltaProcessRecord" in process_names
            assert "MessageFinalizedProcessRecord" in process_names
            assert process_names[-2:] == (
                "TranscriptItemProcessRecord",
                "TranscriptItemProcessRecord",
            )

    run(exercise())


def test_policy_and_query_unit_override_flow_into_saved_context() -> None:
    async def exercise() -> None:
        registry = load_parameter_capability_registry()
        store = InMemorySettingsRepository(registry)
        overridden = json.loads(parser_output())
        overridden["unit_intents"] = [{
            "raw_unit_alias": "mg/l",
            "parameter_reference": "dissolved oxygen",
            "rationale": "User requested mg/l",
        }]
        parser = FakeParser(
            successful_response(),
            successful_response(json.dumps(overridden)),
        )
        async with parser_runtime(
            parser, FakeContextResolver(), unit_policy_store=store,
            yield_app=True,
        ) as application:
            service = application.state.unit_policy_service
            change = next(
                item for item in (await store.read_unit_policy(1)).entries
                if item.parameter.code == "00300"
            ).model_copy(update={"target_unit_id": "micrograms_per_liter"})
            preview = await service.preview(base_version=1, entries=(change,))
            assert preview.confirmation is not None
            await service.confirm(
                token=preview.confirmation.token,
                phrase=preview.confirmation.required_phrase,
                idempotency_key=uuid4(), raw_body=b"confirmed policy",
            )
            coordinator = application.state.run_coordinator
            first, _ = await create_and_wait(coordinator, "global preference")
            second, _ = await create_and_wait(coordinator, "query override")
            assert first.configuration.unit_policy_version == 2
            for created, target, source in (
                (first, "micrograms_per_liter", "global_policy"),
                (second, "milligram_per_liter", "session_override"),
            ):
                _, items = await session_content(coordinator, created.run_id)
                understood = next(
                    json.loads(item.content.safe_body)
                    for item in items
                    if item.content.content_type == "query_understood"
                )
                assert understood["unit_preferences"] == [{
                    "parameter": {"code_system": "usgs", "code": "00300"},
                    "target_unit_id": target,
                    "registry_digest": registry.manifest_digest,
                    "source": source,
                }]

    run(exercise())


def test_schema_invalid_first_call_repairs_once_without_overwriting_message() -> None:
    async def exercise() -> None:
        invalid = '{"parameters":[]}'
        parser = FakeParser(
            successful_response(invalid),
            successful_response(),
        )
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            session, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.COMPLETED
            assert session.status is AgentSessionStatus.COMPLETED
            assert len(parser.calls) == 2
            assert len(resolver.calls) == 1
            inputs = tuple(
                item for item in items
                if item.content.content_type == "query_parser_input"
            )
            assert len(inputs) == 2
            messages = tuple(
                item for item in items
                if item.content.content_type == "query_parser_message"
            )
            assert len(messages) == 2
            assert json.loads(messages[0].content.safe_body)["text"] == invalid
            repair_payload = json.loads(parser.calls[1].messages[1].text)
            assert set(repair_payload) == {"invalid_output", "validation_errors"}
            assert repair_payload["invalid_output"] == invalid
            assert messages[0].operation_id != messages[1].operation_id
            assert inputs[0].operation_id == messages[0].operation_id
            assert inputs[1].operation_id == messages[1].operation_id
            assert inputs[1].content.safe_body == parser.calls[1].model_dump_json()
            context_item = next(
                item for item in items
                if item.content.content_type == "query_understood"
            )
            assert context_item.operation_id == messages[1].operation_id

    run(exercise())


def test_two_schema_invalid_outputs_fail_without_third_call_or_context() -> None:
    async def exercise() -> None:
        parser = FakeParser(
            successful_response('{"parameters":[]}'),
            successful_response("not-json"),
        )
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            session, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.FAILED
            assert session.status is AgentSessionStatus.FAILED
            assert len(parser.calls) == 2
            assert resolver.calls == []
            assert all(
                item.content.content_type != "query_understood" for item in items
            )
            assert coordinator._runs[created.run_id].last_error.code == (
                "query_parse_failed"
            )
            assert coordinator._runs[created.run_id].agent_sessions[
                session.session_id
            ].last_error.code == "query_parse_failed"

    run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            PortUnavailableError("provider unavailable"),
            id="provider-unavailable",
        ),
        pytest.param(
            InvalidPortResponseError("invalid streamed output item"),
            id="invalid-output-item",
        ),
        pytest.param(
            InvalidPortResponseError("invalid incomplete reason"),
            id="invalid-incomplete-reason",
        ),
        pytest.param(BLOCK, id="timeout"),
    ],
)
def test_technical_parser_failure_never_repairs_resolves_or_publishes_context(
    failure,
) -> None:
    async def exercise() -> None:
        parser = FakeParser(failure)
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver, timeout=0.01) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            session, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.FAILED
            assert session.status is AgentSessionStatus.FAILED
            assert len(parser.calls) == 1
            assert resolver.calls == []
            assert all(
                item.content.content_type != "query_understood" for item in items
            )
            assert coordinator._runs[created.run_id].last_error.code == (
                "parser_model_unavailable"
            )
            assert coordinator._runs[created.run_id].agent_sessions[
                session.session_id
            ].last_error.code == "parser_model_unavailable"
            message = next(
                item for item in items
                if item.content.content_type == "query_parser_message"
            )
            body = json.loads(message.content.safe_body)
            assert body["generation_status"] == "incomplete"
            assert body["end_reason"] == "error"
            if failure is BLOCK:
                assert parser.cancelled.is_set()

    run(exercise())


def test_cancel_stops_provider_and_never_starts_repair_or_resolver() -> None:
    async def exercise() -> None:
        parser = FakeParser(BLOCK, successful_response())
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver, timeout=1) as coordinator:
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(parser.started.wait(), timeout=1)
            cancelled = await coordinator.cancel_run(created.run_id)
            await wait_quiesced(coordinator, created.run_id)
            session, items = await session_content(coordinator, created.run_id)

            assert cancelled.status is RunStatus.CANCELLED
            assert session.status is AgentSessionStatus.CANCELLED
            assert len(parser.calls) == 1
            assert parser.cancelled.is_set()
            assert resolver.calls == []
            message = next(
                item for item in items
                if item.content.content_type == "query_parser_message"
            )
            body = json.loads(message.content.safe_body)
            assert body["generation_status"] == "incomplete"
            assert body["end_reason"] == "cancelled"
            assert all(
                item.content.content_type != "query_understood" for item in items
            )

    run(exercise())


@pytest.mark.parametrize(
    "status",
    [
        AnalysisContextResolutionStatus.QUERY_CLARIFICATION_REQUIRED,
        AnalysisContextResolutionStatus.PARAMETER_UNSUPPORTED,
    ],
)
def test_semantic_failures_do_not_trigger_schema_repair(status) -> None:
    async def exercise() -> None:
        ambiguities = (
            [{"subject": "location", "message": "Location is ambiguous."}]
            if status
            is AnalysisContextResolutionStatus.QUERY_CLARIFICATION_REQUIRED
            else None
        )
        parser = FakeParser(successful_response(parser_output(ambiguities=ambiguities)))
        resolver = FakeContextResolver(status)
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)

            assert terminal.status is RunStatus.FAILED
            assert len(parser.calls) == 1
            assert len(resolver.calls) == 1
            assert coordinator._runs[created.run_id].last_error.code == status.value

    run(exercise())


def test_tool_call_finish_is_rejected_before_resolver() -> None:
    async def exercise() -> None:
        parser = FakeParser(
            QueryParserResponse(
                text='{"tool":"geocode"}',
                finish_reason=ParserFinishReason.TOOL_CALL,
            )
        )
        resolver = FakeContextResolver()
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            _, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.FAILED
            assert len(parser.calls) == 1
            assert parser.calls[0].tools == ()
            assert resolver.calls == []
            assert coordinator._runs[created.run_id].last_error.code == (
                "query_parse_failed"
            )
            message = next(
                item for item in items
                if item.content.content_type == "query_parser_message"
            )
            assert json.loads(message.content.safe_body)["end_reason"] == "error"

    run(exercise())


def test_prompt_response_transcript_and_logs_share_credential_redaction(
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_runs")

    async def exercise() -> None:
        secret_response = parser_output(
            rationale="Leaked credential parser-secret"
        )
        parser = FakeParser(successful_response(secret_response))
        resolver = FakeContextResolver()
        async with parser_runtime(
            parser,
            resolver,
            private_values=("parser-secret",),
        ) as coordinator:
            created, terminal = await create_and_wait(
                coordinator,
                "api_key=parser-secret",
            )
            _, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.COMPLETED
            assert "parser-secret" not in parser.calls[0].model_dump_json()
            assert "parser-secret" not in "".join(
                item.content.safe_body for item in items
            )
            assert "parser-secret" not in "".join(
                record.getMessage() for record in caplog.records
            )
            process_text = "".join(
                getattr(record, "text", "")
                for session_runtime in coordinator._runs[
                    created.run_id
                ].agent_sessions.values()
                for record in session_runtime.process_events
            )
            assert "parser-secret" not in process_text
            assert "[REDACTED]" in process_text
            context_item = next(
                item for item in items
                if item.content.content_type == "query_understood"
            )
            assert "[REDACTED]" in context_item.content.safe_body

    run(exercise())


def test_provider_error_details_are_not_copied_to_transcript_or_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_runs")

    async def exercise() -> None:
        parser = FakeParser(
            PortUnavailableError("Authorization: Bearer parser-secret")
        )
        resolver = FakeContextResolver()
        async with parser_runtime(
            parser,
            resolver,
            private_values=("parser-secret",),
        ) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            _, items = await session_content(coordinator, created.run_id)

            assert terminal.status is RunStatus.FAILED
            assert coordinator._runs[created.run_id].last_error.code == (
                "parser_model_unavailable"
            )
            assert "parser-secret" not in "".join(
                item.content.safe_body for item in items
            )
            assert "parser-secret" not in "".join(
                record.getMessage() for record in caplog.records
            )

    run(exercise())


def test_location_dependency_failure_maps_to_safe_retryable_public_error() -> None:
    async def exercise() -> None:
        parser = FakeParser(successful_response())
        resolver = FailingContextResolver()
        async with parser_runtime(parser, resolver) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            session, items = await session_content(coordinator, created.run_id)
            error = coordinator._runs[created.run_id].last_error

            assert terminal.status is RunStatus.FAILED
            assert session.status is AgentSessionStatus.FAILED
            assert error.code == "location_resolution_unavailable"
            assert error.failed_stage == "location_resolution"
            assert error.retryable is True
            assert error.message == (
                "Location resolution services are temporarily unavailable."
            )
            assert "geocoder-secret" not in error.model_dump_json()
            assert "geocoder-secret" not in "".join(
                item.content.safe_body for item in items
            )

    run(exercise())


def test_parameter_catalog_failure_keeps_parameter_public_error_semantics() -> None:
    async def exercise() -> None:
        parser = FakeParser(successful_response())
        async with parser_runtime(
            parser,
            FailingParameterContextResolver(),
        ) as coordinator:
            created, terminal = await create_and_wait(coordinator)
            error = coordinator._runs[created.run_id].last_error

            assert terminal.status is RunStatus.FAILED
            assert error.code == "parameter_catalog_unavailable"
            assert error.failed_stage == "parameter_resolution"
            assert error.retryable is True
            assert error.message == (
                "The parameter catalog is temporarily unavailable."
            )

    run(exercise())
