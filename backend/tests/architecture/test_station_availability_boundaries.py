import ast
from pathlib import Path

from app.domain import SeriesRef, StationAvailability

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
AVAILABILITY_ROOT = APP_ROOT / "application" / "station_availability"


def test_old_monitoring_station_port_has_no_production_residuals() -> None:
    residuals = []
    for source_path in APP_ROOT.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        for symbol in (
            "Monitoring" + "StationPort",
            "find_" + "for_segment",
        ):
            if symbol in source:
                residuals.append(f"{source_path}:{symbol}")
    assert residuals == []


def test_availability_layer_cannot_construct_evidence_or_inference_results() -> None:
    forbidden_names = {
        "EvidenceFragment",
        "EvidenceBundle",
        "InferenceResult",
        "RiskOrTrendAssessment",
    }
    violations = []
    for source_path in AVAILABILITY_ROOT.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{source_path}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.ImportFrom) and node.module and (
                ".evidence" in node.module or ".inference" in node.module
            ):
                violations.append(f"{source_path}:{node.lineno}:{node.module}")
    assert violations == []

    forbidden_fields = {"value", "confidence", "result", "evidence"}
    assert forbidden_fields.isdisjoint(SeriesRef.model_fields)
    assert forbidden_fields.isdisjoint(StationAvailability.model_fields)
