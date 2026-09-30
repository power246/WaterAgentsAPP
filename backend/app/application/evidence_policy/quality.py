import re
import unicodedata

from app.application.evidence_policy.errors import EvidencePolicyError
from app.application.evidence_policy.models import QualityAssessment
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.provider_quality import ProviderQuality
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_registry import (
    ParameterCapabilityRegistry,
)
from app.domain.hydrology.parameter_provider import ParameterProvider


def _normalize_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def assess_provider_quality(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    provider: ParameterProvider,
    channel: str,
    provider_quality: ProviderQuality,
    *,
    protocol_valid: bool = True,
    numeric_value_valid: bool = True,
) -> QualityAssessment:
    policy = registry.get_channel_policy(capability.key, provider, channel)
    if policy is None:
        raise EvidencePolicyError(
            "evidence_channel_unregistered",
            "provider channel is not registered for the capability",
        )
    profile = registry.quality_profiles[policy.quality_profile_id]
    raw_tokens = tuple(
        token
        for token in (provider_quality.approval, *provider_quality.qualifiers)
        if token is not None
    )
    normalized_tokens = tuple(_normalize_token(token) for token in raw_tokens)
    valid_tokens = {_normalize_token(token) for token in profile.valid_tokens}
    invalid_tokens = {_normalize_token(token) for token in profile.invalid_tokens}
    suspicious_tokens = {
        _normalize_token(token) for token in profile.suspicious_tokens
    }

    flags: set[DataQualityFlag] = set()
    if not protocol_valid:
        flags.add(DataQualityFlag.INVALID_PROTOCOL)
        quality_class = EvidenceQualityClass.INVALID
    elif not numeric_value_valid:
        flags.add(DataQualityFlag.MISSING)
        quality_class = EvidenceQualityClass.INVALID
    elif any(token in invalid_tokens for token in normalized_tokens):
        flags.add(DataQualityFlag.PROVIDER_FLAGGED)
        quality_class = EvidenceQualityClass.INVALID
    else:
        unknown = set(normalized_tokens) - (
            valid_tokens | invalid_tokens | suspicious_tokens
        )
        suspicious = set(normalized_tokens) & suspicious_tokens
        if suspicious or unknown or not normalized_tokens:
            quality_class = EvidenceQualityClass.SUSPICIOUS
            flags.add(DataQualityFlag.PROVIDER_FLAGGED)
            if "estimated" in suspicious:
                flags.add(DataQualityFlag.ESTIMATED)
            if "censored" in suspicious:
                flags.add(DataQualityFlag.CENSORED)
            if "ice" in suspicious:
                flags.add(DataQualityFlag.POSSIBLE_FREEZING)
            if "equipment" in suspicious:
                flags.add(DataQualityFlag.ANOMALOUS)
        else:
            quality_class = EvidenceQualityClass.VALID

    return QualityAssessment(
        quality_class=quality_class,
        flags=tuple(sorted(flags, key=lambda flag: flag.value)),
        approval=provider_quality.approval,
        qualifiers=provider_quality.qualifiers,
    )
