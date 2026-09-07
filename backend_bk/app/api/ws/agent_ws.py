import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.agents.graph import run_hydro_graph

logger = logging.getLogger(__name__)
router = APIRouter()

# How often (seconds) to send a keepalive ping while the backend is busy.
# Must be shorter than any proxy / browser WebSocket idle timeout (typically 60s).
HEARTBEAT_INTERVAL = 20


@router.websocket("/hydro-analysis")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time streaming inference.
    Full path: ws://host/ws/hydro-analysis  (prefix added by include_router in main.py)

    Protocol:
      Client -> Server (once):  {"query": "...", "model_name": "..."}
      Server -> Client (stream): sequence of event dicts until type == "done" or "error"

    A background heartbeat task sends {"type": "ping"} every HEARTBEAT_INTERVAL seconds
    so the connection is not dropped by proxies or browsers during long LLM inference.
    """
    await websocket.accept()
    logger.info("[WS] Client connected")

    # Shared flag: set when the main task finishes so the heartbeat loop exits.
    stop_event = asyncio.Event()

    async def heartbeat():
        """Send a lightweight ping every HEARTBEAT_INTERVAL seconds."""
        try:
            while not stop_event.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if stop_event.is_set():
                    break
                await websocket.send_json({"type": "ping"})
                logger.debug("[WS] Heartbeat ping sent")
        except Exception:
            pass  # Connection already closed, silently exit

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        # Receive query parameters sent by the frontend immediately after connecting
        params = await websocket.receive_json()
        logger.info(f"[WS] Query received: {params.get('query', '')[:80]!r}")

        # Concurrent listener: watches for a {"type": "stop"} message from the client.
        # Uses the same stop_event that the heartbeat already references so that
        # the heartbeat also exits when the user stops inference.
        async def listen_for_stop():
            """Read incoming messages and set stop_event on stop signal."""
            try:
                while not stop_event.is_set():
                    msg = await websocket.receive_json()
                    if isinstance(msg, dict) and msg.get("type") == "stop":
                        logger.info("[WS] Stop signal received from client")
                        stop_event.set()
                        break
            except Exception:
                pass  # Connection closed or parse error — silently exit

        stop_listener_task = asyncio.create_task(listen_for_stop())

        # Wrap the entire inference pipeline in an asyncio.Task so it can be
        # cancelled immediately when the user presses Stop, aborting all nested
        # asyncio.gather HTTP calls instead of waiting for the next agent boundary.
        async def _inference_loop():
            async for event in run_hydro_graph(params, stop_event=stop_event):
                await websocket.send_json(event)
                if event.get("type") in ("done", "error", "interrupted"):
                    stop_event.set()
                    break

        async def _stop_watcher(task: asyncio.Task):
            """Await stop_event then cancel the inference task."""
            await stop_event.wait()
            if not task.done():
                task.cancel()
                logger.info("[WS] Inference task cancelled by stop request")

        inference_task    = asyncio.create_task(_inference_loop())
        stop_watcher_task = asyncio.create_task(_stop_watcher(inference_task))

        try:
            await inference_task
        except asyncio.CancelledError:
            logger.info("[WS] Inference task cancelled — backend cleaning up")
        except Exception as exc:
            logger.error(f"[WS] Inference loop raised: {exc}")
        finally:
            # Clean up watcher and listener tasks
            stop_watcher_task.cancel()
            stop_listener_task.cancel()
            await asyncio.gather(
                stop_watcher_task, stop_listener_task, return_exceptions=True
            )
            # Notify the frontend that all backend work has fully stopped.
            # The frontend holds the Start button disabled until it receives this.
            try:
                await websocket.send_json({"type": "backend_idle"})
                logger.info("[WS] backend_idle sent to client")
            except Exception:
                logger.debug("[WS] Could not send backend_idle (client already disconnected)")

    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected early")
    except Exception as e:
        logger.error(f"[WS] Unhandled error: {e}", exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("[WS] Connection closed")