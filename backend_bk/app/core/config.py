from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Dict, Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Water Agent System"
    API_V1_STR:   str = "/api/v1"

    # ── Ollama (local) ────────────────────────────────────────────────────────
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    OPENAI_API_KEY: Optional[str] = None

    # ── Google Gemini ─────────────────────────────────────────────────────────
    GOOGLE_API_KEY: Optional[str] = None       # Gemini / Vertex AI key

    # ── Grok (xAI) ────────────────────────────────────────────────────────────
    GROK_API_KEY:   Optional[str] = None
    GROK_BASE_URL:  str = "https://api.x.ai/v1"

    # ── Google Maps ───────────────────────────────────────────────────────────
    # Used by the Geocoding API (asker_service) and the frontend map component.
    # Separate from GOOGLE_API_KEY (Gemini) — Maps and AI are different products.
    GOOGLE_MAPS_API_KEY: Optional[str] = None

    # ── Default LLM model ─────────────────────────────────────────────────────
    DEFAULT_LLM_MODEL: str = "llama3:8b"

    # ── EPA facility types shown in the settings panel ────────────────────────
    FACILITY_TYPES: List[str] = ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"]
    FACILITY_TYPE_DESCRIPTION: Dict[str, str] = {
        "ICIS": "Contains NPDES permit data, discharge monitoring reports (DMRs), "
                "and enforcement history under the Clean Water Act.",
        "SEMS": "Tracks the nation's most severe hazardous waste cleanup sites "
                "(Superfund sites).",
        "TRIS": "Facilities must report annual releases or transfers of toxic "
                "chemicals to the environment.",
        "NCDB": "Tracks facilities' compliance status under various environmental laws.",
        "CWNS": "Assesses capital needs for publicly owned wastewater treatment "
                "facilities (POTWs).",
        "FFEP": "Tracks environmental compliance for federal agencies "
                "(like military bases).",
    }

    # ── NLDI / topology ───────────────────────────────────────────────────────
    USGS_SEARCH_DISTANCE: float = 0.1   # km radius when looking for nearby USGS sites
    MAX_TOPOLOGY_DEPTH:   int   = 10    # BFS depth limit when building the river graph

    # ── Anomaly detection & prediction limits ─────────────────────────────────
    # A sub-daily window deviating more than this percentage from the daily mean
    # is flagged as an anomaly and passed to the LLM for explanation.
    ANOMALY_THRESHOLD_PCT: float = 25.0
    PREDICTION_MAX_DAYS: int = 7         # Maximum number of future days allowed in prediction mode.

    # ── Data resampling frequencies (used as fallback when auto-freq is off) ──
    WATER_DATA_RESAMPLE_FREQ: str = "6h"
    WEATHER_FREQ:             str = "12h"

    # ── HTTP client ───────────────────────────────────────────────────────────
    HTTP_HEADERS:    Dict[str, str] = {"User-Agent": "WaterAgentSystem/1.0"}
    REQUEST_TIMEOUT: float          = 60.0

    # Load from .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()