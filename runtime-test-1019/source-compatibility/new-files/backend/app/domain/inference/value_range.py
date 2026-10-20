from decimal import Decimal

from app.domain.inference.outcome_status import OutcomeStatus


def validate_result_range(
    status: OutcomeStatus,
    representative_value: str | None,
    value_lower: str | None,
    value_upper: str | None,
) -> None:
    if (value_lower is None) != (value_upper is None):
        raise ValueError("value_lower and value_upper must be provided together")
    if value_lower is None:
        if status is OutcomeStatus.RANGE_ESTIMATE:
            raise ValueError("range estimate requires value_lower and value_upper")
        return
    if status not in (OutcomeStatus.QUANTIFIED, OutcomeStatus.RANGE_ESTIMATE):
        raise ValueError("only numeric outcomes can include a value range")
    lower, upper = Decimal(value_lower), Decimal(value_upper)
    if lower >= upper:
        raise ValueError("value_lower must be less than value_upper")
    if representative_value is not None and not lower <= Decimal(representative_value) <= upper:
        raise ValueError("representative_value must fall within the value range")


def validate_inference_metadata(
    status: OutcomeStatus,
    method: str | None,
    basis: str | None,
    has_measured_anchors: bool,
    value_lower: str | None,
) -> None:
    if basis == "same_parameter_measurement" and not has_measured_anchors:
        raise ValueError("same-parameter basis requires original measured anchors")
    if basis in ("related_evidence", "general_knowledge") and has_measured_anchors:
        raise ValueError("non-measured basis cannot claim measured anchors")
    if method == "model_estimate":
        if status not in (OutcomeStatus.QUANTIFIED, OutcomeStatus.RANGE_ESTIMATE):
            raise ValueError("model estimate metadata requires a numeric outcome")
        if value_lower is None or basis is None:
            raise ValueError("model estimate requires a possible range and inference basis")
        if status is OutcomeStatus.QUANTIFIED and not has_measured_anchors:
            raise ValueError("model point estimate requires original measured anchors")
