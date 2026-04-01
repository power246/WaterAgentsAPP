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


"""
One identifiable source of influence on the inferred parameter value.
"""
class CausalFactor(BaseModel):
    factor: str = Field(
        description="Source of influence, e.g. 'Upstream COMID 12345' or 'ICIS facility XYZ'"
    )
    direction: str = Field(
        description="Direction of effect: 'increasing', 'decreasing', or 'neutral'"
    )
    magnitude: str = Field(
        description="Estimated strength of effect: 'high', 'medium', or 'low'"
    )
    evidence: str = Field(
        description="Specific data or observation that supports this judgment"
    )


"""
Structured causal explanation attached to every inference output.
"""
class HydrologyExplanation(BaseModel):
    primary_drivers: List[CausalFactor] = Field(
        description="All identified factors that influenced the inferred values"
    )
    reasoning_chain: str = Field(
        description=(
            "Full step-by-step causal narrative, e.g. "
            "'A causes B; B affects C through mechanism D'. "
            "In prediction mode this must also describe how values change over time."
        )
    )
    uncertainty_sources: str = Field(
        description="Description of missing data, assumptions made, or other uncertainty"
    )

"""
A sub-daily time window whose value deviates significantly from the daily mean.
"""
class AnomalyWindow(BaseModel):
    time_window: str = Field(
        description="Human-readable window label, e.g. '2026-01-03 06:00 to 12:00'"
    )
    parameter: str = Field(description="Parameter name, e.g. 'pH'")
    observed_value: float = Field(description="Measured or inferred value in this window")
    daily_mean: float = Field(description="Mean value for the full day")
    deviation_pct: float = Field(
        description="Absolute percentage deviation from daily mean"
    )
    possible_cause: str = Field(
        description="LLM-inferred explanation for why this anomaly occurred"
    )

"""
Aggregated result for a single calendar day and a single parameter.
"""
class DailyParameterResult(BaseModel):
    date: str = Field(description="Calendar date, format YYYY-MM-DD")
    representative_value: Optional[float] = Field(
        default=None,
        description="Daily mean used as the single representative value for this day"
    )
    unit: Optional[str] = Field(default=None)
    confidence: float = Field(description="Confidence for this day's value (0.0-1.0)")
    anomalies: List[AnomalyWindow] = Field(
        default_factory=list,
        description="Sub-daily windows flagged as anomalous within this day"
    )

"""
Full time-series result for one parameter across all requested days.
"""
class ParameterResult(BaseModel):
    name: str = Field(description="Parameter name, e.g. 'pH'")
    code: str = Field(description="USGS parameter code, e.g. '00400'")
    unit: Optional[str] = Field(default=None)
    daily_results: List[DailyParameterResult] = Field(
        description=(
            "One entry per day. "
            "Single-day mode: exactly one entry. "
            "Date-range mode: one entry per calendar day."
        )
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
    detailed_results: List[ParameterResult] = Field(
        default_factory=list,
        description=(
            "Full daily breakdown for frontend display (time-series chart, anomaly panel). "
            "The existing 'parameters' field is kept for upstream agent-to-agent passing."
        )
    )
    explanation: Optional[HydrologyExplanation] = Field(
        default=None,
        description="Structured causal explanation; present in every final output."
    )

    # Structured daily flow time-series for frontend display.
    # Mirrors detailed_results structure but for discharge only.
    # estimated_flow (single float above) is kept for upstream agent-to-agent passing.
    flow_results: Optional[ParameterResult] = Field(
        default=None,
        description=(
            "Daily discharge time-series for frontend display. "
            "Uses the same ParameterResult structure as detailed_results entries. "
            "estimated_flow is kept as a single-value summary for upstream passing."
        )
    )
