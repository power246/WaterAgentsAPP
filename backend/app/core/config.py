from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Dict, Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Water Agent System"
    API_V1_STR: str = "/api/v1"
    
    # Ollama 
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3:8b"
    
    # OpenAI / Grok 
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4-turbo"
    
    # Gemini
    GOOGLE_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-1.5-pro"
    
    # Grok 
    GROK_API_KEY: Optional[str] = None
    GROK_BASE_URL: str = "https://api.x.ai/v1"

    # List of optional models for the frontend
    DEFAULT_LLM_MODEL: str = "llama3:8b"
    AVAILABLE_MODELS: List[str] = ["llama3:8b"]

    # SQLite
    DATABASE_URL: str = "sqlite:///./water_agents.db"

    # Buffer
    REDIS_URL: Optional[str] = None

    # facilities
    FACILITY_TYPES: List[str] = ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"]
    FACILITY_TYPE_DESCRIPTION: Dict[str, str] = {
        "ICIS": "Contains NPDES permit data, discharge monitoring reports (DMRs), and enforcement history under the Clean Water Act...",
        "SEMS": "Tracks the nation’s most severe hazardous waste cleanup sites (Superfund sites)...",
        "TRIS": "Facilities must report annual releases or transfers of toxic chemicals to the environment...",
        "NCDB": "Tracks facilities’ compliance status under various environmental laws...",
        "CWNS": "Assesses capital needs for publicly owned wastewater treatment facilities (POTWs)...",
        "FFEP": "Tracks environmental compliance for federal agencies (like military bases)..."
    }

    # use for get_associated_usgs_site in nldi_service.py
    USGS_SEARCH_DISTANCE: float = 0.1

    # set the data sample frequence
    WATER_DATA_RESAMPLE_FREQ: str = "6h"
    WEATHER_FREQ: str = "12h"

    # the maximum depth of BFS in topology graph
    MAX_TOPOLOGY_DEPTH: int = 10

    # HTTP settings
    HTTP_HEADERS: Dict[str, str] = {
        "User-Agent": "WaterAgentSystem/1.0"
    }
    REQUEST_TIMEOUT: float = 60.0

    # Fetching .env file
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()