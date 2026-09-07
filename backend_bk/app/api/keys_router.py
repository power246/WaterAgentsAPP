# REST endpoints for user-managed API key storage.
#
# Routes:
#   GET    /api/keys              → status of all known keys (masked, never raw)
#   POST   /api/keys/{name}       → save or overwrite a key
#   DELETE /api/keys/{name}       → clear a key
#   GET    /api/keys/map-key      → raw Google Maps key (needed by frontend map init)
#   GET    /api/settings/ollama-models  → proxies to local Ollama /api/tags

import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.core.user_keys_store import (
    get_all_status,
    get_key,
    set_key,
    delete_key,
    KNOWN_KEY_NAMES,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request/response schemas ──────────────────────────────────────────────────

class SaveKeyRequest(BaseModel):
    value: str


# ── Key management endpoints ──────────────────────────────────────────────────

@router.get("")
async def list_keys():
    """
    Return the status of every known key (set / not set, masked preview).
    Raw key values are NEVER returned — the frontend uses this only to show
    whether a key is configured and to display a masked preview.
    """
    return await get_all_status()


@router.post("/{name}")
async def save_key(name: str, body: SaveKeyRequest):
    """Save or overwrite a user-supplied API key."""
    if name not in KNOWN_KEY_NAMES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown key name '{name}'. "
                                   f"Allowed: {KNOWN_KEY_NAMES}")
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Key value must not be empty.")

    await set_key(name, value)
    return {"ok": True, "name": name, "message": "Key saved successfully."}


@router.delete("/{name}")
async def clear_key(name: str):
    """Remove a user-saved key so the system falls back to .env."""
    if name not in KNOWN_KEY_NAMES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown key name '{name}'.")
    await delete_key(name)
    return {"ok": True, "name": name, "message": "Key cleared."}


@router.get("/map-key")
async def get_map_key():
    """
    Return the Google Maps API key for frontend map initialisation.
    Priority: user_keys_store → .env GOOGLE_MAPS_API_KEY.

    This endpoint exists because the frontend map component needs the raw key
    to load the Google Maps JS SDK.  All other key values stay server-side.
    """
    key = await get_key("GOOGLE_MAPS_API_KEY")
    if not key:
        # Fall back to environment variable
        key = getattr(settings, "GOOGLE_MAPS_API_KEY", None)
    if not key:
        # No Google Maps key available — frontend will use OpenStreetMap fallback
        return {"key": None}
    return {"key": key}


# ── Ollama dynamic model list ─────────────────────────────────────────────────

@router.get("/ollama-models")
async def get_ollama_models():
    """
    Proxy request to the local Ollama instance to retrieve installed models.
    Returns a list of {id, label} objects compatible with the settings catalogue.
    Falls back to an empty list if Ollama is unreachable.
    """
    ollama_url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(ollama_url)
            resp.raise_for_status()
            data = resp.json()

        models = []
        for m in data.get("models", []):
            model_id = m.get("name", "")
            if model_id:
                # Convert e.g. "llama3:8b" -> "Llama3 8B" for the label
                label = model_id.replace(":", " ").replace("-", " ").title()
                models.append({"id": model_id, "label": label})

        logger.info(f"[OllamaModels] Found {len(models)} local model(s)")
        return {"models": models, "error": None}

    except Exception as exc:
        logger.warning(f"[OllamaModels] Could not reach Ollama at {ollama_url}: {exc}")
        return {
            "models": [],
            "error": f"Ollama not reachable at {settings.OLLAMA_BASE_URL}. "
                     "Is it running?  Falling back to default model list.",
        }