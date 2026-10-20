import asyncio
import json
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from app.domain import GeoPoint
from app.infrastructure.adapters import NldiAdapter
from app.infrastructure.adapters.nldi_request_state import NldiRequestState
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortHttpError,
    PortRateLimitError,
    PortTimeoutError,
)

FIXTURES = Path(__file__).with_name("fixtures")
BASE = "https://provider.test/nldi/linked-data"


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def short_reach_payloads():
    origin = fixture("nldi_position.json")
    navigation = fixture("nldi_upstreams.json")
    coordinates = [[-77.2, 38.8], [-77.199, 38.801]]
    origin["features"][0]["geometry"]["coordinates"] = coordinates
    navigation["features"][0]["geometry"]["coordinates"] = coordinates
    return origin, navigation


def run(coroutine):
    return asyncio.run(coroutine)


def provider_client(handler) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=200_000,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def test_position_uses_point_in_polygon_with_lon_lat_order() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=fixture("nldi_position.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            result = await NldiAdapter(http, base_url=BASE).lookup_position(
                GeoPoint(latitude=38.75, longitude=-77.25)
            )
            assert result == "12345678"

    run(exercise())
    assert requests[0].url.path.endswith("/comid/position")
    assert requests[0].url.params["coords"] == "POINT(-77.25 38.75)"
    assert "hydrolocation" not in str(requests[0].url)


def test_shared_state_coalesces_same_position_across_runs() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            first = NldiAdapter(http, base_url=BASE, shared_state=state)
            second = NldiAdapter(http, base_url=BASE, shared_state=state)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            assert await asyncio.gather(
                first.lookup_position(point), second.lookup_position(point)
            ) == ["12345678", "12345678"]
            assert await second.lookup_position(point) == "12345678"

    run(exercise())
    assert len(requests) == 1


def test_shared_state_spaces_requests_from_different_runs() -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState()
            first = NldiAdapter(
                http, base_url=BASE, request_interval_seconds=0.05,
                shared_state=state,
            )
            second = NldiAdapter(
                http, base_url=BASE, request_interval_seconds=0.05,
                shared_state=state,
            )
            await asyncio.gather(
                first.lookup_position(GeoPoint(latitude=38.75, longitude=-77.25)),
                second.lookup_position(GeoPoint(latitude=38.76, longitude=-77.26)),
            )

    run(exercise())
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.04


def test_shared_state_reuses_completed_upstreams_but_keys_distance() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = (
            fixture("nldi_upstreams.json")
            if request.url.path.endswith("/navigation/UT/flowlines")
            else fixture("nldi_position.json")
        )
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            first = NldiAdapter(http, base_url=BASE, shared_state=state)
            second = NldiAdapter(http, base_url=BASE, shared_state=state)
            expected = await first.get_direct_upstreams("12345678", 12.5)
            assert await second.get_direct_upstreams("12345678", 12.5) == expected
            assert len(requests) == 2
            assert await second.get_direct_upstreams("12345678", 13.5) == expected

    run(exercise())
    assert len(requests) == 4


def test_shared_state_caches_empty_sites_but_not_failed_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, request=request)
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": []},
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            first = NldiAdapter(http, base_url=BASE, shared_state=state)
            second = NldiAdapter(http, base_url=BASE, shared_state=state)
            with pytest.raises(PortHttpError):
                await first.get_monitoring_sites("12345678")
            assert await second.get_monitoring_sites("12345678") == ()
            assert await first.get_monitoring_sites("12345678") == ()

    run(exercise())
    assert len(requests) == 2


def test_shared_state_keeps_rate_limit_cooldown_after_deadline() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        if starts == 1:
            return httpx.Response(
                429, headers={"Retry-After": "1"}, request=request
            )
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            first = NldiAdapter(http, base_url=BASE, shared_state=state)
            second = NldiAdapter(http, base_url=BASE, shared_state=state)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            loop = asyncio.get_running_loop()
            with pytest.raises(PortTimeoutError):
                await first.lookup_position(point, deadline=loop.time() + 0.1)
            async with state.request_lock:
                with pytest.raises(PortRateLimitError) as error:
                    await second.lookup_position(point, deadline=loop.time() + 0.1)
            assert error.value.retry_after_seconds is not None
            assert error.value.retry_after_seconds > 0
            assert starts == 1
            await asyncio.sleep(1.01)
            assert await second.lookup_position(point) == "12345678"

    run(exercise())
    assert starts == 2


