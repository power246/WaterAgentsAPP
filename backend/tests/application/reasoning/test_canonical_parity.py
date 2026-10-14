from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.application.analysis_runs.models import RunStatus
from app.application.reasoning.result_models import (
    _canonical_model_bytes as result_model_bytes,
    _digest_values as result_digest,
)
from app.application.reasoning.sufficiency import (
    _canonical_model_bytes as sufficiency_model_bytes,
    _digest_values as sufficiency_digest,
)
from app.domain import ParameterKey


def test_reasoning_canonical_bytes_and_digests_keep_the_existing_wire_values() -> None:
    parameter = ParameterKey(code_system="usgs", code="00010")
    expected_model = b'{"code":"00010","code_system":"usgs"}'
    assert sufficiency_model_bytes(parameter) == expected_model
    assert result_model_bytes(parameter) == expected_model

    values = {
        "items": ("sensor-1", 2),
        "run_id": UUID(int=1),
        "status": RunStatus.COMPLETED,
        "timestamp": datetime(2026, 10, 8, 8, tzinfo=timezone(timedelta(hours=8))),
    }
    expected_digest = (
        "sha256:07a8b6cbcea388c7715abe584eb31ddfd7e133084d7ac447ae507748906b46f6"
    )
    assert sufficiency_digest(values) == expected_digest
    assert result_digest(values) == expected_digest
