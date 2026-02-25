from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

'''
used for every raver agent.
'''
class RiverAgentData(BaseModel):
    comid: str
    upstream_ids: List[str] = Field(default_factory=list)
    downstream_ids: List[str] = Field(default_factory=list)
    
    # Site Information
    has_usgs_site: bool = False
    site_id: Optional[str] = None
    
    # Coordinates
    lat: float = 0.0
    lon: float = 0.0
    
    # Results and memories
    memory: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[Any] = None
    # LLM I/O stored for Inspector display
    prompt_used:    Optional[str] = None
    llm_raw_output: Optional[str] = None


'''
used for LLM output format (for parameters)
'''
class ParameterInference(BaseModel):
    name: str = Field(description="Parameter name, such as 'pH'")
    code: str = Field(description="USGS parameter code, such as '00400'")
    estimate_value: Optional[float] = Field(
        default=None,
        description="The specific numerical values derived for this river section"
    )
    unit: Optional[str] = Field(
        default=None,
        description="The unit of this parameter, such as 'mg/L', 'cfs'; if there is no unit, it is None"
    )


'''
used for LLM output format (the overall prompt)
'''
class HydrologyInference(BaseModel):
    parameters: List[ParameterInference] = Field(description="List of derivation results for all monitoring parameters.")
    estimated_flow: Optional[float] = Field(
        default=None,
        description="Estimated flow (cfs)"
    )
    confidence: float = Field(description="Derive confidence (0.0-1.0)")
    analysis: str = Field(description="Professional hydrological analysis conclusions incorporating upstream and nearby environmental factors")