def test_missing_retry_after_starts_fallback_cooldown_without_early_retry() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        return httpx.Response(429, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            adapter = NldiAdapter(http, base_url=BASE, shared_state=state)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            loop = asyncio.get_running_loop()
            with pytest.raises(PortTimeoutError):
                await adapter.lookup_position(point, deadline=loop.time() + 0.1)
            assert state.rate_limited_until - loop.time() > 29
            with pytest.raises(PortRateLimitError):
                await adapter.lookup_position(point, deadline=loop.time() + 0.1)

    run(exercise())
    assert starts == 1


def test_explicit_zero_retry_after_does_not_start_fallback_cooldown() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        if starts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            adapter = NldiAdapter(http, base_url=BASE, shared_state=state)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            assert await adapter.lookup_position(point) == "12345678"
            assert state.rate_limited_until <= asyncio.get_running_loop().time()

    run(exercise())
    assert starts == 2


def test_cancelled_shared_cache_owner_allows_waiter_to_fetch() -> None:
    started = asyncio.Event()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState(cache_ttl_seconds=60)
            first = NldiAdapter(http, base_url=BASE, shared_state=state)
            second = NldiAdapter(http, base_url=BASE, shared_state=state)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            owner = asyncio.create_task(first.lookup_position(point))
            await started.wait()
            waiter = asyncio.create_task(second.lookup_position(point))
            await asyncio.sleep(0)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert await waiter == "12345678"

    run(exercise())
    assert requests == 2


def test_concurrent_nldi_requests_are_spaced_on_one_adapter() -> None:
    starts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(
                http, base_url=BASE, request_interval_seconds=0.05
            )
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            results = await asyncio.gather(
                *(adapter.lookup_position(point) for _ in range(4))
            )
            assert results == ["12345678"] * 4

    run(exercise())
    assert len(starts) == 4
    assert all(later - earlier >= 0.04 for earlier, later in pairwise(starts))


def test_slow_nldi_response_does_not_add_another_request_interval() -> None:
    starts: list[float] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    released_at = 0.0

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            first_started.set()
            await release_first.wait()
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        nonlocal released_at
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE, request_interval_seconds=0.2)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            first = asyncio.create_task(adapter.lookup_position(point))
            await first_started.wait()
            second = asyncio.create_task(adapter.lookup_position(point))
            await asyncio.sleep(0.25)
            released_at = asyncio.get_running_loop().time()
            release_first.set()
            assert await asyncio.gather(first, second) == ["12345678"] * 2

    run(exercise())
    assert starts[1] - starts[0] >= 0.19
    assert starts[1] - released_at < 0.12


def test_nldi_spacing_survives_delayed_http_dispatch() -> None:
    starts: list[float] = []
    attempts: list[float] = []
    interval = 0.05

    class DelayedProviderHttpClient(ProviderHttpClient):
        calls = 0

        async def get_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.03)
            return await super().get_json(*args, **kwargs)

    class RecordingNldiAdapter(NldiAdapter):
        def _mark_request_attempt(self) -> None:
            super()._mark_request_attempt()
            attempts.append(self._request_state.next_request_time - interval)

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with DelayedProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=200_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            adapter = RecordingNldiAdapter(
                http, base_url=BASE, request_interval_seconds=interval
            )
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            assert await asyncio.gather(
                adapter.lookup_position(point), adapter.lookup_position(point)
            ) == ["12345678", "12345678"]

    run(exercise())
    assert len(starts) == 2
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= interval - 0.000001


def test_nldi_slot_rechecks_if_sleep_wakes_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            state = NldiRequestState()
            interval = 0.05
            ready_at = asyncio.get_running_loop().time() + interval
            state.next_request_time = ready_at
            adapter = NldiAdapter(
                http,
                base_url=BASE,
                request_interval_seconds=interval,
                shared_state=state,
            )
            actual_sleep = asyncio.sleep
            woke_early = False

            async def early_sleep(delay: float, result: object = None) -> object:
                nonlocal woke_early
                if delay > 0 and not woke_early:
                    woke_early = True
                    return result
                return await actual_sleep(delay, result)

            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "sleep", early_sleep)
                assert (
                    await adapter.lookup_position(
                        GeoPoint(latitude=38.75, longitude=-77.25)
                    )
                    == "12345678"
                )
            assert woke_early
            assert state.next_request_time - interval >= ready_at - 0.000001

    run(exercise())
    assert starts == 1


