import json
import re
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainModel, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.mode import InferenceMode
from app.domain.memory_rag.canonical_intent_token import CanonicalIntentToken
from app.domain.memory_rag.canonical_window_descriptor import (
    CanonicalWindowDescriptorV1,
)

MAX_CANONICAL_QUERY_BYTES = 16_384
_PARAMETER_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CanonicalCaseQueryV1(DomainModel):
    schema_version: Literal[1] = 1
    template_version: Literal[1] = 1
    projection_schema_version: Literal[1] = 1
    mode: InferenceMode
    requested_parameter_keys: Annotated[
        tuple[ParameterKey, ...],
        Field(min_length=1, max_length=6),
    ]
    supporting_parameter_keys: tuple[ParameterKey, ...] = ()
    window_descriptors: Annotated[
        tuple[CanonicalWindowDescriptorV1, ...],
        Field(min_length=1, max_length=60),
    ]
    normalized_intent_tokens: Annotated[
        tuple[CanonicalIntentToken, ...],
        Field(min_length=1),
    ]

    @field_validator("requested_parameter_keys", "supporting_parameter_keys")
    @classmethod
    def canonicalize_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        ensure_unique_tuple(value, field_name="canonical parameter keys")
        for parameter in value:
            if not _PARAMETER_PART.fullmatch(
                parameter.code_system
            ) or not _PARAMETER_PART.fullmatch(parameter.code):
                raise ValueError("canonical parameter keys must use bounded ASCII parts")
        return tuple(sorted(value, key=lambda item: (item.code_system, item.code)))

    @field_validator("window_descriptors")
    @classmethod
    def canonicalize_windows(
        cls,
        value: tuple[CanonicalWindowDescriptorV1, ...],
    ) -> tuple[CanonicalWindowDescriptorV1, ...]:
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.window_kind.value,
                    item.duration_days,
                    item.local_months,
                    tuple(season.value for season in item.season_buckets),
                    item.resolution.value,
                ),
            )
        )

    @field_validator("normalized_intent_tokens")
    @classmethod
    def canonicalize_tokens(
        cls,
        value: tuple[CanonicalIntentToken, ...],
    ) -> tuple[CanonicalIntentToken, ...]:
        ensure_unique_tuple(value, field_name="canonical intent tokens")
        order = {token: index for index, token in enumerate(CanonicalIntentToken)}
        return tuple(sorted(value, key=order.__getitem__))

    @model_validator(mode="after")
    def validate_parameter_roles(self) -> Self:
        if set(self.requested_parameter_keys) & set(self.supporting_parameter_keys):
            raise ValueError("requested and supporting parameters must be disjoint")
        return self

    def canonical_bytes(self) -> bytes:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_CANONICAL_QUERY_BYTES:
            raise ValueError("canonical case query exceeds the byte limit")
        return encoded

    @property
    def canonical_text(self) -> str:
        return self.canonical_bytes().decode("ascii")

    @property
    def canonical_digest(self) -> str:
        return f"sha256:{sha256(self.canonical_bytes()).hexdigest()}"
