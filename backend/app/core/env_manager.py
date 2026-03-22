import os
import shutil
import logging
import time
from diskcache import Cache

logger = logging.getLogger(__name__)


class EnvManager:
    """
    Manages workspace directories and DiskCache instances.

    Two reset modes:
      - reset_memory_only()  : clears only ./data/memory (used on normal startup)
                               Safe to call with uvicorn --reload because it does
                               NOT touch .cache/, which uvicorn's file watcher scans.
      - full_reset()         : clears both ./data/memory and ./.cache
                               Use only when you want a completely clean slate
                               (e.g., manual debug runs via debug_main.py).
                               Do NOT call this on normal uvicorn startup.
    """

    # Normal startup: only wipe agent memory
    @staticmethod
    def reset_memory_only():
        """
        Clear ./data/memory and recreate it.
        Does NOT touch ./.cache, so uvicorn --reload won't race against it.
        Called automatically in main.py's lifespan on every server start.
        """
        memory_dir = "./data/memory"
        EnvManager._rmdir_safe(memory_dir)
        os.makedirs(memory_dir, exist_ok=True)
        logger.info(f"Memory directory reset: {memory_dir}")

        # Ensure cache subdirs exist (DiskCache creates them lazily, but we
        # pre-create them so uvicorn's watcher never sees a missing directory)
        EnvManager._ensure_cache_dirs()

    # Full reset: wipe everything (debug / manual use only)
    @staticmethod
    def full_reset():
        """
        Completely wipe ./data/memory AND ./.cache, then reinitialize.
        Use this in debug_main.py or manual scripts, NOT in uvicorn startup.
        """
        EnvManager._close_all_caches()

        for directory in ["./data/memory", "./.cache"]:
            EnvManager._rmdir_safe(directory)
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Directory fully reset: {directory}")

        EnvManager._reinit_all_caches()

    # Legacy alias kept for backward compatibility
    @staticmethod
    def reset_workspace(directories: list = None):
        """
        Backward-compatible wrapper.
        If called with the default (None) args it now calls reset_memory_only()
        so that existing code using reset_workspace() is safe with uvicorn --reload.
        Pass explicit directories=['./data/memory', './.cache'] to get the old
        full-reset behavior (only do this outside uvicorn).
        """
        if directories is None:
            # Safe default: only wipe memory
            EnvManager.reset_memory_only()
        else:
            # Caller explicitly asked for specific dirs – honor it
            EnvManager._close_all_caches()
            for directory in directories:
                EnvManager._rmdir_safe(directory)
                os.makedirs(directory, exist_ok=True)
                logger.info(f"Directory reset: {directory}")
            EnvManager._reinit_all_caches()

    # Internal helpers

    @staticmethod
    def _rmdir_safe(directory: str, retries: int = 3):
        """Remove a directory with retry logic. Silently skips if not found."""
        if not os.path.exists(directory):
            return
        for attempt in range(retries):
            try:
                shutil.rmtree(directory)
                return
            except Exception as e:
                if attempt < retries - 1:
                    logger.warning(
                        f"Failed to remove {directory}, retrying ({attempt + 1}/{retries}): {e}"
                    )
                    time.sleep(0.3)
                else:
                    logger.error(f"Could not remove {directory} after {retries} attempts: {e}")

    @staticmethod
    def _ensure_cache_dirs():
        """
        Pre-create the three cache subdirectories so uvicorn's file watcher
        never encounters a missing path during a concurrent reset.
        """
        for sub in ["nldi_data", "nwis_data", "weather_data"]:
            path = f"./.cache/{sub}"
            os.makedirs(path, exist_ok=True)

    @staticmethod
    def reset_cache_contents():
        """
        Clear all cached entries from the three DiskCache stores WITHOUT deleting
        the directories or closing the Cache objects.

        This is the safe way to flush cache on startup when uvicorn --reload is
        active: the reloader process holds the cache directories open, so
        shutil.rmtree would raise "Device or resource busy". cache.clear() only
        removes key-value entries and leaves the directory structure intact.

        This method is idempotent and safe to call from any process.
        """
        from app.services.nldi_service import nldi_cache
        from app.services.nwis_service import nwis_cache
        from app.services.weather_service import weather_cache

        for name, cache in [
            ("nldi",    nldi_cache),
            ("nwis",    nwis_cache),
            ("weather", weather_cache),
        ]:
            try:
                count = len(cache)
                cache.clear()
                logger.info(f"Cache cleared: {name} ({count} entries removed)")
            except Exception as exc:
                logger.warning(f"Cache clear warning ({name}): {exc}")

    @staticmethod
    def _close_all_caches():
        """Close all DiskCache instances to release file handles before deletion."""
        from app.services.nldi_service import nldi_cache
        from app.services.nwis_service import nwis_cache
        from app.services.weather_service import weather_cache

        for cache in (nldi_cache, nwis_cache, weather_cache):
            try:
                cache.close()
            except Exception as e:
                logger.warning(f"Cache close warning: {e}")

    @staticmethod
    def _reinit_all_caches():
        """Reopen DiskCache instances (replaces module-level global variables)."""
        import app.services.nldi_service as nldi_mod
        import app.services.nwis_service as nwis_mod
        import app.services.weather_service as weather_mod

        nldi_mod.nldi_cache    = Cache("./.cache/nldi_data",    size_limit=500 * 1024 ** 2)
        nwis_mod.nwis_cache    = Cache("./.cache/nwis_data",    size_limit=500 * 1024 ** 2)
        weather_mod.weather_cache = Cache("./.cache/weather_data", size_limit=500 * 1024 ** 2)
        logger.info("All caches reinitialized.")