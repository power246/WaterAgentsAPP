from app.domain.domain_model import DomainModel

KEEPALIVE_FRAME = b": keepalive\n\n"


def encode_event(
    event_name: str,
    data: DomainModel,
    *,
    event_id: int | None = None,
) -> bytes:
    identifier = f"id: {event_id}\n" if event_id is not None else ""
    return (
        f"{identifier}event: {event_name}\n"
        f"data: {data.model_dump_json()}\n\n"
    ).encode("utf-8")
