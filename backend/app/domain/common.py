from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from typing import Annotated, Hashable, TypeAlias, TypeVar

from pydantic import (
    AfterValidator,
    BeforeValidator,
    JsonValue as PydanticJsonValue,
    StrictFloat,
    StringConstraints,
)

from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel

ItemT = TypeVar("ItemT", bound=Hashable)
DomainFloat: TypeAlias = StrictFloat

NonEmptyString: TypeAlias = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


def ensure_finite_json_numbers(value: object) -> object:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, list):
        for item in value:
            ensure_finite_json_numbers(item)
    elif isinstance(value, dict):
        for item in value.values():
            ensure_finite_json_numbers(item)
    return value


JsonValue: TypeAlias = Annotated[
    PydanticJsonValue,
    BeforeValidator(deepcopy),
    AfterValidator(ensure_finite_json_numbers),
]


def normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(timezone.utc)


UtcDatetime: TypeAlias = Annotated[
    datetime,
    AfterValidator(normalize_utc_datetime),
]


def ensure_unique_tuple(
    values: tuple[ItemT, ...],
    *,
    field_name: str,
) -> tuple[ItemT, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values
