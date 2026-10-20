from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.application.canonical_json import canonical_json_digest
from app.application.run_summary.draft_models import (
    RunSummaryDraft,
    ValidatedRunSummaryDraft,
)
from app.application.run_summary.errors import RunSummaryValidationError
from app.application.run_summary.models import (
    RunSummaryHighlight,
    RunSummaryInput,
    RunSummaryResult,
)
from app.domain import ModelSelection, ModelUsageSummary

_URL = re.compile(
    r"(?i)(?:https?://|www\.|\b[a-z0-9][a-z0-9.-]*\.(?:com|org|net|io|gov|edu)(?:/|\b))"
)
_COMID = re.compile(r"(?i)\bCOMID\b\s*[:#]?\s*([A-Za-z0-9._-]+)")
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
    r"(?![A-Za-z0-9_])"
)
_WINDOW_ID_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?:window[-_][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*|w\d+[A-Za-z0-9_-]*)"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])"
)
_RISK_CLAIM = re.compile(
    r"(?i)(?:\b(none|low|medium|high|indeterminate)[ -]risk\b|"
    r"\brisk\s+(?:is|was|=|:)\s*(none|low|medium|high|indeterminate)\b)"
)
_CJK = re.compile(r"[\u3400-\u9fff]")
_FORBIDDEN_TEXT = (
    "api key",
    "authorization bearer",
    "chain-of-thought",
    "hidden reasoning",
    "internal log",
    "stack trace",
    "system prompt",
    "traceback",
)


class RunSummaryValidator:
    def validate(
        self,
        summary_input: RunSummaryInput,
        output: str,
        *,
        model: ModelSelection,
        usage: ModelUsageSummary | None,
        generated_at: datetime,
    ) -> tuple[ValidatedRunSummaryDraft, RunSummaryResult]:
        draft = self._parse(output)
        issues: list[tuple[str, str]] = []
        expected_comids = (
            *(card.target_comid for card in summary_input.report_cards),
            summary_input.target_comid,
        )
        if draft.language != query_language(summary_input.query):
            issues.append(("language_mismatch", "$.language"))
        if draft.target_comid != summary_input.target_comid:
            issues.append(("target_mismatch", "$.target_comid"))
        if draft.graph_coverage is not summary_input.graph_coverage:
            issues.append(("coverage_mismatch", "$.graph_coverage"))
        if draft.covered_comids != expected_comids:
            issues.append(("active_scope_mismatch", "$.covered_comids"))

        allowed_comids = set(expected_comids)
        allowed_risks = {
            summary_input.target_result_projection.overall_risk.value,
            *(
                card.overall_risk.value
                for card in summary_input.report_cards
                if card.overall_risk is not None
            ),
            *(
                window.overall_risk.value
                for projection in (
                    summary_input.target_result_projection,
                    *summary_input.report_cards,
                )
                for window in projection.window_summaries
            ),
        }
        for path, text in (
            ("$.plain_language_summary", draft.plain_language_summary),
            *(
                (f"$.highlights[{index}].text", item.text)
                for index, item in enumerate(draft.highlights)
            ),
        ):
            if _URL.search(text):
                issues.append(("url_forbidden", path))
            folded = text.casefold()
            if any(term in folded for term in _FORBIDDEN_TEXT):
                issues.append(("internal_detail_forbidden", path))
            unknown = set(_COMID.findall(text)) - allowed_comids
            if unknown:
                issues.append(("comid_outside_scope", path))
            claimed_risks = {
                next(value for value in match.groups() if value is not None).casefold()
                for match in _RISK_CLAIM.finditer(text)
            }
            if not claimed_risks <= allowed_risks:
                issues.append(("unsupported_risk_assertion", path))

        allowed_refs = set(summary_input.allowed_source_refs)
        numeric_facts = _numeric_fact_catalog(summary_input)
        allowed_window_ids = {
            window.window_id
            for projection in (
                summary_input.target_result_projection,
                *summary_input.report_cards,
            )
            for window in projection.window_summaries
        }
        allowed_window_ids.update(
            reference.window_id
            for reference in allowed_refs
            if reference.window_id is not None
        )
        known_window_pattern = _known_window_pattern(allowed_window_ids)
        if _unsupported_numeric_tokens(
            draft.plain_language_summary,
            allowed_comids=allowed_comids,
            known_window_pattern=known_window_pattern,
        ):
            issues.append(
                ("unsupported_numeric_assertion", "$.plain_language_summary")
            )
        for index, item in enumerate(draft.highlights):
            path = f"$.highlights[{index}]"
            if item.source_ref not in allowed_refs:
                issues.append(("source_ref_outside_input", path + ".source_ref"))
                continue
            numeric = item.display_value is not None or item.value_lower is not None
            numeric_tokens = _unsupported_numeric_tokens(
                item.text,
                allowed_comids=allowed_comids,
                known_window_pattern=known_window_pattern,
            )
            if numeric:
                expected = numeric_facts.get(item.source_ref)
                actual = (
                    item.display_value, item.value_lower, item.value_upper,
                    item.display_unit, item.window_id,
                )
                if expected is None or actual != expected:
                    issues.append(("numeric_fact_mismatch", path))
                if any(token not in {
                    item.display_value, item.value_lower, item.value_upper
                } for token in numeric_tokens):
                    issues.append(("unsupported_numeric_assertion", path + ".text"))
            elif numeric_tokens:
                issues.append(("unsupported_numeric_assertion", path + ".text"))

        if issues:
            raise RunSummaryValidationError(tuple(issues))

        validated = ValidatedRunSummaryDraft(
            draft=draft,
            validation_digest=canonical_json_digest(draft.model_dump(mode="json")),
        )
        result = RunSummaryResult(
            run_id=summary_input.run_id,
            session_id=summary_input.session_id,
            input_digest=summary_input.input_digest,
            model=model,
            language=draft.language,
            plain_language_summary=draft.plain_language_summary,
            highlights=tuple(
                RunSummaryHighlight(
                    kind=item.kind,
                    text=item.text,
                    source_ref=item.source_ref,
                    display_value=item.display_value,
                    value_lower=item.value_lower,
                    value_upper=item.value_upper,
                    display_unit=item.display_unit,
                    window_id=item.window_id,
                )
                for item in draft.highlights
            ),
            usage=usage,
            generated_at=generated_at,
        )
        summary_input.validate_result(result)
        return validated, result

    @staticmethod
    def _parse(output: str) -> RunSummaryDraft:
        try:
            parsed = json.loads(output, object_pairs_hook=_reject_duplicates)
            return RunSummaryDraft.model_validate(parsed)
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            issues: list[tuple[str, str]] = []
            if isinstance(error, ValidationError):
                for item in error.errors(include_url=False):
                    location = ".".join(str(part) for part in item["loc"])
                    issues.append(("invalid_output", "$" + ("." + location if location else "")))
            if not issues:
                issues.append(("invalid_json", "$"))
            raise RunSummaryValidationError(tuple(issues)) from None