def test_nldi_retry_attempts_obey_request_interval() -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE, request_interval_seconds=0.05)
            assert await adapter.lookup_position(
                GeoPoint(latitude=38.75, longitude=-77.25)
            ) == "12345678"

    run(exercise())
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.04


def test_nldi_queued_request_is_spaced_from_retry_attempt() -> None:
    starts: list[float] = []
    retry_started = asyncio.Event()
    release_retry = asyncio.Event()
    released_at = 0.0

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            await asyncio.sleep(0.25)
            return httpx.Response(503, request=request)
        if len(starts) == 2:
            retry_started.set()
            await release_retry.wait()
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        nonlocal released_at
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE, request_interval_seconds=0.2)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            first = asyncio.create_task(adapter.lookup_position(point))
            await retry_started.wait()
            second = asyncio.create_task(adapter.lookup_position(point))
            await asyncio.sleep(0.25)
            released_at = asyncio.get_running_loop().time()
            release_retry.set()
            assert await asyncio.gather(first, second) == ["12345678"] * 2

    run(exercise())
    assert len(starts) == 3
    assert all(later - earlier >= 0.19 for earlier, later in pairwise(starts))
    assert starts[2] - released_at < 0.12


def test_cancelled_nldi_slot_wait_starts_no_request() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(
                http, base_url=BASE, request_interval_seconds=0.2
            )
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            assert await adapter.lookup_position(point) == "12345678"
            waiting = asyncio.create_task(adapter.lookup_position(point))
            async with asyncio.timeout(0.1):
                while not adapter._request_state.request_lock.locked():
                    await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting

    run(exercise())
    assert starts == 1


def test_cancelled_nldi_request_releases_slot_for_next_request() -> None:
    entered = asyncio.Event()
    starts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE, request_interval_seconds=0.05)
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            pending = asyncio.create_task(adapter.lookup_position(point))
            await entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await adapter.lookup_position(point) == "12345678"

    run(exercise())
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.04


