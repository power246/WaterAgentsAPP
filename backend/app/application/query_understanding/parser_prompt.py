from __future__ import annotations

import json
from datetime import datetime

from pydantic import ValidationError

from app.application.query_understanding.context_models import ParsedIntentV1
from app.application.query_understanding.parser_models import (
    ParserMessageRole,
    ParserPromptMessage,
    QueryParserInputV1,
)
from app.application.transcripts.models import AnalysisRequestV1
from app.application.transcripts.redaction import redact, redact_text
from app.domain import ParameterCapabilityRegistry
from app.domain.common import normalize_utc_datetime
from app.domain.time.timezone import get_iana_timezone


class QueryParserPromptBuilder:
    def __init__(self, registry: ParameterCapabilityRegistry) -> None:
        self._registry = registry

    def build(
        self,
        request: AnalysisRequestV1,
        *,
        decision_instant: datetime,
        analysis_timezone: str,
        private_values: tuple[str, ...] = (),
    ) -> QueryParserInputV1:
        decision = normalize_utc_datetime(decision_instant)
        zone = get_iana_timezone(analysis_timezone)
        schema = ParsedIntentV1.model_json_schema()
        capabilities = tuple(
            {
                "name": capability.canonical_name,
                "unit": capability.canonical_unit,
                "meaning": capability.official_name,
            }
            for capability in sorted(
                self._registry.capabilities,
                key=lambda item: (item.key.code_system, item.key.code),
            )
        )
        system_payload = {
            "task": (
                "Extract candidate location, coordinate, local date range, water "
                "parameter concepts, and material ambiguities. Return only JSON "
                "matching response_schema."
            ),
            "constraints": (
                "Do not output COMID, ParameterKey, UTC boundaries, inference mode, "
                "time resolution, topology, tool calls, or evidence sufficiency. "
                "Do not replace an unsupported concept with a weaker supported one."
            ),
            "response_schema": schema,
            "executable_capabilities": capabilities,
            "few_shots": self._few_shots(),
        }
        source = (
            "frontend"
            if "time_resolution" in request.model_fields_set
            else "default"
        )
        user_payload = {
            "query": request.query,
            "structured_target": (
                request.target.model_dump(mode="json")
                if request.target is not None
                else None
            ),
            "structured_date_range": (
                request.date_range.model_dump(mode="json")
                if request.date_range is not None
                else None
            ),
            "resolution_preference": {
                "value": request.time_resolution.value,
                "source": source,
            },
            "decision": {
                "local_today": decision.astimezone(zone).date().isoformat(),
                "analysis_timezone": analysis_timezone,
            },
        }
        safe_user_payload = redact(user_payload, private_values)
        return QueryParserInputV1(
            model=request.parser_model,
            messages=(
                ParserPromptMessage(
                    role=ParserMessageRole.SYSTEM,
                    text=self._json(system_payload),
                ),
                ParserPromptMessage(
                    role=ParserMessageRole.USER,
                    text=self._json(safe_user_payload),
                ),
            ),
        )

    def build_repair(
        self,
        model_request: QueryParserInputV1,
        invalid_output: str,
        validation_error: ValidationError,
        *,
        private_values: tuple[str, ...] = (),
    ) -> QueryParserInputV1:
        schema = ParsedIntentV1.model_json_schema()
        summary = tuple(
            {
                "path": ".".join(str(part) for part in issue["loc"]),
                "type": issue["type"],
            }
            for issue in validation_error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
        system_payload = {
            "task": "Repair the supplied output and return only schema-valid JSON.",
            "response_schema": schema,
            "constraints": (
                "Do not add COMID, ParameterKey, UTC boundaries, inference mode, "
                "time resolution, topology, tools, or evidence conclusions."
            ),
        }
        user_payload = {
            "invalid_output": redact_text(invalid_output, private_values),
            "validation_errors": summary,
        }
        return QueryParserInputV1(
            model=model_request.model,
            messages=(
                ParserPromptMessage(
                    role=ParserMessageRole.SYSTEM,
                    text=self._json(system_payload),
                ),
                ParserPromptMessage(
                    role=ParserMessageRole.USER,
                    text=self._json(user_payload),
                ),
            ),
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _few_shots() -> tuple[dict[str, object], ...]:
        return (
            {
                "input": "Check dissolved oxygen tomorrow.",
                "output": {
                    "parameters": [
                        {
                            "concept": "dissolved oxygen",
                            "rationale": "Dissolved oxygen is explicitly requested.",
                            "explicitly_named": True,
                        }
                    ],
                    "ambiguities": [],
                },
            },
            {
                "input": "Will fish be able to breathe after the heat wave?",
                "output": {
                    "parameters": [
                        {
                            "concept": "dissolved oxygen",
                            "rationale": "Oxygen availability is relevant to fish respiration.",
                            "explicitly_named": False,
                        }
                    ],
                    "ambiguities": [],
                },
            },
            {
                "input": "Check the river by Springfield.",
                "output": {
                    "parameters": [
                        {
                            "concept": "water temperature",
                            "rationale": "Temperature is required by the stated check.",
                            "explicitly_named": False,
                        }
                    ],
                    "ambiguities": [
                        {
                            "subject": "location",
                            "message": "Springfield does not identify one location.",
                        }
                    ],
                },
            },
        )
