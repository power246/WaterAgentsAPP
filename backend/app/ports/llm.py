from typing import Protocol, TypeVar

RequestT = TypeVar("RequestT", contravariant=True)
ResponseT = TypeVar("ResponseT", covariant=True)


class LLMPort(Protocol[RequestT, ResponseT]):
    async def generate(self, request: RequestT) -> ResponseT: ...
