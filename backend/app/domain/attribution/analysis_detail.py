from pydantic import field_validator

from app.domain.attribution.analysis_step import AnalysisStep
from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class AnalysisDetail(DomainModel):
    summary: NonEmptyString
    steps: tuple[AnalysisStep, ...]
    transcript_item_refs: tuple[NonEmptyString, ...] = ()

    @field_validator("transcript_item_refs")
    @classmethod
    def validate_transcript_item_refs(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="transcript_item_refs")
