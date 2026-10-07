import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from app.application.query_understanding import (
    AnalysisContext,
    AnalysisTimeSegment,
    ParameterIntentSource,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
)
from app.application.transcript_memory import (
    RAGSearchUnavailableError,
    TranscriptEvidenceProducer,
)
from app.domain import (
    EmbeddingProfile,
    GeoPoint,
    InferenceMode,
    ParameterKey,
    TimeRange,
    TimeResolution,
)
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.model_catalog import ModelCapability, ModelRole, ModelSelection
from app.infrastructure.config import ModelCatalog

PARSER_MODEL = ModelSelection(provider="test-provider", model="parser-v1")
WATERBODY_MODEL = ModelSelection(provider="test-provider", model="waterbody-v1")
EMBEDDING_PROFILE = EmbeddingProfile(
    provider="test-provider",
    model="embedding-v1",
    output_dimensions=8,
    max_input_tokens=512,
    semantic_min_similarity=0.78,
)
EMBEDDING_CAPABILITY = ModelCapability(
    provider=EMBEDDING_PROFILE.provider,
    model=EMBEDDING_PROFILE.model,
    roles=(ModelRole.EMBEDDING,),
    embedding_profile=EMBEDDING_PROFILE,
)
MODEL_CAPABILITIES = (
    ModelCapability(
        **PARSER_MODEL.model_dump(),
        roles=(ModelRole.QUERY_PARSER,),
    ),
    ModelCapability(
        **WATERBODY_MODEL.model_dump(),
        roles=(ModelRole.WATERBODY,),
        context_window_tokens=10_000_000,
        max_output_tokens=1_000_000,
    ),
    EMBEDDING_CAPABILITY,
)
MODEL_CATALOG = ModelCatalog(MODEL_CAPABILITIES)

_OMITTED = object()


class _UnscriptedWaterbodyModelPort:
    async def stream(self, request):
        del request
        raise AssertionError("test app received an unscripted M6 model call")
        yield  # pragma: no cover


@asynccontextmanager
async def _build_test_m6_runtime(settings, **kwargs):
    from app.infrastructure.runtime import (
        OpenAIResponsesSizeEstimator,
        WaterbodyAdapterRegistration,
    )
    from app.infrastructure.runtime.m6_test import build_m6_test_runtime

    async with build_m6_test_runtime(
        settings,
        registrations=(
            WaterbodyAdapterRegistration(
                provider="test-provider",
                port=_UnscriptedWaterbodyModelPort(),
                size_estimator=OpenAIResponsesSizeEstimator(),
            ),
        ),
        **kwargs,
    ) as runtime:
        yield runtime


class NoOpWorkflow:
    async def run(self, context) -> None:
        context.cancellation.checkpoint()


class _BuildingSearchSession:
    async def search(self, intent, *, operation_id, deadline=None):
        del intent, operation_id, deadline
        raise RAGSearchUnavailableError("index_building")

    def clear(self) -> None:
        pass


class _BuildingSearchService:
    def bind(self, scope):
        del scope
        return _BuildingSearchSession()


class FakeM8Runtime:
    def __init__(self) -> None:
        self.producer = TranscriptEvidenceProducer(_BuildingSearchService())
        self.started = False
        self.stopped = False
        self.wakes = 0

    async def start(self) -> None:
        self.started = True

    def notify_run_terminal(self, run_id) -> None:
        del run_id
        if not self.stopped:
            self.wakes += 1

    async def shutdown(self) -> None:
        self.stopped = True


@asynccontextmanager
async def build_test_m8_runtime(*args, **kwargs):
    del args, kwargs
    runtime = FakeM8Runtime()
    try:
        yield runtime
    finally:
        await runtime.shutdown()


def create_request(
    query: str = "lake",
    *,
    time_resolution: object = _OMITTED,
    **overrides: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "query": query,
        "parser_model": PARSER_MODEL.model_dump(mode="json"),
        "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
        "memory_mode": "stateless",
    }
    if time_resolution is not _OMITTED:
        body["time_resolution"] = time_resolution
    body.update(overrides)
    return body


def create_request_bytes(
    query: str = "lake",
    *,
    time_resolution: object = _OMITTED,
    **overrides: object,
) -> bytes:
    return json.dumps(
        create_request(
            query,
            time_resolution=time_resolution,
            **overrides,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def analysis_context(
    query: str = "Will dissolved oxygen become unsafe?",
) -> AnalysisContext:
    requested_start = date(2026, 9, 26)
    requested_end = date(2026, 9, 28)
    time_range = TimeRange(
        start=datetime(2026, 9, 26, 4, tzinfo=UTC),
        end=datetime(2026, 9, 29, 4, tzinfo=UTC),
    )
    return AnalysisContext(
        query=query,
        decision_instant=datetime(2026, 9, 25, 8, tzinfo=UTC),
        analysis_timezone="America/New_York",
        target=ResolvedTarget(
            comid="12345678",
            point=GeoPoint(latitude=38.8719, longitude=-77.0479),
            display_name="Selected river reach",
            country_code="US",
            subdivision_code="US-DC",
            source=TargetSource.FRONTEND,
        ),
        requested_date_range={
            "start_date": requested_start,
            "end_date": requested_end,
        },
        time_range=time_range,
        time_segments=(AnalysisTimeSegment(
            segment_index=0,
            mode=InferenceMode.PREDICTION,
            time_range=time_range,
        ),),
        mode=InferenceMode.PREDICTION,
        time_resolution=TimeResolution.SIX_HOURS,
        time_resolution_source=TimeResolutionSource.AUTO,
        params_used=(ResolvedParameterIntent(
            key=ParameterKey(code_system="usgs", code="00300"),
            canonical_name="dissolved oxygen",
            canonical_unit="mg/L",
            source=ParameterIntentSource.EXPLICIT,
            rationale="Dissolved oxygen is explicitly requested.",
        ),),
        supporting_params=(SupportingParameterIntent(
            key=ParameterKey(code_system="usgs", code="00060"),
            canonical_name="discharge",
            canonical_unit="ft3/s",
            reason=SupportingIntentReason.SYSTEM_REQUIRED,
        ),),
        parser_model=PARSER_MODEL,
        waterbody_model=WATERBODY_MODEL,
        memory_mode="stateless",
    )


def create_test_app(*args: object, **kwargs: object):
    from app.main import create_app

    kwargs.setdefault("model_catalog", MODEL_CATALOG)
    kwargs.setdefault("m6_runtime_builder", _build_test_m6_runtime)
    kwargs.setdefault("m8_runtime_builder", build_test_m8_runtime)
    if kwargs.get("workflow") is None:
        kwargs["workflow"] = NoOpWorkflow()
    return create_app(*args, **kwargs)
