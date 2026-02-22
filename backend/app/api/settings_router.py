from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()

# ---------------------------------------------------------------------------
# Provider catalogue: defines every LLM provider the system supports.
# "needs_api_key": whether the frontend must collect an API key from the user.
# "models": ordered list of recommended model IDs for that provider.
# ---------------------------------------------------------------------------
PROVIDER_CATALOGUE = [
    {
        "id":           "ollama",
        "label":        "Ollama (local)",
        "needs_api_key": False,
        "models": [
            {"id": "llama3:8b",   "label": "Llama 3 8B"},
            {"id": "llama3:70b",  "label": "Llama 3 70B"},
            {"id": "mistral:7b",  "label": "Mistral 7B"},
            {"id": "qwen2:7b",    "label": "Qwen 2 7B"},
        ],
    },
    {
        "id":           "openai",
        "label":        "OpenAI",
        "needs_api_key": True,
        "api_key_hint":  "sk-...",
        "models": [
            {"id": "gpt-4o",           "label": "GPT-4o"},
            {"id": "gpt-4-turbo",      "label": "GPT-4 Turbo"},
            {"id": "gpt-4o-mini",      "label": "GPT-4o Mini"},
            {"id": "gpt-3.5-turbo",    "label": "GPT-3.5 Turbo"},
        ],
    },
    {
        "id":           "gemini",
        "label":        "Google Gemini",
        "needs_api_key": True,
        "api_key_hint":  "AIza...",
        "models": [
            {"id": "gemini-1.5-pro",   "label": "Gemini 1.5 Pro"},
            {"id": "gemini-1.5-flash", "label": "Gemini 1.5 Flash"},
            {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
        ],
    },
    {
        "id":           "grok",
        "label":        "Grok (xAI)",
        "needs_api_key": True,
        "api_key_hint":  "xai-...",
        "models": [
            {"id": "grok-beta",        "label": "Grok Beta"},
            {"id": "grok-2",           "label": "Grok 2"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Facility type catalogue – full descriptions shown in the UI.
# ---------------------------------------------------------------------------
FACILITY_CATALOGUE = [
    {
        "id":          "ICIS",
        "label":       "ICIS",
        "description": "Integrated Compliance Information System — NPDES permit data, "
                       "discharge monitoring reports (DMRs), and enforcement history "
                       "under the Clean Water Act.",
    },
    {
        "id":          "SEMS",
        "label":       "SEMS",
        "description": "Superfund Enterprise Management System — tracks the nation's most "
                       "severe hazardous waste cleanup sites (Superfund / NPL sites).",
    },
    {
        "id":          "TRIS",
        "label":       "TRIS",
        "description": "Toxics Release Inventory System — annual releases or transfers "
                       "of toxic chemicals to the environment reported by industrial facilities.",
    },
    {
        "id":          "NCDB",
        "label":       "NCDB",
        "description": "National Compliance Database — tracks facilities' compliance status "
                       "under various environmental laws (CAA, CWA, RCRA, SDWA).",
    },
    {
        "id":          "CWNS",
        "label":       "CWNS",
        "description": "Clean Watersheds Needs Survey — assesses capital needs for publicly "
                       "owned wastewater treatment facilities (POTWs).",
    },
    {
        "id":          "FFEP",
        "label":       "FFEP",
        "description": "Federal Facility Environmental Program — environmental compliance "
                       "for federal agencies such as military bases and national labs.",
    },
]

# ---------------------------------------------------------------------------
# Valid ranges for numeric settings.
# ---------------------------------------------------------------------------
LIMITS = {
    "max_topology_depth": {"min": 1,  "max": 20,  "unit": "hops"},
    "resample_freq_h":    {"min": 1,  "max": 168, "unit": "hours"},
    "weather_freq_h":     {"min": 1,  "max": 168, "unit": "hours"},
}

# Auto-freq rules evaluated in order, first match wins.
# Sent to the frontend so it can preview the auto value before submitting.
AUTO_FREQ_RULES = [
    {"max_days":  7,  "resample_h":  6, "weather_h":  12, "label": "<= 7 days"},
    {"max_days": 14,  "resample_h": 12, "weather_h":  24, "label": "<= 14 days"},
    {"max_days": 30,  "resample_h": 24, "weather_h":  48, "label": "<= 30 days"},
    {"max_days": 90,  "resample_h": 48, "weather_h":  72, "label": "<= 90 days"},
    {"max_days": 9999, "resample_h": 72, "weather_h": 168, "label": "> 90 days"},
]


def _parse_freq_h(freq_str: str) -> int:
    """Convert e.g. '6h' -> 6. Defaults to 6 on parse error."""
    try:
        return int(freq_str.rstrip("h"))
    except Exception:
        return 6


@router.get("/defaults")
async def get_settings_defaults():
    """
    Return all selectable options and the current default values.
    Called once by the frontend on startup to populate the Settings panel.
    """
    return {
        "providers":        PROVIDER_CATALOGUE,
        "facility_types":   FACILITY_CATALOGUE,
        "limits":           LIMITS,
        "auto_freq_rules":  AUTO_FREQ_RULES,
        "defaults": {
            "provider":           "ollama",
            "model_name":         settings.DEFAULT_LLM_MODEL,
            "api_key":            "",
            "facility_types":     settings.FACILITY_TYPES,
            "max_topology_depth": settings.MAX_TOPOLOGY_DEPTH,
            "resample_freq_h":    _parse_freq_h(settings.WATER_DATA_RESAMPLE_FREQ),
            "weather_freq_h":     _parse_freq_h(settings.WEATHER_FREQ),
            "resample_auto":      True,
        },
    }