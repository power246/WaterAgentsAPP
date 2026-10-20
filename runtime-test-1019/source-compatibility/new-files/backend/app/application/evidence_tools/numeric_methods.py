from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import partial
from hashlib import sha256
from math import isfinite
from typing import Literal

from app.domain import (
    EvidenceAggregationMethod,
    ParameterCapabilityRegistry,
    ParameterKey,
    ParameterProvider,
    TimeRange,
)

InputWindowRelation = Literal[
    "same_window", "nearest_before", "nearest_after", "nearest_anchor"
]


@dataclass(frozen=True, slots=True)
class NumericMethodInput:
    parameter: ParameterKey
    unit: str
    window_relation: InputWindowRelation = "same_window"
    waterbody_relation: Literal["same_waterbody"] = "same_waterbody"
    allow_estimated: bool = True
    required_payload_type: str | None = None


NumericCalculator = Callable[
    [tuple[Decimal, ...], tuple[TimeRange, ...], TimeRange], Decimal
]


@dataclass(frozen=True, slots=True)
class NumericMethod:
    method_id: str
    version: int
    output_parameter: ParameterKey
    output_unit: str
    inputs: tuple[NumericMethodInput, ...]
    calculate: NumericCalculator | None
    confidence_cap: float
    selection_priority: int = 0
    allowed_modes: tuple[str, ...] = ("gap_filling",)
    production: bool = False
    kind: Literal["explicit_calculation", "model_estimate"] = "explicit_calculation"

    def __post_init__(self) -> None:
        if not self.method_id.strip() or len(self.method_id) > 64 or self.version < 1:
            raise ValueError("numeric method identity is invalid")
        if (
            not self.output_unit.strip()
            or len(self.output_unit) > 32
            or (not self.inputs and self.kind != "model_estimate")
        ):
            raise ValueError("numeric method requires a unit and inputs")
        if (self.calculate is None) != (self.kind == "model_estimate"):
            raise ValueError("numeric method execution must match its kind")
        if not 0 <= self.confidence_cap <= 1:
            raise ValueError("numeric method confidence cap is invalid")
        if not isfinite(self.confidence_cap) or self.selection_priority < 0:
            raise ValueError("numeric method limits are invalid")
        if len(self.inputs) > 8:
            raise ValueError("numeric method has too many inputs")
        if not self.allowed_modes:
            raise ValueError("numeric method requires an allowed mode")


