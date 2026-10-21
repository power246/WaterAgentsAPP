import json
from typing import Annotated
from uuid import uuid4

from pydantic import Field, field_validator

from app.application.canonical_json import canonical_json_digest, canonical_json_text
from app.application.reasoning.model_models import (
    WaterbodyJsonSchema, WaterbodyModelPhase, WaterbodyToolDescriptor,
    WaterbodyModelMessage, WaterbodyMessageRole, ToolCallCompleted,
)
from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, require_safe_model_text


SEARCH_GUIDELINES_SKILL = """You are using the water-quality guideline lookup skill.
Rewrite the original analysis question as one concise professional retrieval query, using
the supplied task and evidence context. Preserve the original goal, parameter identity,
time and spatial bounds, and the distinction between observed and missing information.
Use relevant scientific terms and common synonyms; do not assert an unobserved mechanism
or add a new research goal. Also identify one to three distinct aspects already present in
that question. Each aspect is a self-contained relationship query, retaining necessary
conditions and uncertainty. Do not produce synonymous copies or invent new data needs.
Submit query and aspects only through search_guidelines(query, aspects).
Make exactly one tool call, or return {"decision":"skip"} if lookup is unnecessary.
Never select a provider, scope, profile, catalog, parameter filter, cutoff, or top_k.
Retrieved guidance is advisory, never a measurement or permission to weaken evidence rules.
Each additional evidence request must explain which judgment about the original question
it may change. Auxiliary indicators do not become independent research tasks. Stop when
no new evidence is available or a source is confirmed unavailable; rephrasing is not progress.
Report remaining gaps as uncertainty under the existing point/range eligibility rules."""


class GuidelineSearchArguments(DomainModel):
    query: Annotated[NormalizedText, Field(min_length=1, max_length=400)]
    aspects: Annotated[tuple[Annotated[NormalizedText, Field(min_length=1, max_length=240)], ...], Field(min_length=1, max_length=3)]

    @field_validator("query")
    @classmethod
    def safe_query(cls, value):
        if not value.strip():
            raise ValueError("guideline query must not be blank")
        require_safe_model_text(value)
        return value

    @field_validator("aspects")
    @classmethod
    def safe_aspects(cls, values):
        for value in values:
            cls.safe_query(value)
        return tuple(dict.fromkeys(values))


def search_request(request, original_question):
    require_safe_model_text(original_question)
    descriptor = WaterbodyToolDescriptor(name="search_guidelines",
        description="Find up to three approved guidelines by matching distinct relationship aspects and their coverage.",
        input_schema=WaterbodyJsonSchema(name="search_guidelines",
            schema_text=canonical_json_text(GuidelineSearchArguments.model_json_schema())))
    return request.model_copy(update={
        "operation_id": uuid4(), "phase": WaterbodyModelPhase.EVIDENCE_PLANNING,
        "system_prompt": SEARCH_GUIDELINES_SKILL, "tool_descriptors": (descriptor,),
        "prompt_policy_digest": canonical_json_digest({"skill": SEARCH_GUIDELINES_SKILL, "tool": descriptor.model_dump(mode="json")}),
        "response_schema": WaterbodyJsonSchema(name="guideline_search_decision", schema_text=canonical_json_text({
            "type": "object", "properties": {"decision": {"type": "string", "enum": ["skip"]}},
            "required": ["decision"], "additionalProperties": False})),
        "numeric_plan_ref": None, "numeric_input_refs": (), "batch_id": "guideline-search",
        "messages": (*request.messages, WaterbodyModelMessage(message_id="guideline-search:original",
            role=WaterbodyMessageRole.USER, text=canonical_json_text({"original_question": original_question}))),
    })


def search_messages(event: ToolCallCompleted, result):
    return (
        WaterbodyModelMessage(message_id="guideline-search:call", role=WaterbodyMessageRole.ASSISTANT,
            provider_call_id=event.provider_call_id, tool_name=event.name, text=event.arguments),
        WaterbodyModelMessage(message_id="guideline-search:result", role=WaterbodyMessageRole.TOOL,
            provider_call_id=event.provider_call_id, tool_name=event.name, text=canonical_json_text({
                "status": result.status, "query": json.loads(event.arguments)["query"],
                "aspects": json.loads(event.arguments)["aspects"],
                "versions": [str(match.version.version_id) for match in result.matches],
                "content_location": "advisory_knowledge", "safe_error_code": result.safe_error_code})),
    )
