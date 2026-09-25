from app.domain.model_catalog import ModelRole
from app.infrastructure.config import AppSettings


def smoke_request(
    query: str,
    *,
    time_resolution: str | None = None,
) -> dict[str, object]:
    settings = AppSettings()
    selections: dict[ModelRole, dict[str, str]] = {}
    for role in ModelRole:
        capability = next(
            (
                item
                for item in settings.model_catalog
                if role in item.roles
            ),
            None,
        )
        if capability is None:
            raise RuntimeError(
                f"WATERAGENTS_MODEL_CATALOG requires a selectable {role.value} model"
            )
        selections[role] = {
            "provider": capability.provider,
            "model": capability.model,
        }
    body: dict[str, object] = {
        "query": query,
        "parser_model": selections[ModelRole.QUERY_PARSER],
        "waterbody_model": selections[ModelRole.WATERBODY],
        "memory_mode": "stateless",
    }
    if time_resolution is not None:
        body["time_resolution"] = time_resolution
    return body