class NumericMethodRegistry:
    def __init__(self, methods: tuple[NumericMethod, ...]) -> None:
        identities = tuple((method.method_id, method.version) for method in methods)
        if len(identities) != len(set(identities)):
            raise ValueError("numeric method identities must be unique")
        self._methods = tuple(
            sorted(
                methods,
                key=lambda item: (
                    item.output_parameter.code_system,
                    item.output_parameter.code,
                    item.selection_priority,
                    item.method_id,
                    item.version,
                ),
            )
        )
        self._by_identity = {
            (item.method_id, item.version): item for item in self._methods
        }
        descriptors = tuple(
            {
                "method_id": item.method_id,
                "kind": item.kind,
                "version": item.version,
                "output_parameter": item.output_parameter.model_dump(mode="json"),
                "output_unit": item.output_unit,
                "inputs": tuple(
                    {
                        "parameter": input_.parameter.model_dump(mode="json"),
                        "unit": input_.unit,
                        "window_relation": input_.window_relation,
                        "waterbody_relation": input_.waterbody_relation,
                        "allow_estimated": input_.allow_estimated,
                        "required_payload_type": input_.required_payload_type,
                    }
                    for input_ in item.inputs
                ),
                "confidence_cap": item.confidence_cap,
                "selection_priority": item.selection_priority,
                "allowed_modes": item.allowed_modes,
                "production": item.production,
            }
            for item in self._methods
        )
        encoded = json.dumps(
            descriptors, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        self.manifest_digest = sha256(encoded).hexdigest()

    def get(self, method_id: str, version: int) -> NumericMethod | None:
        return self._by_identity.get((method_id, version))

    def method(self, method_id: str, version: int) -> NumericMethod | None:
        return self.get(method_id, version)

    def for_output(self, parameter: ParameterKey) -> tuple[NumericMethod, ...]:
        return tuple(
            method for method in self._methods if method.output_parameter == parameter
        )

    @property
    def methods(self) -> tuple[NumericMethod, ...]:
        return self._methods


def _seconds(start: TimeRange, end: TimeRange) -> Decimal:
    first = start.start + (start.end - start.start) / 2
    second = end.start + (end.end - end.start) / 2
    delta = second - first
    return Decimal(delta.days * 86400 + delta.seconds) + (
        Decimal(delta.microseconds) / Decimal(1_000_000)
    )


def linear_interpolation(
    values: tuple[Decimal, ...],
    windows: tuple[TimeRange, ...],
    target: TimeRange,
) -> Decimal:
    if len(values) != 2 or len(windows) != 2:
        raise ValueError("linear interpolation requires two anchors")
    span = _seconds(windows[0], windows[1])
    if span <= 0:
        raise ValueError("linear interpolation anchors must be ordered")
    with localcontext() as context:
        context.prec = 34
        return values[0] + (values[1] - values[0]) * (
            _seconds(windows[0], target) / span
        )


def nearest_hold(
    values: tuple[Decimal, ...],
    windows: tuple[TimeRange, ...],
    target: TimeRange,
) -> Decimal:
    if len(values) != 1 or len(windows) != 1:
        raise ValueError("nearest hold requires one anchor")
    return values[0]


def create_production_numeric_method_registry(
    capabilities: ParameterCapabilityRegistry,
) -> NumericMethodRegistry:
    methods = []
    for capability in capabilities.capabilities:
        unit = capabilities.unit_definitions[capability.unit_policy_id].symbol
        methods.append(NumericMethod(
            method_id=f"model_estimate_v1_{capability.key.code_system}_{capability.key.code}",
            version=1,
            output_parameter=capability.key,
            output_unit=unit,
            inputs=(),
            calculate=None,
            confidence_cap=1.0,
            selection_priority=100,
            allowed_modes=("gap_filling", "prediction"),
            production=True,
            kind="model_estimate",
        ))
        if not any(
            item.provider
            in {
                ParameterProvider.WDFN_TIME_SERIES,
                ParameterProvider.USGS_SAMPLES,
            }
            and item.method
            in {
                EvidenceAggregationMethod.MEAN,
                EvidenceAggregationMethod.LATEST,
            }
            for item in capability.default_aggregation_by_resolution
        ):
            continue
        unit = capabilities.unit_definitions[capability.unit_policy_id].symbol
        shared = {
            "version": 1,
            "output_parameter": capability.key,
            "output_unit": unit,
            "confidence_cap": 0.6,
            "production": True,
        }
        anchor = partial(
            NumericMethodInput,
            parameter=capability.key,
            unit=unit,
            allow_estimated=False,
            required_payload_type="aggregate",
        )
        methods.extend(
            (
                NumericMethod(
                    method_id=(
                        "temporal_gap_fill_linear_v1_"
                        f"{capability.key.code_system}_{capability.key.code}"
                    ),
                    inputs=(
                        anchor(window_relation="nearest_before"),
                        anchor(window_relation="nearest_after"),
                    ),
                    calculate=linear_interpolation,
                    selection_priority=0,
                    **shared,
                ),
                NumericMethod(
                    method_id=(
                        "temporal_gap_fill_hold_v1_"
                        f"{capability.key.code_system}_{capability.key.code}"
                    ),
                    inputs=(anchor(window_relation="nearest_anchor"),),
                    calculate=nearest_hold,
                    selection_priority=1,
                    **shared,
                ),
            )
        )
    return NumericMethodRegistry(tuple(methods))
