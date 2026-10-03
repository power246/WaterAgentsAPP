"""Opt-in live smoke for one minimal M6 OpenAI waterbody contract.

Set WATERAGENTS_M6_PROVIDER_SMOKE=1 and WATERAGENTS_M6_SMOKE_MODEL before
invoking this script. The JSON output contains counts only, never prompts,
provider bodies, URLs, or credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from app.application.analysis_runs.workflow import CancellationToken
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.domain import ItemReference, ModelCatalogSnapshot, ModelRole, ModelSelection
from app.infrastructure.adapters import OpenAIResponsesWaterbodyAdapter
from app.infrastructure.config import AppSettings
from app.infrastructure.http import ProviderHttpClient
from app.ports import WaterbodyModelPortError


OPT_IN = "WATERAGENTS_M6_PROVIDER_SMOKE"
MODEL_ENV = "WATERAGENTS_M6_SMOKE_MODEL"
OPERATION_ID = UUID("60000000-0000-4000-8000-000000000011")


def _environment(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _not_run(reason: str) -> dict[str, object]:
    return {
        "provider": "openai",
        "status": "not_run",
        "reason": reason,
        "requests": 0,
        "responses": 0,
        "events": 0,
        "visible_events": 0,
        "tool_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
    }


async def _network_preflight(settings: AppSettings) -> None:
    host = urlsplit(settings.openai_base_url).hostname
    if host is None:
        raise ValueError("configured provider origin is invalid")
    try:
        await asyncio.get_running_loop().getaddrinfo(host, 443)
    except OSError as error:
        raise ValueError("provider network preflight failed") from error


def _selection(settings: AppSettings) -> ModelSelection:
    model = _environment(MODEL_ENV)
    if model is None:
        raise ValueError(f"missing {MODEL_ENV}")
    selection = ModelSelection(provider="openai", model=model)
    catalog = ModelCatalogSnapshot(capabilities=settings.model_catalog)
    if not catalog.supports(selection, ModelRole.WATERBODY):
        raise ValueError("selected model is not catalog-authorized for waterbody")
    return selection


async def _run() -> dict[str, object]:
    settings = AppSettings()
    if settings.openai_api_key is None:
        raise ValueError("OpenAI credential is not configured")
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise ValueError("OpenAI credential is not configured")
    selection = _selection(settings)
    await _network_preflight(settings)
    deadline = datetime.now(UTC) + timedelta(
        seconds=min(
            settings.reasoning_model_timeout_seconds,
            settings.reasoning_session_deadline_seconds,
        )
    )
    request = WaterbodyModelRequest(
        operation_id=OPERATION_ID,
        phase=WaterbodyModelPhase.REASONING_BATCH,
        model=selection,
        system_prompt="Return the requested strict JSON object.",
        messages=(
            WaterbodyModelMessage(
                message_id="m6-provider-smoke-message",
                role=WaterbodyMessageRole.USER,
                text='Return {"status":"ok"}.',
            ),
        ),
        response_schema=WaterbodyJsonSchema(
            name="m6_provider_smoke",
            schema_text=json.dumps(
                {
                    "type": "object",
                    "properties": {"status": {"const": "ok"}},
                    "required": ["status"],
                    "additionalProperties": False,
                }
            ),
        ),
        prompt_policy_digest="m6-provider-smoke-policy",
        scope_digest="m6-provider-smoke-scope",
        evidence_bundle_ref=ItemReference(item_id="m6-provider-smoke-bundle"),
        batch_id="m6-provider-smoke-batch",
        deadline=deadline,
    )
    async with ProviderHttpClient(
        timeout_seconds=settings.reasoning_model_timeout_seconds,
        max_response_bytes=settings.provider_http_max_response_bytes,
        retry_delay_seconds=settings.provider_http_retry_delay_seconds,
        attempts=1,
    ) as http:
        response = await consume_waterbody_model_stream(
            OpenAIResponsesWaterbodyAdapter(
                http,
                api_key=api_key,
                base_url=settings.openai_base_url,
            ),
            request,
            cancellation=CancellationToken(asyncio.Event()),
        )
    return {
        "provider": selection.provider,
        "model": selection.model,
        "status": "passed",
        "reason": None,
        "requests": 1,
        "responses": 1,
        "events": len(response.events),
        "visible_events": sum(
            isinstance(event, VisibleTextDelta) for event in response.events
        ),
        "tool_calls": sum(
            isinstance(event, ToolCallCompleted) for event in response.events
        ),
        "structured_outputs": sum(
            isinstance(event, StructuredOutputCompleted) for event in response.events
        ),
        "input_tokens": response.usage.input_tokens if response.usage else None,
        "output_tokens": response.usage.output_tokens if response.usage else None,
    }


async def _main() -> dict[str, object]:
    if _environment(OPT_IN) != "1":
        return _not_run(f"set {OPT_IN}=1 to opt in")
    try:
        return await _run()
    except ValidationError:
        return _not_run("provider smoke settings are invalid")
    except ValueError as error:
        return _not_run(str(error))
    except WaterbodyModelPortError as error:
        result = _not_run(error.code)
        result["status"] = "failed"
        result["requests"] = 1
        return result
    except Exception:
        result = _not_run("waterbody_model_response_invalid")
        result["status"] = "failed"
        result["requests"] = 1
        return result


def main() -> int:
    result = asyncio.run(_main())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
