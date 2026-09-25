import json

from app.domain.hydrology import EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256
from app.infrastructure.config import load_parameter_capability_registry


def audit_parameter_capabilities() -> dict[str, object]:
    registry = load_parameter_capability_registry()
    keys = tuple(
        f"{capability.key.code_system}:{capability.key.code}"
        for capability in registry.capabilities
    )
    required_support_keys = tuple(
        f"{capability.key.code_system}:{capability.key.code}"
        for capability in registry.required_support
    )
    return {
        "capability_count": len(registry.capabilities),
        "catalog_key_set_sha256": EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256,
        "group_counts": dict(registry.group_counts),
        "required_support_count": len(required_support_keys),
        "required_support_keys": required_support_keys,
        "resolution_enabled_count": sum(
            capability.resolution_enabled
            for capability in registry.capabilities
        ),
        "unique_key_count": len(set(keys)),
    }


if __name__ == "__main__":
    print(
        json.dumps(
            audit_parameter_capabilities(),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
