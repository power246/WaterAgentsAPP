from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from math import isfinite
from typing import NoReturn, Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError

from app.application.analysis_runs.models import AgentRole, PublicError
from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    CancellationToken,
    WorkflowContext,
    WorkflowFailed,
    WorkflowStopped,
)
from app.application.query_understanding.context_models import (
    AnalysisContextResolution,
    AnalysisContextResolutionStatus,
    ParsedIntentV1,
)
from app.application.query_understanding.parser_models import (
    ParserFinishReason,
    QueryParserInputV1,
    QueryParserMessageV1,
    QueryParserResponse,
)
from app.application.query_understanding.parser_port import QueryParserPort
from app.application.query_understanding.parser_prompt import QueryParserPromptBuilder
from app.application.transcripts.models import (
    AnalysisRequestV1,
    GenerationStatus,
    MessageEndReason,
)
from app.application.transcripts.redaction import redact, redact_text
from app.ports import PortTimeoutError


class QueryContextResolver(Protocol):
    async def resolve(
        self,
        request: AnalysisRequestV1,
        parsed_intent: ParsedIntentV1,
        *,
        analysis_timezone: str,
        decision_instant: datetime,
    ) -> AnalysisContextResolution: ...


class _ParserModelUnavailable(Exception):
    pass


class _ParserOutputRejected(Exception):
    pass


ResultT = TypeVar("ResultT")