def query_language(query: str) -> str:
    return "zh-CN" if _CJK.search(query) else "en"


def _unsupported_numeric_tokens(
    text: str,
    *,
    allowed_comids: set[str],
    known_window_pattern: re.Pattern[str] | None,
) -> tuple[str, ...]:
    without_known_windows = (
        known_window_pattern.sub(" ", text)
        if known_window_pattern is not None
        else text
    )
    unknown_windows = _WINDOW_ID_TOKEN.findall(without_known_windows)
    other_numbers = tuple(
        token
        for token in _NUMBER.findall(without_known_windows)
        if token not in allowed_comids
    )
    return (*unknown_windows, *other_numbers)


def _known_window_pattern(window_ids: set[str]) -> re.Pattern[str] | None:
    if not window_ids:
        return None
    alternatives = "|".join(
        re.escape(window_id) for window_id in sorted(window_ids, key=len, reverse=True)
    )
    return re.compile(
        rf"(?<![A-Za-z0-9_.-])(?:{alternatives})"
        r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])"
    )


def _numeric_fact_catalog(
    summary_input: RunSummaryInput,
) -> dict[object, tuple[str | None, str | None, str | None, str, str]]:
    result: dict[object, tuple[str | None, str | None, str | None, str, str]] = {}
    target = summary_input.target_result_projection
    if target.full_result is not None:
        points = {
            point.cell_id: point
            for series in target.full_result.series
            for point in series.points
        }
        for reference in target.source_refs:
            point = points.get(reference.cell_id)
            if (
                point is not None
                and (point.representative_value is not None or point.value_lower is not None)
                and point.unit_id is not None
                and reference.window_id is not None
            ):
                result[reference] = (
                    point.representative_value,
                    point.value_lower,
                    point.value_upper,
                    point.unit_id,
                    reference.window_id,
                )
    for projection in (target, *summary_input.report_cards):
        for finding in projection.notable_findings:
            if (
                (finding.display_value is not None or finding.value_lower is not None)
                and finding.display_unit is not None
                and finding.window_id is not None
            ):
                result[finding.source_ref] = (
                    finding.display_value,
                    finding.value_lower,
                    finding.value_upper,
                    finding.display_unit,
                    finding.window_id,
                )
    return result


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
