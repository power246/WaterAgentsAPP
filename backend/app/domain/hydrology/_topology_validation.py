from app.domain.hydrology.coverage_unit import CoverageUnit


def validate_comid(value: str) -> str:
    if not value.isdecimal() or value.startswith("0"):
        raise ValueError("comid must be a positive decimal string")
    return value


def comid_key(comid: str) -> tuple[int, str]:
    return int(comid), comid


def coverage_key(unit: CoverageUnit) -> tuple[int, int, str, str]:
    return (
        unit.segment_index,
        -1 if unit.window_index is None else unit.window_index,
        unit.parameter.code_system,
        unit.parameter.code,
    )
