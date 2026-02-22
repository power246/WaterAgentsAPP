from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class RiverAgentData(BaseModel):
    """
    The complete data model of each water body agent.
    Including topological relationships, site information, coordinates, reasoning results, as well as LLM debugging information for front-end display.
    """
    comid: str
    upstream_ids: List[str] = Field(default_factory=list)
    downstream_ids: List[str] = Field(default_factory=list)

    # Site Information
    has_usgs_site: bool = False
    site_id: Optional[str] = None

    # coordinates
    lat: float = 0.0
    lon: float = 0.0

    # Inference results & Memory
    memory: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[Any] = None

    # LLM debugging information
    # The complete prompt sent to LLM (the merged string of system and user)
    prompt_used: Optional[str] = Field(
        default=None,
        description="The full prompt string sent to the LLM (system + user merged)"
    )
    # The original string returned by LLM (without JSON parsing)
    llm_raw_output: Optional[str] = Field(
        default=None,
        description="The raw string output from the LLM before JSON parsing"
    )


class ParameterInference(BaseModel):
    """Inference results of a single water quality parameter"""
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


class HydrologyInference(BaseModel):
    """The complete inference structure output by LLM"""
    parameters: List[ParameterInference] = Field(
        description="List of derivation results for all monitoring parameters."
    )
    estimated_flow: Optional[float] = Field(
        default=None,
        description="Estimated flow (cfs)"
    )
    confidence: float = Field(description="Derive confidence (0.0-1.0)")
    analysis: str = Field(
        description="Professional hydrological analysis conclusions "
                    "incorporating upstream and nearby environmental factors"
    )