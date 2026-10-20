import asyncio

import pytest

from app.application.query_understanding import (
    LocationResolutionStatus,
    LocationResolver,
    ParameterConcept,
    ParsedIntentV1,
    TargetSource,
)
from app.application.transcripts.models import AnalysisTargetV1
from app.domain import (
    AdministrativeLocation,
    GeoPoint,
    GeocodingCandidate,
    RiverSegment,
)
from app.ports import PortUnavailableError


SELECTED_POINT = GeoPoint(latitude=38.9, longitude=-77.0)
SEGMENT_POINT = GeoPoint(latitude=38.91, longitude=-77.01)
OTHER_POINT = GeoPoint(latitude=39.0, longitude=-77.1)
LIVE_POINT = GeoPoint(latitude=46.682323895, longitude=-92.414165705)
LIVE_SEGMENT_POINT = GeoPoint(latitude=46.683, longitude=-92.415)
LIVE_COORDINATE_TEXT = "coordinates: -92.414165705, 46.682323895"


def admin(
    subdivision_code: str = "US-DC",
    *,
    country_code: str = "US",
    display_name: str = "Potomac reach",
) -> AdministrativeLocation:
    return AdministrativeLocation(
        country_code=country_code,
        subdivision_code=subdivision_code,
        display_name=display_name,
        provider_identity=f"provider:{country_code}:{subdivision_code}",
    )


def parsed(
    *,
    location: str | None = None,
    coordinate: GeoPoint | None = None,
) -> ParsedIntentV1:
    return ParsedIntentV1(
        location_mention=location,
        coordinate_mention=coordinate,
        parameters=(
            ParameterConcept(
                concept="water temperature",
                rationale="Water temperature was requested.",
                explicitly_named=True,
            ),
        ),
    )


class FakeGeocoder:
    def __init__(self) -> None:
        self.text_results: dict[str, tuple[GeocodingCandidate, ...]] = {}
        self.reverse_results: dict[GeoPoint, AdministrativeLocation | None] = {}
        self.text_calls: list[str] = []
        self.reverse_calls: list[GeoPoint] = []

    async def resolve_text(
        self,
        location_text: str,
    ) -> tuple[GeocodingCandidate, ...]:
        self.text_calls.append(location_text)
        return self.text_results.get(location_text, ())

    async def reverse(self, point: GeoPoint) -> AdministrativeLocation | None:
        self.reverse_calls.append(point)
        return self.reverse_results.get(point)


class FakeComidLookup:
    def __init__(self) -> None:
        self.results: dict[GeoPoint, str | None] = {}
        self.calls: list[GeoPoint] = []

    async def lookup_position(self, point: GeoPoint) -> str | None:
        self.calls.append(point)
        return self.results.get(point)


class FakeTopology:
    def __init__(self) -> None:
        self.segments: dict[str, RiverSegment | None] = {}
        self.calls: list[str] = []

    async def get_segment(self, comid: str) -> RiverSegment | None:
        self.calls.append(comid)
        return self.segments.get(comid)

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
    ) -> tuple[RiverSegment, ...]:
        return ()


def configured_resolver(
    *,
    point: GeoPoint = SELECTED_POINT,
    segment_point: GeoPoint = SEGMENT_POINT,
    point_admin: AdministrativeLocation | None = None,
    segment_admin: AdministrativeLocation | None = None,
    position_comid: str | None = "12345678",
    segment_comid: str = "12345678",
) -> tuple[LocationResolver, FakeGeocoder, FakeComidLookup, FakeTopology]:
    geocoder = FakeGeocoder()
    geocoder.reverse_results[point] = point_admin or admin()
    geocoder.reverse_results[segment_point] = segment_admin or admin()
    lookup = FakeComidLookup()
    lookup.results[point] = position_comid
    topology = FakeTopology()
    topology.segments[segment_comid] = RiverSegment(
        comid=segment_comid,
        location=segment_point,
    )
    return LocationResolver(geocoder, lookup, topology), geocoder, lookup, topology


def run(coroutine):
    return asyncio.run(coroutine)


def test_frontend_coordinate_only_resolves_and_double_gates_region_and_comid() -> None:
    resolver, geocoder, lookup, topology = configured_resolver()

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.comid == "12345678"
    assert result.target.point == SELECTED_POINT
    assert result.target.source is TargetSource.FRONTEND
    assert geocoder.reverse_calls == [SELECTED_POINT, SEGMENT_POINT]
    assert lookup.calls == [SELECTED_POINT]
    assert topology.calls == ["12345678"]


