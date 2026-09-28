from collections.abc import Awaitable, Callable
from typing import Protocol

from app.application.query_understanding.parser_models import (
    QueryParserInputV1,
    QueryParserResponse,
)

ParserDeltaHandler = Callable[[str], Awaitable[None]]


class QueryParserPort(Protocol):
    async def generate(
        self,
        request: QueryParserInputV1,
        on_delta: ParserDeltaHandler,
    ) -> QueryParserResponse: ...
