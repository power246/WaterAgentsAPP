from app.application.analysis_runs.models import RunLifecycleEvent

KEEPALIVE_FRAME = b": keepalive\n\n"


def encode_event(event: RunLifecycleEvent) -> bytes:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    ).encode("utf-8")
