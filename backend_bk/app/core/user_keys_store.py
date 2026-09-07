# Persists user-entered API keys to ./data/user_keys.json.
# This file survives server restarts and is only cleared by explicit DELETE calls.
#
# Priority for LLM calls:
#   1. Key passed explicitly from frontend (per-request override)
#   2. Key stored in this file (user saved in Settings panel)
#   3. Key from .env via settings (server default)

import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_STORE_PATH = Path("./data/user_keys.json")

# All key names the system recognises.  Extend this list when adding providers.
KNOWN_KEY_NAMES = [
    "GOOGLE_MAPS_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",   # Gemini
    "GROK_API_KEY",
]

# File-level async lock: prevents concurrent read-modify-write races
# when multiple agents finish simultaneously.
_lock = asyncio.Lock()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read() -> Dict[str, str]:
    """Read the store from disk.  Returns {} on missing or corrupt file."""
    if not _STORE_PATH.exists():
        return {}
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[KeyStore] Could not read {_STORE_PATH}: {exc}")
        return {}


def _write(data: Dict[str, str]) -> None:
    """Write the store to disk, creating parent directories as needed."""
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")


def _mask(value: str) -> str:
    """Return a masked display string: all but the last 4 chars replaced with bullets."""
    if len(value) <= 4:
        return "••••"
    return "•" * (len(value) - 4) + value[-4:]


# ── Public async API ──────────────────────────────────────────────────────────

async def get_key(name: str) -> Optional[str]:
    """Return the stored value for *name*, or None if not set."""
    async with _lock:
        return _read().get(name)


async def set_key(name: str, value: str) -> None:
    """Save or overwrite a key.  Strips surrounding whitespace."""
    async with _lock:
        data = _read()
        data[name] = value.strip()
        _write(data)
        logger.info(f"[KeyStore] Saved key: {name}")


async def delete_key(name: str) -> None:
    """Remove a key from the store.  No-op if not present."""
    async with _lock:
        data = _read()
        if name in data:
            del data[name]
            _write(data)
            logger.info(f"[KeyStore] Deleted key: {name}")


async def get_all_status() -> Dict[str, dict]:
    """
    Return a status dict for all known keys:
      {
        "OPENAI_API_KEY": {"set": true,  "masked": "••••••••abcd"},
        "GROK_API_KEY":   {"set": false, "masked": null},
        ...
      }
    The actual key values are never sent to the frontend.
    """
    async with _lock:
        data = _read()
    result = {}
    for name in KNOWN_KEY_NAMES:
        val = data.get(name)
        result[name] = {
            "set":    val is not None,
            "masked": _mask(val) if val else None,
        }
    return result


async def resolve_llm_key(provider: str, frontend_key: Optional[str]) -> Optional[str]:
    """
    Resolve the API key for a given provider using priority order:
      1. frontend_key  (passed explicitly from this request)
      2. user_keys_store  (saved by user in Settings panel)
      3. Returns None  (caller falls back to .env via settings)

    provider: "openai" | "gemini" | "grok"  (ollama needs no key)
    """
    if frontend_key:
        return frontend_key

    key_map = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "grok":   "GROK_API_KEY",
    }
    store_name = key_map.get(provider)
    if store_name:
        stored = await get_key(store_name)
        if stored:
            logger.debug(f"[KeyStore] Using stored key for provider '{provider}'")
            return stored

    return None  # fall back to .env