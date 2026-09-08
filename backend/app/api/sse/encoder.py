from app.application.analysis_sessions.models import LifecycleEvent

KEEPALIVE_FRAME = b": keepalive\n\n"


def encode_event(event: LifecycleEvent) -> bytes:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    ).encode("utf-8")