def test_frontend_comid_only_uses_segment_representative_point() -> None:
    resolver, geocoder, lookup, _ = configured_resolver()

    result = run(
        resolver.resolve(
            AnalysisTargetV1(comid="12345678"),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.point == SEGMENT_POINT
    assert geocoder.reverse_calls == [SEGMENT_POINT]
    assert lookup.calls == []


def test_frontend_comid_and_coordinate_must_identify_same_segment() -> None:
    resolver, _, _, _ = configured_resolver()

    result = run(
        resolver.resolve(
            AnalysisTargetV1(comid="87654321", coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.TARGET_MISMATCH
    assert result.target is None


def test_matching_frontend_comid_and_coordinate_are_accepted() -> None:
    resolver, _, _, _ = configured_resolver()

    result = run(
        resolver.resolve(
            AnalysisTargetV1(comid="12345678", coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.comid == "12345678"


def test_query_coordinate_is_used_when_frontend_target_is_missing() -> None:
    resolver, _, _, _ = configured_resolver()

    result = run(resolver.resolve(None, parsed(coordinate=SELECTED_POINT)))

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.source is TargetSource.QUERY


def test_frontend_coordinate_echo_reuses_resolved_target() -> None:
    resolver, geocoder, lookup, topology = configured_resolver(
        point=LIVE_POINT,
        segment_point=LIVE_SEGMENT_POINT,
        point_admin=admin("US-MN"),
        segment_admin=admin("US-MN"),
    )

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=LIVE_POINT),
            parsed(location=LIVE_COORDINATE_TEXT, coordinate=LIVE_POINT),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.source is TargetSource.FRONTEND
    assert geocoder.text_calls == []
    assert geocoder.reverse_calls == [LIVE_POINT, LIVE_SEGMENT_POINT]
    assert lookup.calls == [LIVE_POINT]
    assert topology.calls == ["12345678"]


def test_query_coordinate_echo_does_not_forward_geocode() -> None:
    resolver, geocoder, lookup, _ = configured_resolver(
        point=LIVE_POINT,
        segment_point=LIVE_SEGMENT_POINT,
        point_admin=admin("US-MN"),
        segment_admin=admin("US-MN"),
    )

    result = run(
        resolver.resolve(
            None,
            parsed(location=LIVE_COORDINATE_TEXT, coordinate=LIVE_POINT),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.source is TargetSource.QUERY
    assert geocoder.text_calls == []
    assert lookup.calls == [LIVE_POINT]


def test_unique_query_text_candidate_resolves_through_position_lookup() -> None:
    resolver, geocoder, _, _ = configured_resolver()
    geocoder.text_results["the Potomac"] = (
        GeocodingCandidate(
            point=SELECTED_POINT,
            administrative_location=admin(),
        ),
    )

    result = run(resolver.resolve(None, parsed(location="the Potomac")))

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.comid == "12345678"
    assert geocoder.text_calls == ["the Potomac"]
    assert geocoder.reverse_calls == [SEGMENT_POINT]


def test_multiple_text_candidates_with_different_comids_are_ambiguous() -> None:
    resolver, geocoder, lookup, topology = configured_resolver()
    geocoder.text_results["Springfield"] = (
        GeocodingCandidate(point=SELECTED_POINT, administrative_location=admin()),
        GeocodingCandidate(point=OTHER_POINT, administrative_location=admin("US-VA")),
    )
    lookup.results[OTHER_POINT] = "87654321"
    other_segment_point = GeoPoint(latitude=39.01, longitude=-77.11)
    topology.segments["87654321"] = RiverSegment(
        comid="87654321",
        location=other_segment_point,
    )
    geocoder.reverse_results[other_segment_point] = admin("US-VA")

    result = run(resolver.resolve(None, parsed(location="Springfield")))

    assert result.status is LocationResolutionStatus.LOCATION_AMBIGUOUS
    assert result.target is None


def test_duplicate_text_candidates_for_same_comid_are_one_business_identity() -> None:
    resolver, geocoder, _, _ = configured_resolver()
    geocoder.text_results["Potomac aliases"] = (
        GeocodingCandidate(point=SELECTED_POINT, administrative_location=admin()),
        GeocodingCandidate(point=SELECTED_POINT, administrative_location=admin()),
    )

    result = run(resolver.resolve(None, parsed(location="Potomac aliases")))

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.comid == "12345678"


def test_frontend_and_query_names_for_same_comid_are_not_a_conflict() -> None:
    resolver, geocoder, _, _ = configured_resolver()
    geocoder.text_results["another name"] = (
        GeocodingCandidate(
            point=SELECTED_POINT,
            administrative_location=admin(display_name="Different display name"),
        ),
    )

    result = run(
        resolver.resolve(
            AnalysisTargetV1(comid="12345678"),
            parsed(location="another name"),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.source is TargetSource.FRONTEND


def test_frontend_and_query_different_comids_fail_with_location_conflict() -> None:
    resolver, geocoder, lookup, topology = configured_resolver()
    geocoder.text_results["different reach"] = (
        GeocodingCandidate(point=OTHER_POINT, administrative_location=admin("US-VA")),
    )
    lookup.results[OTHER_POINT] = "87654321"
    other_segment_point = GeoPoint(latitude=39.01, longitude=-77.11)
    topology.segments["87654321"] = RiverSegment(
        comid="87654321",
        location=other_segment_point,
    )
    geocoder.reverse_results[other_segment_point] = admin("US-VA")

    result = run(
        resolver.resolve(
            AnalysisTargetV1(comid="12345678"),
            parsed(location="different reach"),
        )
    )

    assert result.status is LocationResolutionStatus.LOCATION_CONFLICT


def test_named_location_with_coordinate_keeps_conflict_check() -> None:
    resolver, geocoder, lookup, topology = configured_resolver()
    location_text = "different reach near coordinates: -77.0, 38.9"
    geocoder.text_results[location_text] = (
        GeocodingCandidate(point=OTHER_POINT, administrative_location=admin("US-VA")),
    )
    lookup.results[OTHER_POINT] = "87654321"
    other_segment_point = GeoPoint(latitude=39.01, longitude=-77.11)
    topology.segments["87654321"] = RiverSegment(
        comid="87654321",
        location=other_segment_point,
    )
    geocoder.reverse_results[other_segment_point] = admin("US-VA")

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=SELECTED_POINT),
            parsed(location=location_text, coordinate=SELECTED_POINT),
        )
    )

    assert result.status is LocationResolutionStatus.LOCATION_CONFLICT
    assert geocoder.text_calls == [location_text]


def test_missing_frontend_and_query_locations_requires_location() -> None:
    resolver, _, _, _ = configured_resolver()

    result = run(resolver.resolve(None, parsed()))

    assert result.status is LocationResolutionStatus.LOCATION_REQUIRED


@pytest.mark.parametrize(
    ("country_code", "subdivision_code"),
    [
        ("US", "US-AK"),
        ("US", "US-HI"),
        ("US", "US-PR"),
        ("CA", "CA-ON"),
    ],
)
def test_unsupported_regions_are_distinct_from_missing_comid(
    country_code: str,
    subdivision_code: str,
) -> None:
    outside = admin(subdivision_code, country_code=country_code)
    resolver, geocoder, _, _ = configured_resolver(point_admin=outside)
    geocoder.reverse_results[SELECTED_POINT] = outside

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION


def test_dc_is_in_the_supported_region() -> None:
    resolver, _, _, _ = configured_resolver()

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.SUCCESS
    assert result.target is not None
    assert result.target.subdivision_code == "US-DC"


def test_conus_point_without_comid_is_location_not_supported() -> None:
    resolver, _, _, _ = configured_resolver(position_comid=None)

    result = run(
        resolver.resolve(
            AnalysisTargetV1(coordinates=SELECTED_POINT),
            parsed(),
        )
    )

    assert result.status is LocationResolutionStatus.LOCATION_NOT_SUPPORTED


def test_port_unavailability_is_not_rewritten_as_no_match() -> None:
    class UnavailableLookup(FakeComidLookup):
        async def lookup_position(self, point: GeoPoint) -> str | None:
            raise PortUnavailableError("NLDI unavailable")

    _, geocoder, _, topology = configured_resolver()
    resolver = LocationResolver(geocoder, UnavailableLookup(), topology)

    with pytest.raises(PortUnavailableError, match="NLDI unavailable"):
        run(
            resolver.resolve(
                AnalysisTargetV1(coordinates=SELECTED_POINT),
                parsed(),
            )
        )
