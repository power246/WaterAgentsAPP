from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Hashable, TypeAlias, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    JsonValue as PydanticJsonValue,
    StringConstraints,
)

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


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class DomainEnum(str, Enum):
    def __new__(cls, value: str) -> "DomainEnum":
        if not isinstance(value, str) or not value or value != value.lower():
            raise ValueError("domain enum values must be non-empty lowercase strings")
        member = str.__new__(cls, value)
        member._value_ = value
        return member
