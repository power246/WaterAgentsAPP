from app.domain import ParameterKey


_PRECIPITATION = ParameterKey(code_system="usgs", code="00045")
_WEATHER_VARIABLES: dict[ParameterKey, dict[str, str]] = {
    _PRECIPITATION: {
        "observation": "precipitationLastHour",
        "forecast": "quantitativePrecipitation",
    },
}


def weather_variables_for_parameters(
    parameters: tuple[ParameterKey, ...],
    evidence_kind: str,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            mapping[evidence_kind]
            for parameter in parameters
            if (mapping := _WEATHER_VARIABLES.get(parameter)) is not None
            and evidence_kind in mapping
        )
    )


def weather_parameter_for_variable(
    variable: str,
    evidence_kind: str,
) -> ParameterKey | None:
    matches = tuple(
        parameter
        for parameter, mapping in _WEATHER_VARIABLES.items()
        if mapping.get(evidence_kind) == variable
    )
    return matches[0] if len(matches) == 1 else None


def weather_capable_parameters(
    parameters: tuple[ParameterKey, ...],
) -> tuple[ParameterKey, ...]:
    return tuple(parameter for parameter in parameters if parameter in _WEATHER_VARIABLES)
