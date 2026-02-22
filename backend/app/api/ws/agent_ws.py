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

        # Stream inference events from the LangGraph pipeline
        async for event in run_hydro_graph(params):
            await websocket.send_json(event)
            # Tell heartbeat to stop once the terminal event is sent
            if event.get("type") in ("done", "error"):
                stop_event.set()

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