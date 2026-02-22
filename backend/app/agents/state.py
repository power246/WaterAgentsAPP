# backend/app/agents/state.py

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class AgentSubState(TypedDict):
    """State slice passed to each individual inference node."""
    comid:            str
    start_date:       str
    end_date:         str
    current_params:   List[List[str]]
    facility_types:   List[str]
    task:             str
    upstream_results: Dict[str, Any]

    # LLM selection (forwarded from frontend settings)
    selected_model: str
    provider:       str            # "ollama" | "openai" | "gemini" | "grok"
    api_key:        Optional[str]  # user-supplied key; None -> use server .env

    # Runtime data-fetch settings (forwarded from frontend settings)
    resample_freq: str   # e.g. "6h"  - USGS data resampling interval
    weather_freq:  str   # e.g. "12h" - weather data resampling interval


class WaterSystemState(TypedDict):
    """Global state shared across all nodes in the LangGraph pipeline."""
    task:           str
    current_params: List[List[str]]
    facility_types: List[str]
    start_date:     str
    end_date:       str

    # Merged inference results for every river segment (uses dict-merge reducer)
    global_data: Annotated[Dict[str, Dict[str, Any]], operator.ior]

    # Entry-point COMIDs (river segments with real USGS stations)
    inlet_comids: List[str]

    # LLM settings
    selected_model: str
    provider:       str
    api_key:        Optional[str]

    # Data-fetch settings
    resample_freq: str
    weather_freq:  str