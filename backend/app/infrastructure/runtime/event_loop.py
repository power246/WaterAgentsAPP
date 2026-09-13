import asyncio
import sys


def configure_asyncio_event_loop() -> None:
    if sys.platform != "win32":
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        policy_type = getattr(asyncio, "WindowsSelectorEventLoopPolicy")
        asyncio.set_event_loop_policy(policy_type())
        return
    raise RuntimeError("The Windows event loop policy must be configured before startup.")
