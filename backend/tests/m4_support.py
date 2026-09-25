import json

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


def create_test_app(*args: object, **kwargs: object):
    from app.main import create_app

    kwargs.setdefault("model_catalog", MODEL_CATALOG)
    return create_app(*args, **kwargs)
