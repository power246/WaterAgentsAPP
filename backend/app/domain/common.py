from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Hashable, TypeAlias, TypeVar

from pydantic import (
    AfterValidator,
    BeforeValidator,
    JsonValue as PydanticJsonValue,
    StringConstraints,
)

from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel

ItemT = TypeVar("ItemT", bound=Hashable)

NonEmptyString: TypeAlias = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
JsonValue: TypeAlias = Annotated[
    PydanticJsonValue,
    BeforeValidator(deepcopy),
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
