from collections.abc import AsyncIterator
from typing import Protocol

from app.application.reasoning.model_models import (
    WaterbodyModelEvent,
    WaterbodyModelRequest,
)


class WaterbodyModelPort(Protocol):
    def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]: ...
