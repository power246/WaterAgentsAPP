import logging
from typing import Annotated

from pydantic import Field

GuidelineRefs = Annotated[tuple[Annotated[str, Field(min_length=1, max_length=64)], ...], Field(max_length=3)] | None

logger = logging.getLogger("wateragents.guidelines")


def sanitize_guideline_refs(value, allowed=frozenset()):
    invalid = False

    def visit(item):
        nonlocal invalid
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        output = {}
        for key, child in item.items():
            if key != "guideline_refs":
                output[key] = visit(child)
                continue
            if child is None:
                output[key] = None
                continue
            refs = []
            if not isinstance(child, (list, tuple)):
                invalid = True
                child = ()
            for alias in child:
                if isinstance(alias, str) and alias in allowed and alias not in refs:
                    refs.append(alias)
                else:
                    invalid = True
            output[key] = refs
        return output

    sanitized = visit(value)
    if invalid:
        logger.warning("Invalid guideline references were discarded.",
            extra={"event_name": "guideline_reference_discarded", "safe_error_code": "guideline_alias_invalid"})
    return sanitized
