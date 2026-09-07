import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


# The "package" received by each parallel sub-node
class AgentSubState(TypedDict):
    comid: str
    start_date: str
    end_date: str
    current_params: List[List[str]]
    facility_types: List[str]
    task: str
    upstream_results: Dict[str, Any]
    selected_model: str
    
    # Resampling settings forwarded from frontend settings panel
    resample_auto:   bool   # True = auto-select interval from date range
    resample_freq_h: int    # Manual override: interval in hours (ignored when auto=True)
    
    # Inference mode: "gap_filling" (historical) or "prediction" (future)
    mode: str
    # Time granularity: "single_day" (start == end) or "date_range"
    time_mode: str
    # Ordered list of time segments; mixed-range queries produce two entries:
    # [{"start":..., "end":..., "mode":"gap_filling"},
    #  {"start":..., "end":..., "mode":"prediction"}]
    segments: List[Dict[str, Any]]


# Global state definition of the Water Agent system
class WaterSystemState(TypedDict):
    task: str                        # Description of the current task
    current_params: List[List[str]]  # Monitoring parameters, such as [['Discharge', '00060']]
    facility_types: List[str]        # Facility type filtering, such as ['ICIS', 'TRIS']
    start_date: str                  # Starting time of the analysis
    end_date: str                    # Ending time of the analysis
    
    # Store the inference results for each COMID
    global_data: Annotated[Dict[str, Dict[str, Any]], operator.ior]
    
    # Queue to be processed (entry point in tree structure)
    inlet_comids: List[str]

    # The model name selected by the frontend defaults to the setting in the config
    selected_model: str
    # Resampling settings forwarded from frontend settings panel
    resample_auto:   bool   # True = auto-select interval from date range
    resample_freq_h: int    # Manual override: interval in hours (ignored when auto=True)
    
    # Inference mode: "gap_filling", "prediction", or "mixed"
    mode: str
    # Time granularity: "single_day" or "date_range"
    time_mode: str
    # Ordered list of time segments (see AgentSubState for structure)
    segments: List[Dict[str, Any]]