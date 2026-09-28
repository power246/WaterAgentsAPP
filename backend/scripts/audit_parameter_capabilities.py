import argparse
import asyncio
import json
from collections.abc import Sequence

import httpx

from app.domain.hydrology import EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256
from app.infrastructure.adapters import UsgsWaterDataAdapter
from app.infrastructure.config import (
    load_parameter_capability_registry,
)
from app.infrastructure.http import ProviderHttpClient


def audit_parameter_capabilities() -> dict[str, object]:
    registry = load_parameter_capability_registry()
    keys = tuple(
        f"{capability.key.code_system}:{capability.key.code}"
        for capability in registry.capabilities
    )
    required_support_keys = tuple(
        f"{capability.key.code_system}:{capability.key.code}"
        for capability in registry.required_support
    )
    return {
        "capability_count": len(registry.capabilities),
        "catalog_key_set_sha256": EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256,
        "group_counts": dict(registry.group_counts),
        "required_support_count": len(required_support_keys),
        "required_support_keys": required_support_keys,
        "resolution_enabled_count": sum(
            capability.resolution_enabled
            for capability in registry.capabilities
        ),
        "unique_key_count": len(set(keys)),
    }


async def compare_official_parameter_catalog(
    adapter: UsgsWaterDataAdapter,
) -> dict[str, object]:
    """Report WDFN metadata drift without modifying the checked-in manifest."""
    registry = load_parameter_capability_registry()
    expected = {capability.key: capability for capability in registry.capabilities}
    records = await adapter.get_parameter_codes(tuple(expected))
    actual = {record.key: record for record in records}
    missing = sorted(
        f"{key.code_system}:{key.code}" for key in set(expected) - set(actual)
    )
    unexpected = sorted(
        f"{key.code_system}:{key.code}" for key in set(actual) - set(expected)
    )
    name_drift = []
    unit_drift = []
    for key in sorted(set(expected) & set(actual), key=lambda item: item.code):
        capability = expected[key]
        record = actual[key]
        identity = f"{key.code_system}:{key.code}"
        if record.name != capability.official_name:
            name_drift.append(
                {
                    "key": identity,
                    "manifest": capability.official_name,
                    "official": record.name,
                }
            )
        if record.unit != capability.official_unit:
            unit_drift.append(
                {
                    "key": identity,
                    "manifest": capability.official_unit,
                    "official": record.unit,
                }
            )
    drift = bool(missing or unexpected or name_drift or unit_drift)
    return {
        "api_version": "USGS Water Data OGC API v1",
        "status": "drift" if drift else "no_drift",
        "requested_count": len(expected),
        "received_count": len(actual),
        "missing_keys": missing,
        "unexpected_keys": unexpected,
        "name_drift": name_drift,
        "unit_drift": unit_drift,
        "manifest_updated": False,
    }


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport()
        self.request_count = 0

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.request_count += 1
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


async def refresh_official_catalog(timeout_seconds: float) -> dict[str, object]:
    transport = CountingTransport()
    async with ProviderHttpClient(
        timeout_seconds=timeout_seconds,
        max_response_bytes=2_000_000,
        retry_delay_seconds=0.25,
        transport=transport,
    ) as client:
        result = await compare_official_parameter_catalog(
            UsgsWaterDataAdapter(
                client,
                page_size=1000,
                max_pages=2,
                max_records=2000,
            )
        )
        result["request_count"] = transport.request_count
        return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the M4 parameter capability manifest.",
    )
    parser.add_argument(
        "--refresh-official",
        action="store_true",
        help=(
            "query the official USGS OGC v1 parameter catalog and report drift; "
            "never modifies the checked-in manifest"
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10,
        help="per-request timeout for the optional official refresh",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    result = audit_parameter_capabilities()
    if args.refresh_official:
        result["official_refresh"] = asyncio.run(
            refresh_official_catalog(args.timeout_seconds)
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
