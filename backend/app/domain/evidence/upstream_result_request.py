from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey


class UpstreamResultRequest(DomainModel):
    operation_id: NonEmptyString
    run_id: NonEmptyString
    session_id: NonEmptyString
    agent_comid: NonEmptyString
    direct_upstream_comids: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    affected_window_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        for field_name in (
            "direct_upstream_comids",
            "parameter_keys",
            "affected_window_ids",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        return self
