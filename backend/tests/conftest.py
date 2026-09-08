import logging

import pytest

from app.infrastructure.observability.json_logging import JsonFormatter


@pytest.fixture
def application_logs(caplog):
    lines = []

    class CaptureJson(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    logger = logging.getLogger("wateragents")
    previous = logger.handlers[:], logger.level, logger.propagate
    handler = CaptureJson()
    handler.setFormatter(JsonFormatter())
    previous_formatter = caplog.handler.formatter
    caplog.handler.setFormatter(JsonFormatter())
    logger.handlers = [handler, caplog.handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield lines
    finally:
        logger.handlers, logger.level, logger.propagate = previous
        caplog.handler.setFormatter(previous_formatter)
