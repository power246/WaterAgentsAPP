import asyncio
import selectors
import sys

WINDOWS_SELECTOR_LOOP_FACTORY = (
    "app.infrastructure.runtime.event_loop:windows_selector_loop_factory"
)


def windows_selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def uvicorn_loop() -> str:
    if sys.platform == "win32":
        return WINDOWS_SELECTOR_LOOP_FACTORY
    return "auto"


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
