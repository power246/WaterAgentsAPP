from decimal import Decimal
from typing import Self

from pydantic import Field, StrictInt, model_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.hydrology.unit_kind import UnitKind
from app.domain.hydrology.unit_normalization import normalize_unit_identity


class UnitDefinition(DomainModel):
    unit_id: NonEmptyString
    symbol: NonEmptyString
    aliases: tuple[NonEmptyString, ...]
    dimension_id: NonEmptyString
    scale_to_base: CanonicalDecimal
    offset_to_base: CanonicalDecimal
    kind: UnitKind
    precision: StrictInt = Field(ge=0, le=18)

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        ensure_unique_tuple(self.aliases, field_name="aliases")
        normalized_names = tuple(
            normalize_unit_identity(value) for value in (self.symbol, *self.aliases)
        )
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("unit symbol and aliases must be unique")
        if Decimal(self.scale_to_base) == 0:
            raise ValueError("unit scale_to_base must not be zero")
        if self.kind == UnitKind.IDENTITY_ONLY and (
            self.scale_to_base != "1" or self.offset_to_base != "0"
        ):
            raise ValueError("identity-only units require scale 1 and offset 0")
        return self