class QueryParserWorkflow(AnalysisWorkflow):
    def __init__(
        self,
        parser: QueryParserPort,
        context_resolver: QueryContextResolver,
        prompt_builder: QueryParserPromptBuilder,
        *,
        error_id_factory: Callable[[], UUID],
        timeout_seconds: float = 45,
        private_values: tuple[str, ...] = (),
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("query parser timeout must be finite and positive")
        self._parser = parser
        self._context_resolver = context_resolver
        self._prompt_builder = prompt_builder
        self._error_id_factory = error_id_factory
        self._timeout_seconds = timeout_seconds
        self._private_values = private_values

    async def run(self, context: WorkflowContext) -> None:
        context.cancellation.checkpoint()
        run_id = context.run.run_id
        session = await context.operations.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        context.cancellation.checkpoint()
        await context.operations.mark_agent_session_running(
            run_id,
            session.session_id,
        )

        parser_request = self._prompt_builder.build(
            context.request,
            decision_instant=context.run.created_at,
            analysis_timezone=context.run.configuration.analysis_timezone,
            private_values=self._private_values,
        )
        operation = await context.operations.create_operation(
            run_id,
            session.session_id,
        )
        input_item = await context.operations.reserve_transcript_item(
            run_id,
            session_id=session.session_id,
            operation_id=operation.operation_id,
        )
        context.cancellation.checkpoint()
        await context.operations.finalize_transcript_item(
            run_id,
            input_item.item_id,
            "query_parser_input",
            1,
            parser_request,
            private_values=self._private_values,
        )

        parsed_intent: ParsedIntentV1 | None = None
        for attempt in range(2):
            context.cancellation.checkpoint()
            if attempt:
                operation = await context.operations.create_operation(
                    run_id,
                    session.session_id,
                )
            try:
                response = await self._generate_message(
                    context,
                    session.session_id,
                    operation.operation_id,
                    parser_request,
                )
            except _ParserModelUnavailable:
                await self._fail(
                    context,
                    session.session_id,
                    code="parser_model_unavailable",
                    message="The selected query parser model is unavailable.",
                    failed_stage="query_parsing",
                    retryable=True,
                )
            except _ParserOutputRejected:
                await self._fail_parse(context, session.session_id)

            try:
                parsed_output = ParsedIntentV1.model_validate_json(response.text)
                parsed_intent = ParsedIntentV1.model_validate(
                    redact(
                        parsed_output.model_dump(mode="json"),
                        self._private_values,
                    )
                )
            except ValidationError as error:
                if attempt:
                    await self._fail_parse(context, session.session_id)
                safe_output = redact_text(response.text, self._private_values)
                parser_request = self._prompt_builder.build_repair(
                    parser_request,
                    safe_output,
                    error,
                    private_values=self._private_values,
                )
                continue
            break

        assert parsed_intent is not None
        context.cancellation.checkpoint()
        resolution = await self._await_with_cancellation(
            self._context_resolver.resolve(
                context.request,
                parsed_intent,
                analysis_timezone=context.run.configuration.analysis_timezone,
                decision_instant=context.run.created_at,
            ),
            context.cancellation,
        )
        if resolution.context is None:
            status = resolution.status
            await self._fail(
                context,
                session.session_id,
                code=status.value,
                message=self._resolution_message(status),
                failed_stage=self._resolution_stage(status),
                retryable=False,
            )

        context.cancellation.checkpoint()
        understood_item = await context.operations.reserve_transcript_item(
            run_id,
            session_id=session.session_id,
            operation_id=operation.operation_id,
        )
        context.cancellation.checkpoint()
        await context.operations.finalize_transcript_item(
            run_id,
            understood_item.item_id,
            "query_understood",
            1,
            resolution.context,
            private_values=self._private_values,
        )
        context.cancellation.checkpoint()
        await context.operations.complete_agent_session(
            run_id,
            session.session_id,
        )

    async def _generate_message(
        self,
        context: WorkflowContext,
        session_id: UUID,
        operation_id: UUID,
        parser_request: QueryParserInputV1,
    ) -> QueryParserResponse:
        message = await context.operations.start_transcript_message(
            context.run.run_id,
            session_id,
            "query_parser_message",
            1,
            operation_id=operation_id,
            private_values=self._private_values,
        )
        raw_chunks: list[str] = []

        async def on_delta(text: str) -> None:
            context.cancellation.checkpoint()
            raw_chunks.append(text)
            await context.operations.append_transcript_message_delta(
                context.run.run_id,
                message.item_id,
                text,
            )
            context.cancellation.checkpoint()

        try:
            response = await self._await_with_cancellation(
                self._parser.generate(parser_request, on_delta),
                context.cancellation,
                timeout=self._timeout_seconds,
            )
        except (WorkflowStopped, asyncio.CancelledError):
            raise
        except Exception:
            await self._finalize_incomplete(
                context,
                message.item_id,
                "".join(raw_chunks),
                MessageEndReason.ERROR,
            )
            raise _ParserModelUnavailable() from None

        streamed = "".join(raw_chunks)
        if response.finish_reason is not ParserFinishReason.STOP:
            end_reason = (
                MessageEndReason.TRUNCATED
                if response.finish_reason is ParserFinishReason.LENGTH
                else MessageEndReason.ERROR
            )
            text = response.text if response.text.startswith(streamed) else streamed
            await self._finalize_incomplete(
                context,
                message.item_id,
                text,
                end_reason,
            )
            raise _ParserOutputRejected()

        await context.operations.finalize_transcript_message(
            context.run.run_id,
            message.item_id,
            QueryParserMessageV1(
                text=response.text,
                generation_status=GenerationStatus.COMPLETED,
                end_reason=MessageEndReason.COMPLETED,
            ),
        )
        if not response.text.startswith(streamed):
            raise _ParserOutputRejected()
        return response

    async def _finalize_incomplete(
        self,
        context: WorkflowContext,
        item_id: UUID,
        text: str,
        end_reason: MessageEndReason,
    ) -> None:
        await context.operations.finalize_transcript_message(
            context.run.run_id,
            item_id,
            QueryParserMessageV1(
                text=text,
                generation_status=GenerationStatus.INCOMPLETE,
                end_reason=end_reason,
            ),
        )

    async def _fail_parse(
        self,
        context: WorkflowContext,
        session_id: UUID,
    ) -> NoReturn:
        await self._fail(
            context,
            session_id,
            code="query_parse_failed",
            message="The query parser did not return a valid structured intent.",
            failed_stage="query_parsing",
            retryable=False,
        )

    async def _fail(
        self,
        context: WorkflowContext,
        session_id: UUID,
        *,
        code: str,
        message: str,
        failed_stage: str,
        retryable: bool,
    ) -> NoReturn:
        context.cancellation.checkpoint()
        await context.operations.fail_agent_session(
            context.run.run_id,
            session_id,
            PublicError(
                error_id=self._error_id_factory(),
                code=code,
                message=message,
                failed_stage=failed_stage,
                retryable=retryable,
            ),
        )
        context.cancellation.checkpoint()
        raise WorkflowFailed(
            "The query understanding workflow failed.",
            code=code,
            public_message=message,
            failed_stage=failed_stage,
            retryable=retryable,
        )

    @staticmethod
    async def _await_with_cancellation(
        awaitable: Awaitable[ResultT],
        cancellation: CancellationToken,
        *,
        timeout: float | None = None,
    ) -> ResultT:
        work = asyncio.ensure_future(awaitable)
        stopped = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (work, stopped),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation.is_requested() or stopped in done:
                raise WorkflowStopped()
            if work not in done:
                raise PortTimeoutError("query parser timed out")
            return work.result()
        finally:
            for task in (work, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)

    @staticmethod
    def _resolution_stage(status: AnalysisContextResolutionStatus) -> str:
        if status.value.startswith("location_") or status.value == "target_mismatch":
            return "location_resolution"
        if status.value.startswith("date_") or status.value == "resolution_conflict":
            return "date_resolution"
        if status.value.startswith("parameter_") or status.value == "too_many_parameters":
            return "parameter_resolution"
        return "query_resolution"

    @staticmethod
    def _resolution_message(status: AnalysisContextResolutionStatus) -> str:
        messages = {
            AnalysisContextResolutionStatus.QUERY_CLARIFICATION_REQUIRED: (
                "The query requires clarification before it can be analyzed."
            ),
            AnalysisContextResolutionStatus.LOCATION_REQUIRED: (
                "A location is required for this analysis."
            ),
            AnalysisContextResolutionStatus.LOCATION_AMBIGUOUS: (
                "The requested location is ambiguous."
            ),
            AnalysisContextResolutionStatus.LOCATION_CONFLICT: (
                "The structured and query locations conflict."
            ),
            AnalysisContextResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION: (
                "The requested location is outside the supported region."
            ),
            AnalysisContextResolutionStatus.LOCATION_NOT_SUPPORTED: (
                "The requested location does not map to a supported river segment."
            ),
            AnalysisContextResolutionStatus.TARGET_MISMATCH: (
                "The selected COMID and coordinates do not identify the same target."
            ),
            AnalysisContextResolutionStatus.DATE_RANGE_REQUIRED: (
                "A complete date range is required for this analysis."
            ),
            AnalysisContextResolutionStatus.DATE_RANGE_CONFLICT: (
                "The structured and query date ranges conflict."
            ),
            AnalysisContextResolutionStatus.DATE_RANGE_OUT_OF_BOUNDS: (
                "The requested dates are outside the supported range."
            ),
            AnalysisContextResolutionStatus.RESOLUTION_CONFLICT: (
                "The requested time resolution is not compatible with the date range."
            ),
            AnalysisContextResolutionStatus.PARAMETER_REQUIRED: (
                "At least one water parameter is required."
            ),
            AnalysisContextResolutionStatus.PARAMETER_AMBIGUOUS: (
                "A requested water parameter is ambiguous."
            ),
            AnalysisContextResolutionStatus.PARAMETER_UNSUPPORTED: (
                "A requested water parameter is not supported."
            ),
            AnalysisContextResolutionStatus.TOO_MANY_PARAMETERS: (
                "The query requests more than six target parameters."
            ),
        }
        return messages.get(status, "The query could not be resolved.")
