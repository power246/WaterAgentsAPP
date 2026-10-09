"""M9 domain contract."""

from pydantic import field_validator
from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory import MemoryMode
from app.domain.model_selection import ModelSelection
from app.domain.time.time_resolution_preference import TimeResolutionPreference
from app.domain.time.timezone import get_iana_timezone
from .primitives import ProfileId


class MutableDefaults(DomainModel):
    analysis_timezone: NonEmptyString
    parser_model: ModelSelection
    waterbody_model: ModelSelection
    memory_mode: MemoryMode
    time_resolution: TimeResolutionPreference
    desired_embedding_profile_id: ProfileId

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value
