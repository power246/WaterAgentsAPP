import inspect
from collections.abc import AsyncIterator
from typing import get_type_hints

from app.application.reasoning.model_models import (
    WaterbodyModelEvent,
    WaterbodyModelRequest,
)
from app.ports import (
    InvalidPortResponseError,
    PortError,
    PortTimeoutError,
    PortUnavailableError,
    WaterbodyModelCancelledError,
    WaterbodyModelContentFilteredError,
    WaterbodyModelIncompleteError,
    WaterbodyModelPortError,
    WaterbodyModelProtocolError,
    WaterbodyModelRefusalError,
    WaterbodyModelTimeoutError,
    WaterbodyModelTruncatedError,
    WaterbodyModelUnavailableError,
)
from app.ports.waterbody_model import WaterbodyModelPort


def test_waterbody_model_port_is_streaming_protocol() -> None:
    signature = inspect.signature(WaterbodyModelPort.stream)
    hints = get_type_hints(WaterbodyModelPort.stream, include_extras=True)

    assert getattr(WaterbodyModelPort, "_is_protocol", False)
    assert tuple(signature.parameters) == ("self", "request")
    assert hints["request"] is WaterbodyModelRequest
    assert hints["return"] == AsyncIterator[WaterbodyModelEvent]
    assert not inspect.iscoroutinefunction(WaterbodyModelPort.stream)


def test_waterbody_model_errors_are_typed_without_dynamic_provider_messages() -> None:
    error_types = (
        WaterbodyModelUnavailableError,
        WaterbodyModelTimeoutError,
        WaterbodyModelContentFilteredError,
        WaterbodyModelRefusalError,
        WaterbodyModelProtocolError,
        WaterbodyModelTruncatedError,
        WaterbodyModelIncompleteError,
        WaterbodyModelCancelledError,
    )

    errors = tuple(
        error_type(
            provider="openai",
            model="private-model-identity",
            operation_id="operation-1",
        )
        for error_type in error_types
    )

    assert all(isinstance(error, WaterbodyModelPortError) for error in errors)
    assert all(isinstance(error, PortError) for error in errors)
    assert isinstance(errors[0], PortUnavailableError)
    assert isinstance(errors[1], PortTimeoutError)
    assert isinstance(errors[4], InvalidPortResponseError)
    assert tuple(error.code for error in errors) == (
        "waterbody_model_unavailable",
        "waterbody_model_timeout",
        "waterbody_model_content_filtered",
        "waterbody_model_refused",
        "waterbody_model_response_invalid",
        "waterbody_model_truncated",
        "waterbody_model_incomplete",
        "waterbody_model_cancelled",
    )
    assert "openai" not in " ".join(str(error) for error in errors)
    assert "private-model-identity" not in " ".join(str(error) for error in errors)
