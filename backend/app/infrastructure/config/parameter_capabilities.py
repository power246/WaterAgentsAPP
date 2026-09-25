from functools import lru_cache
from importlib.resources import files

from pydantic import ValidationError

from app.domain.hydrology import (
    ParameterCapabilityConfigurationError,
    ParameterCapabilityManifest,
    ParameterCapabilityRegistry,
)

PARAMETER_CAPABILITY_RESOURCE = "parameter_capabilities.v1.json"


def load_parameter_capability_registry_from_bytes(
    payload: bytes,
) -> ParameterCapabilityRegistry:
    try:
        manifest = ParameterCapabilityManifest.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ParameterCapabilityConfigurationError(
            "invalid parameter capability manifest"
        ) from error
    return ParameterCapabilityRegistry(manifest)


@lru_cache(maxsize=1)
def load_parameter_capability_registry() -> ParameterCapabilityRegistry:
    payload = files(__package__).joinpath(PARAMETER_CAPABILITY_RESOURCE).read_bytes()
    return load_parameter_capability_registry_from_bytes(payload)
