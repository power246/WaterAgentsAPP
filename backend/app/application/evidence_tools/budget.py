from app.application.evidence_tools.errors import EvidenceToolBudgetExceededError


class EvidenceBudgetLedger:
    """Counts canonical work; aliases cannot manufacture a second allowance."""

    def __init__(self, *, collection_limit: int, transform_limit: int) -> None:
        if collection_limit < 1 or transform_limit < 1:
            raise ValueError("evidence budget limits must be positive")
        self._limits = {
            "collection": collection_limit,
            "transform": transform_limit,
        }
        self._keys: dict[str, set[str]] = {
            "collection": set(),
            "transform": set(),
        }

    def reserve(self, kind: str, canonical_key: str) -> bool:
        if kind not in self._keys:
            raise ValueError("unknown evidence budget kind")
        keys = self._keys[kind]
        if canonical_key in keys:
            return False
        if len(keys) >= self._limits[kind]:
            raise EvidenceToolBudgetExceededError()
        keys.add(canonical_key)
        return True

    def used(self, kind: str) -> int:
        if kind not in self._keys:
            raise ValueError("unknown evidence budget kind")
        return len(self._keys[kind])