def test_nldi_slot_wait_honors_deadline_without_starting_request() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        return httpx.Response(200, json=fixture("nldi_position.json"), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(
                http, base_url=BASE, request_interval_seconds=0.2
            )
            point = GeoPoint(latitude=38.75, longitude=-77.25)
            assert await adapter.lookup_position(point) == "12345678"
            with pytest.raises(PortTimeoutError):
                await adapter.lookup_position(
                    point,
                    deadline=asyncio.get_running_loop().time() + 0.02,
                )

    run(exercise())
    assert starts == 1


def test_segment_and_navigation_are_normalized_to_direct_edges() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/navigation/UT/flowlines"):
            return httpx.Response(
                200,
                json=fixture("nldi_upstreams.json"),
                request=request,
            )
        return httpx.Response(
            200,
            json=fixture("nldi_position.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE)
            segment = await adapter.get_segment("12345678")
            upstreams = await adapter.get_direct_upstreams("12345678", 12.5)
            assert segment is not None
            assert segment.comid == "12345678"
            assert segment.geometry is not None
            assert segment.geometry_ref == "geometry:12345678"
            assert segment.geometry_summary == segment.geometry.summary()
            assert segment.location == segment.geometry.representative_point
            assert [item.comid for item in upstreams] == ["11111111", "22222222"]
            assert all(item.downstream_comids == ("12345678",) for item in upstreams)
            assert all(
                item.distance_to_downstream_km is not None
                and item.distance_to_downstream_km > 0
                for item in upstreams
            )
            assert "33333333" not in {item.comid for item in upstreams}

    run(exercise())
    navigation = requests[-1]
    assert navigation.url.params["distance"] == "12.5"
    assert navigation.url.params["excludeGeometry"] == "false"


def test_upstream_navigation_uses_short_reach_length() -> None:
    origin, navigation = short_reach_payloads()
    distances: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/navigation/UT/flowlines"):
            distances.append(float(request.url.params["distance"]))
            return httpx.Response(200, json=navigation, request=request)
        return httpx.Response(200, json=origin, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            upstreams = await NldiAdapter(http, base_url=BASE).get_direct_upstreams(
                "12345678", 100
            )
            assert [item.comid for item in upstreams] == ["11111111", "22222222"]

    run(exercise())
    assert len(distances) == 1
    assert 0.25 < distances[0] < 1


def test_upstream_navigation_checks_full_cap_when_short_probe_has_no_neighbor() -> None:
    origin, navigation = short_reach_payloads()
    distances: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/navigation/UT/flowlines"):
            distances.append(float(request.url.params["distance"]))
            payload = origin if len(distances) == 1 else navigation
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(200, json=origin, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            upstreams = await NldiAdapter(http, base_url=BASE).get_direct_upstreams(
                "12345678", 100
            )
            assert [item.comid for item in upstreams] == ["11111111", "22222222"]

    run(exercise())
    assert len(distances) == 2
    assert distances[1] == 100


@pytest.mark.parametrize("max_distance_km", (5, 1000))
def test_upstream_navigation_reaches_cap_for_headwater_and_never_exceeds_it(
    max_distance_km: float,
) -> None:
    origin, _ = short_reach_payloads()
    distances: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/navigation/UT/flowlines"):
            distances.append(float(request.url.params["distance"]))
        return httpx.Response(200, json=origin, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            upstreams = await NldiAdapter(http, base_url=BASE).get_direct_upstreams(
                "12345678", max_distance_km
            )
            assert upstreams == ()

    run(exercise())
    assert len(distances) == 2
    assert distances[-1] == max_distance_km
    assert all(
        first < second <= max_distance_km
        for first, second in pairwise(distances)
    )


def test_upstream_navigation_caps_at_reach_length() -> None:
    origin, _ = short_reach_payloads()
    distances: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/navigation/UT/flowlines"):
            distances.append(float(request.url.params["distance"]))
        return httpx.Response(200, json=origin, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            upstreams = await NldiAdapter(http, base_url=BASE).get_direct_upstreams(
                "12345678", 0.01
            )
            assert upstreams == ()

    run(exercise())
    assert distances == [0.01]


def test_monitoring_sites_use_provider_comid_association_and_deduplicate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=fixture("nldi_sites.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            stations = await NldiAdapter(
                http,
                base_url=BASE,
            ).get_monitoring_sites("12345678")
            assert len(stations) == 1
            assert stations[0].station_id == "USGS-01646500"
            assert stations[0].associated_comid == "12345678"
            assert stations[0].location == GeoPoint(
                latitude=38.93,
                longitude=-77.03,
            )

    run(exercise())


def test_successful_empty_results_remain_empty() -> None:
    empty = {"type": "FeatureCollection", "features": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=empty, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE)
            assert await adapter.lookup_position(
                GeoPoint(latitude=38.75, longitude=-77.25)
            ) is None
            assert await adapter.get_segment("12345678") is None
            assert await adapter.get_direct_upstreams("12345678", 1) == ()
            assert await adapter.get_monitoring_sites("12345678") == ()

    run(exercise())


def test_missing_geometry_and_field_drift_are_invalid_not_empty() -> None:
    origin = fixture("nldi_position.json")
    malformed_navigation = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"nhdplus_comid": 11111111},
                "geometry": None,
            }
        ],
    }
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=origin if calls == 1 else malformed_navigation,
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(
                    http,
                    base_url=BASE,
                ).get_direct_upstreams("12345678", 1)

    run(exercise())


def test_segment_with_mismatched_provider_comid_is_invalid() -> None:
    drifted = fixture("nldi_position.json")
    drifted["features"][0]["properties"]["nhdplus_comid"] = 99999999

    async def exercise() -> None:
        async with provider_client(
            lambda request: httpx.Response(200, json=drifted, request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")

    run(exercise())


@pytest.mark.parametrize(
    "geometry",
    (
        {"type": "LineString", "coordinates": []},
        {
            "type": "LineString",
            "coordinates": [[-77.0, 39.0], [-77.0, 39.0]],
        },
        {
            "type": "LineString",
            "coordinates": [[-181.0, 39.0], [-77.0, 39.0]],
        },
        {
            "type": "MultiLineString",
            "coordinates": [[[-77.0, 39.0], [-76.0, 39.0]]],
        },
        {"type": "Point", "coordinates": [-77.0, 39.0]},
        None,
    ),
)
def test_segment_rejects_empty_repeated_invalid_and_unsupported_geometry(
    geometry,
) -> None:
    payload = fixture("nldi_position.json")
    payload["features"][0]["geometry"] = geometry

    async def exercise() -> None:
        async with provider_client(
            lambda request: httpx.Response(200, json=payload, request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")

    run(exercise())


def test_non_success_http_is_not_rewritten_as_no_match() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            with pytest.raises(PortHttpError) as captured:
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")
            assert captured.value.status_code == 404

    run(exercise())
    assert calls == 1
