import json
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
from app.domain import GeoPoint, InferenceMode, ParameterKey, TimeRange, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.model_catalog import ModelCapability, ModelRole, ModelSelection
from app.infrastructure.config import ModelCatalog

PARSER_MODEL = ModelSelection(provider="test-provider", model="parser-v1")
WATERBODY_MODEL = ModelSelection(provider="test-provider", model="waterbody-v1")
MODEL_CAPABILITIES = (
    ModelCapability(
        **PARSER_MODEL.model_dump(),
        roles=(ModelRole.QUERY_PARSER,),
    ),
    ModelCapability(
        **WATERBODY_MODEL.model_dump(),
        roles=(ModelRole.WATERBODY,),
    ),
)
MODEL_CATALOG = ModelCatalog(MODEL_CAPABILITIES)

_OMITTED = object()


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
    return create_app(*args, **kwargs)
