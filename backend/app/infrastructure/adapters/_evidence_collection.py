from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain import (
    CollectionCompletionContract,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    ProviderCollectionBatch,
    TimeRange,
)
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    UnsupportedPortVersionError,
)

RecordT = TypeVar("RecordT")


def request_identity(
    url: str,
    params: Mapping[str, object] | None = None,
) -> str:
    parsed = urlsplit(url)
    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    if params:
        for name, raw in params.items():
            values = raw if isinstance(raw, tuple) else (raw,)
            pairs.extend((name, str(value)) for value in values)
    safe_pairs = [
        (name, value)
        for name, value in pairs
        if name.casefold() not in {"api_key", "apikey", "key", "token"}
    ]
    canonical = urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            urlencode(sorted(safe_pairs)),
            "",
        )
    )
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


def build_batch(
    *,
    operation_id: str,
    records: Sequence[RecordT],
    pages: Sequence[CollectionPageManifest],
    request_count: int,
    complete: bool,
    completed_ranges: tuple[TimeRange, ...],
    contract: CollectionCompletionContract,
    incomplete_reason: str | None = None,
) -> ProviderCollectionBatch[RecordT]:
    terminal = pages[-1].page_identity if complete else None
    proof_payload = {
        "contract": contract.value,
        "complete": complete,
        "terminal_identity": terminal,
        "pages": [page.model_dump(mode="json") for page in pages],
    }
    digest = "sha256:" + sha256(
        json.dumps(
            proof_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return ProviderCollectionBatch[RecordT](
        operation_id=operation_id,
        records=tuple(records),
        page_or_chunk_manifests=tuple(pages),
        completed_ranges=completed_ranges,
        counts=CollectionCounts(
            requests=request_count,
            pages=len(pages),
            records=len(records),
            response_bytes=sum(page.response_bytes for page in pages),
        ),
        completion_proof=CollectionCompletionProof(
            contract=contract,
            complete=complete,
            terminal_identity=terminal,
            digest=digest,
        ),
        incomplete_reason=incomplete_reason,
    )


def safe_error_code(error: PortError) -> str:
    if isinstance(error, PortAuthenticationError):
        return "evidence_provider_authentication_failed"
    if isinstance(error, PortRateLimitError):
        return "evidence_provider_rate_limited"
    if isinstance(error, PortTimeoutError):
        return "evidence_provider_timeout"
    if isinstance(error, PortResponseLimitError):
        return "evidence_provider_response_too_large"
    if isinstance(error, (InvalidPortResponseError, UnsupportedPortVersionError)):
        return "evidence_provider_response_invalid"
    if isinstance(
        error,
        (PortUnavailableError, PortHttpError, PortConfigurationError),
    ):
        return "evidence_provider_unavailable"
    return "evidence_provider_unavailable"
