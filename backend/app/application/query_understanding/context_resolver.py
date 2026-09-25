from __future__ import annotations

from datetime import datetime

from app.application.query_understanding.context_models import (
    AnalysisContext,
    AnalysisContextResolution,
    AnalysisContextResolutionStatus,
    ParameterIntentSource,
    ParsedIntentV1,
    ResolvedParameterIntent,
    SupportingIntentReason,
    SupportingParameterIntent,
)
from app.application.query_understanding.date_resolver import DateResolver
from app.application.query_understanding.location_resolver import LocationResolver
from app.application.query_understanding.models import (
    ParameterResolutionStatus,
    SupportingParameterReason,
)
from app.application.query_understanding.parameter_resolver import ParameterResolver
from app.application.transcripts.models import AnalysisRequestV1
from app.domain import ParameterCapabilityRegistry
from app.domain.common import normalize_utc_datetime
from app.ports import ClockPort


class AnalysisContextResolver:
    def __init__(
        self,
        *,
        clock: ClockPort,
        location_resolver: LocationResolver,
        date_resolver: DateResolver,
        parameter_resolver: ParameterResolver,
        capability_registry: ParameterCapabilityRegistry,
    ) -> None:
        self._clock = clock
        self._location_resolver = location_resolver
        self._date_resolver = date_resolver
        self._parameter_resolver = parameter_resolver
        self._capability_registry = capability_registry

    async def resolve(
        self,
        request: AnalysisRequestV1,
        parsed_intent: ParsedIntentV1,
        *,
        analysis_timezone: str,
        decision_instant: datetime | None = None,
    ) -> AnalysisContextResolution:
        decision_instant = normalize_utc_datetime(
            self._clock.now() if decision_instant is None else decision_instant
        )
        configuration = request.to_configuration_snapshot(analysis_timezone)
        if parsed_intent.ambiguities:
            return self._failure(
                AnalysisContextResolutionStatus.QUERY_CLARIFICATION_REQUIRED
            )

        location = await self._location_resolver.resolve(request.target, parsed_intent)
        if location.target is None:
            return self._failure(
                AnalysisContextResolutionStatus(location.status.value)
            )

        temporal = self._date_resolver.resolve(
            request.date_range,
            parsed_intent,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            resolution_preference=configuration.time_resolution_preference,
            resolution_preference_source=(
                configuration.time_resolution_preference_source
            ),
        )
        if temporal.temporal is None:
            return self._failure(
                AnalysisContextResolutionStatus(temporal.status.value)
            )

        parameters = await self._parameter_resolver.resolve(parsed_intent.parameters)
        if parameters.status is not ParameterResolutionStatus.SUCCESS:
            return self._failure(
                AnalysisContextResolutionStatus(parameters.status.value)
            )

        resolved_parameters = tuple(
            ResolvedParameterIntent(
                key=item.key,
                canonical_name=item.canonical_name,
                canonical_unit=item.canonical_unit,
                source=(
                    ParameterIntentSource.EXPLICIT
                    if any(concept.explicitly_named for concept in item.concepts)
                    else ParameterIntentSource.INFERRED
                ),
                rationale=next(
                    (
                        concept.rationale
                        for concept in item.concepts
                        if concept.explicitly_named
                    ),
                    item.concepts[0].rationale,
                ),
            )
            for item in parameters.target_parameters
        )
        supporting_parameters = tuple(
            SupportingParameterIntent(
                key=item.key,
                canonical_name=item.canonical_name,
                canonical_unit=item.canonical_unit,
                reason=(
                    SupportingIntentReason.CAPABILITY_REQUIRED
                    if item.reason is SupportingParameterReason.CAPABILITY_REQUIRED
                    else SupportingIntentReason.SYSTEM_REQUIRED
                ),
            )
            for item in parameters.supporting_parameters
        )
        facts = temporal.temporal
        context = AnalysisContext(
            query=request.query,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            target=location.target,
            requested_date_range=facts.requested_date_range,
            time_range=facts.time_range,
            time_segments=facts.time_segments,
            mode=facts.mode,
            time_resolution=facts.time_resolution,
            time_resolution_source=facts.time_resolution_source,
            params_used=resolved_parameters,
            supporting_params=supporting_parameters,
            parser_model=request.parser_model,
            waterbody_model=request.waterbody_model,
            memory_mode=request.memory_mode,
        )
        context.validate_capability_registry(self._capability_registry)
        context.validate_configuration(configuration)
        return AnalysisContextResolution(
            status=AnalysisContextResolutionStatus.SUCCESS,
            context=context,
        )

    @staticmethod
    def _failure(
        status: AnalysisContextResolutionStatus,
    ) -> AnalysisContextResolution:
        return AnalysisContextResolution(status=status)
