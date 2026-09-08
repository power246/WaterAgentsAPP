from typing import Protocol, TypeVar

EventT = TypeVar("EventT", contravariant=True)


class EventPublisherPort(Protocol[EventT]):
    async def publish(self, event: EventT) -> None: ...
