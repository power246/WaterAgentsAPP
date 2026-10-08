"""M9 domain contract."""

from typing import Self
from pydantic import field_validator, model_validator
from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory import MemoryMode
from app.domain.time.time_resolution_preference import TimeResolutionPreference
from app.domain.time.timezone import get_iana_timezone
from .embedding_profile_option import EmbeddingProfileOption
from .model_option import ModelOption
from .unit_parameter_option import UnitParameterOption


class SettingsCatalogProjection(DomainModel):
    models: tuple[ModelOption, ...]
    embedding_profiles: tuple[EmbeddingProfileOption, ...]
    memory_modes: tuple[MemoryMode, ...]
    time_resolutions: tuple[TimeResolutionPreference, ...]
    iana_timezones: tuple[NonEmptyString, ...]
    unit_options: tuple[UnitParameterOption, ...] = ()

    @field_validator("iana_timezones")
    @classmethod
    def validate_timezones(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            get_iana_timezone(value)
        return values

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        for field in ("memory_modes", "time_resolutions", "iana_timezones"):
            values = getattr(self, field)
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{field} must be nonempty and unique")
        selections = tuple(option.selection for option in self.models)
        profiles = tuple(option.profile_id for option in self.embedding_profiles)
        if len(selections) != len(set(selections)) or len(profiles) != len(set(profiles)):
            raise ValueError("catalog identities must be unique")
        return self
