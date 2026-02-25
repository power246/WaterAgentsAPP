import json
import re
import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.agents.state import AgentSubState
from app.services.nwis_service import NWISService
from app.services.environment_service import EnvironmentService
from app.services.memory_service import MemoryService
from app.services.llm_service import LLMService
from app.core.config import settings
from app.models.river import RiverAgentData, HydrologyInference
from app.services.river_manager import RiverManager
from app.agents.prompts import (
    HYDRO_USGS_PROMPT,
    HYDRO_INFER_PROMPT,
    FACILITY_INFO_STR,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data formatting helpers
# These functions convert raw API responses into clean, readable text blocks
# that small LLMs (llama3:8b) can process reliably without being confused by
# deeply nested JSON.
# ─────────────────────────────────────────────────────────────────────────────

# Auto-resample interval table (matches frontend Settings panel auto mode)
# List of (day_threshold, hours_per_bucket) pairs, checked in order.
_AUTO_USGS_INTERVALS = [
    (7,    6),
    (14,   12),
    (30,   24),
    (90,   48),
    (9999, 72),
]


def _auto_resample_hours(start_date: str, end_date: str) -> int:
    """
    Return the appropriate USGS resample interval in hours for the given date range,
    matching the frontend auto-resample table:
      <= 7 days  ->  6h
      <= 14 days -> 12h
      <= 30 days -> 24h
      <= 90 days -> 48h
      > 90 days  -> 72h
    Falls back to 12h if dates cannot be parsed.
    """
    try:
        d0 = datetime.strptime(start_date, "%Y-%m-%d")
        d1 = datetime.strptime(end_date,   "%Y-%m-%d")
        days = (d1 - d0).days
    except ValueError:
        return 12  # safe fallback

    for threshold, hours in _AUTO_USGS_INTERVALS:
        if days <= threshold:
            return hours
    return 72


def _resample_to_nh(data_6h: List[Dict], interval_hours: int) -> List[Dict]:
    """
    Average raw 6-hour USGS readings into N-hour windows.
    interval_hours should be a multiple of 6 (6, 12, 24, 48, 72).
    Returns a list of {"date": "YYYY-MM-DD HH:MM", "avg_value": float}.
    Windows that contain only null readings are omitted for a cleaner output.
    """
    if interval_hours < 6:
        interval_hours = 6

    buckets: Dict[str, List[float]] = defaultdict(list)
    for pt in data_6h:
        date_str = pt.get("date", "")
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        # Snap hour down to the nearest multiple of interval_hours
        snapped_hour = (dt.hour // interval_hours) * interval_hours
        bucket_key = f"{dt.strftime('%Y-%m-%d')} {snapped_hour:02d}:00"
        if pt.get("avg_value") is not None:
            buckets[bucket_key].append(float(pt["avg_value"]))

    result = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        if vals:
            result.append({
                "date":      key,
                "avg_value": round(sum(vals) / len(vals), 4),
            })
    return result


def _format_target_params_block(requested: List[List[str]]) -> str:
    """
    Format the target parameters list as a numbered plain-text block.
    requested: [[name, code], ...]
    Example output:
      1. pH  (USGS code: 00400)
      2. Alkalinity  (USGS code: 00409)
    """
    lines = []
    for i, (name, code) in enumerate(requested, 1):
        lines.append(f"  {i}. {name}  (USGS code: {code})")
    return "\n".join(lines) if lines else "  (none specified)"


def _format_sensor_data_block(
    obs_dict: Optional[Dict],
    requested: List[List[str]],
    interval_hours: int = 12,
) -> str:
    """
    Format USGS observation time-series into a human-readable text block.
    Each requested parameter gets its own section at the given interval.
    Parameters with no sensor data are listed explicitly as "[NO DATA]".

    obs_dict:       raw return from NWISService.get_daily_averages()
    requested:      [[name, code], ...]
    interval_hours: resampling window in hours (6 / 12 / 24 / 48 / 72)
    """
    if not obs_dict:
        return "  [No observation data available]"

    # Index available time-series by parameter code
    ts_by_code: Dict[str, Dict] = {}
    for ts in obs_dict.get("averages", []):
        ts_by_code[ts["parameter_code"]] = ts

    lines = []
    for req_name, req_code in requested:
        ts = ts_by_code.get(req_code)
        if ts is None:
            lines.append(f"\n{req_name} (code {req_code}):")
            lines.append("  [NO SENSOR DATA — set estimate_value to null]")
            continue

        unit = ts.get("unit", "")
        resampled = _resample_to_nh(ts.get("data_6h", []), interval_hours)
        valid = [r for r in resampled if r["avg_value"] is not None]

        if not valid:
            lines.append(f"\n{req_name} (code {req_code}, {unit}):")
            lines.append("  [ALL READINGS NULL — set estimate_value to null]")
            continue

        lines.append(f"\n{req_name} (code {req_code}, {unit}):")
        for r in valid:
            lines.append(f"  {r['date']}  ->  {r['avg_value']}")

    return "\n".join(lines) if lines else "  [No usable sensor data]"


def _format_flow_block(flow_dict: Optional[Dict], interval_hours: int = 12) -> str:
    """
    Format discharge (flow) time-series into a readable text block.
    Shows readings at the given interval. If all readings are null, says so.

    flow_dict:      raw return from NWISService.get_flow_data()
    interval_hours: resampling window in hours
    """
    if not flow_dict:
        return "  [No discharge data — set estimated_flow to null]"

    lines = []
    for ts in flow_dict.get("averages", []):
        unit = ts.get("unit", "ft3/s")
        resampled = _resample_to_nh(ts.get("data_6h", []), interval_hours)
        valid = [r for r in resampled if r["avg_value"] is not None]
        if not valid:
            lines.append(f"  [All discharge readings null for this period — set estimated_flow to null]")
            continue
        lines.append(f"  Streamflow ({unit}):")
        for r in valid:
            lines.append(f"    {r['date']}  ->  {r['avg_value']}")

    return "\n".join(lines) if lines else "  [No discharge data — set estimated_flow to null]"


def _format_env_block(env_factors: Optional[Dict]) -> str:
    """
    Format environmental data (facilities, weather, alerts) as readable text.
    """
    if not env_factors:
        return "  [No environmental data available]"

    parts = []

    # Facilities
    facilities = env_factors.get("facilities", [])
    if facilities:
        parts.append("Nearby facilities:")
        for f in facilities:
            types = ", ".join(f.get("program_types", []))
            parts.append(
                f"  - {f.get('name', 'Unknown')} ({types})"
                f"  at ({f.get('lat')}, {f.get('lon')})"
                f"  — {f.get('address', '')}"
            )
        parts.append(
            "  Facility type meanings:\n" + FACILITY_INFO_STR
        )
    else:
        parts.append("Nearby facilities: none found within search radius.")

    # Weather observations
    weather = env_factors.get("weather", [])
    if weather:
        parts.append("\nWeather observations (recent):")
        for w in weather[-6:]:  # Show last 6 entries (3 days at 12h)
            temp = f"{w.get('temp_c')} C" if w.get("temp_c") is not None else "N/A"
            precip = f"{w.get('precip_mm')} mm" if w.get("precip_mm") is not None else "N/A"
            parts.append(f"  {w.get('time', '')}  temp={temp}  precip={precip}")
    else:
        parts.append("\nWeather: no observations available.")

    # Alerts
    alerts = env_factors.get("alerts", [])
    if alerts:
        parts.append("\nActive weather alerts:")
        for a in alerts:
            window = a.get("window", "")
            for evt in a.get("active_alerts", []):
                parts.append(f"  [{window}] {evt.get('event')} (severity: {evt.get('severity')})")
    else:
        parts.append("Active weather alerts: none.")

    return "\n".join(parts)


def _format_history_block(history_context: List[Dict]) -> str:
    """
    Format the last 2 historical inference records into a readable summary.
    Only extracts numeric values and key conclusions — not raw JSON structure.
    """
    if not history_context:
        return "  [No previous inferences for this segment]"

    parts = []
    for i, record in enumerate(history_context, 1):
        ts = record.get("timestamp", "unknown time")
        task = record.get("task", "")
        inf = record.get("structured_inference", {})

        if not inf or "error" in inf:
            parts.append(f"Inference {i} ({ts}): failed or no structured data.")
            continue

        parts.append(f"Inference {i} ({ts}):")
        parts.append(f"  Task: {task}")

        for p in inf.get("parameters", []):
            val = p.get("estimate_value")
            unit = p.get("unit", "")
            val_str = f"{val} {unit}".strip() if val is not None else "null"
            parts.append(f"  {p.get('name', '?')}: {val_str}")

        flow = inf.get("estimated_flow")
        parts.append(f"  Flow: {flow} ft3/s" if flow is not None else "  Flow: null")

        conf = inf.get("confidence")
        parts.append(f"  Confidence: {conf}")

        analysis = inf.get("analysis", "")
        if analysis and len(analysis) > 20:
            # Trim to first 200 chars so it doesn't overwhelm the context
            snippet = analysis[:200].rstrip() + ("..." if len(analysis) > 200 else "")
            parts.append(f"  Analysis snippet: {snippet}")

    return "\n".join(parts)


def _format_upstream_block(upstream_results: Dict[str, Any]) -> str:
    """
    Format upstream agent outputs into a clear readable block.
    upstream_results: {comid: inference_result_dict, ...}
    inference_result_dict is the HydrologyInference.model_dump() output.
    """
    if not upstream_results:
        return "  [No upstream segments — this is a headwater segment]"

    parts = []
    for comid, result in upstream_results.items():
        parts.append(f"\nUpstream COMID {comid}:")
        if result is None or "error" in (result or {}):
            parts.append("  [Upstream inference failed — no data available]")
            continue

        # Parameters
        for p in result.get("parameters", []):
            val = p.get("estimate_value")
            unit = p.get("unit", "")
            val_str = f"{val} {unit}".strip() if val is not None else "null (no data)"
            parts.append(f"  {p.get('name', '?')} (code {p.get('code', '?')}): {val_str}")

        # Flow
        flow = result.get("estimated_flow")
        parts.append(f"  Estimated flow: {flow} ft3/s" if flow is not None else "  Estimated flow: null")

        # Confidence
        conf = result.get("confidence")
        parts.append(f"  Upstream confidence: {conf}")

        # Analysis summary (first 250 chars)
        analysis = result.get("analysis", "")
        if analysis and len(analysis) > 20:
            snippet = analysis[:250].rstrip() + ("..." if len(analysis) > 250 else "")
            parts.append(f"  Upstream analysis: {snippet}")

    return "\n".join(parts) if parts else "  [No upstream data]"


# ─────────────────────────────────────────────────────────────────────────────
# JSON normalization helpers
# llama3 frequently violates the output schema. These functions fix the most
# common violations so that Pydantic validation can still succeed.
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_parameters(
    params_raw: Any,
    comid: str,
) -> Tuple[List[Dict], Optional[float], Optional[str]]:
    """
    Normalize "parameters" to a clean list of {name, code, estimate_value, unit}.
    Also rescue "confidence" and "analysis" if llama3 buried them inside
    parameter entries instead of at the top level.

    Returns: (normalized_list, rescued_confidence, rescued_analysis)
    """
    rescued_confidence: Optional[float] = None
    rescued_analysis: Optional[str] = None
    normalized: List[Dict] = []

    # If the LLM returned a dict instead of a list, convert to list of values
    if isinstance(params_raw, dict):
        params_raw = list(params_raw.values())
        logger.warning(f"[Agent {comid}] 'parameters' was a dict — converted to list")

    if not isinstance(params_raw, list):
        logger.warning(f"[Agent {comid}] 'parameters' has unexpected type {type(params_raw)} — treated as empty")
        return [], rescued_confidence, rescued_analysis

    for entry in params_raw:
        if not isinstance(entry, dict):
            continue

        # Rescue top-level fields that llama3 sometimes buries inside entries
        if rescued_confidence is None:
            val = entry.get("confidence")
            if isinstance(val, (int, float)):
                rescued_confidence = float(val)

        if rescued_analysis is None:
            val = entry.get("analysis")
            if isinstance(val, str) and len(val) > 15:
                rescued_analysis = val

        # Remap alternative field names to the canonical schema
        name = (
            entry.get("name")
            or entry.get("parameter_name")
            or entry.get("param_name")
            or ""
        )
        code = (
            entry.get("code")
            or entry.get("parameter_code")
            or entry.get("param_code")
            or ""
        )
        # estimate_value: canonical name first, then common alternatives
        ev = entry.get("estimate_value")
        if ev is None:
            ev = entry.get("estimated_value",
                 entry.get("value",
                 entry.get("avg_value", None)))

        unit = entry.get("unit") or ""

        normalized.append({
            "name":           name,
            "code":           code,
            "estimate_value": ev,
            "unit":           unit,
        })

    return normalized, rescued_confidence, rescued_analysis


def _normalize_analysis(raw_analysis: Any, comid: str) -> str:
    """
    Ensure "analysis" is a plain string.
    llama3 sometimes returns a dict or list.
    """
    if isinstance(raw_analysis, str) and len(raw_analysis) > 5:
        return raw_analysis

    if isinstance(raw_analysis, dict):
        parts = []
        for k, v in raw_analysis.items():
            if isinstance(v, str):
                parts.append(f"{k}: {v}")
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        heading = item.get("heading", "")
                        text    = item.get("text", "")
                        parts.append(f"{heading}: {text}" if heading else text)
                    elif isinstance(item, str):
                        parts.append(item)
        result = "\n".join(parts)
        logger.warning(f"[Agent {comid}] 'analysis' was a dict — flattened to string")
        return result

    if isinstance(raw_analysis, list):
        result = " ".join(str(x) for x in raw_analysis)
        logger.warning(f"[Agent {comid}] 'analysis' was a list — joined to string")
        return result

    return ""


def _inject_missing_params(
    normalized: List[Dict],
    requested: List[List[str]],
    comid: str,
) -> List[Dict]:
    """
    Ensure every requested parameter appears in the output.
    If the LLM omitted one, inject a null entry and log a warning.
    """
    output_codes = {p["code"].strip() for p in normalized if p.get("code")}
    output_names = {p["name"].strip().lower() for p in normalized if p.get("name")}

    for req_name, req_code in requested:
        already_present = (
            req_code.strip() in output_codes
            or req_name.strip().lower() in output_names
        )
        if not already_present:
            normalized.append({
                "name":           req_name,
                "code":           req_code,
                "estimate_value": None,
                "unit":           None,
            })
            logger.warning(
                f"[Agent {comid}] Parameter '{req_name}' (code {req_code}) "
                f"was omitted by LLM — injected with null"
            )

    return normalized


# ─────────────────────────────────────────────────────────────────────────────
# Main inference node
# ─────────────────────────────────────────────────────────────────────────────

async def inference_node(state: AgentSubState) -> Dict:
    """
    Core inference node for a single COMID.
    Selects the appropriate prompt (USGS sensor vs upstream-only),
    calls the LLM, and normalizes the output back into a valid
    HydrologyInference structure.
    """
    comid = state["comid"]
    logger.info(f"[Agent {comid}] Inference started")

    # ── 1. Initialize river model ──────────────────────────────────────────
    river = RiverAgentData(comid=comid)

    # ── 2. Load memory (past inferences + cached metadata) ────────────────
    file_path = f"./data/memory/{comid}.json"
    memory = await MemoryService.load_memory(file_path)
    history_context = memory.get("past_inferences", [])[-2:]
    river.memory = memory
    cached_metadata = memory.get("static_metadata", {})

    # ── 3. Resolve geographic metadata (coordinates + USGS site ID) ───────
    if cached_metadata.get("lat") and cached_metadata.get("site_id"):
        logger.info(f"[Agent {comid}] Metadata loaded from memory cache")
        river.lat         = cached_metadata["lat"]
        river.lon         = cached_metadata["lon"]
        river.site_id     = cached_metadata["site_id"]
        river.has_usgs_site = True
    else:
        logger.info(f"[Agent {comid}] Fetching metadata via RiverManager (cache miss)")
        metadata = await RiverManager.get_river_metadata(comid)
        river.lat         = metadata["lat"]
        river.lon         = metadata["lon"]
        river.site_id     = metadata["site_id"]
        river.has_usgs_site = metadata["has_usgs_site"]
        memory["static_metadata"] = metadata

    # ── 4. Fetch external data concurrently ───────────────────────────────
    param_codes = state["current_params"]  # [[name, code], ...]
    logger.info(f"[Agent {comid}] Fetching external data (EPA, weather, USGS)...")

    env_task  = EnvironmentService.get_environmental_factors(
        comid, river.lat, river.lon,
        state["start_date"], state["end_date"],
        state["facility_types"],
    )
    # Flow and observations are only fetched for USGS-equipped nodes
    flow_task = (
        NWISService.get_flow_data(river.site_id, state["start_date"], state["end_date"])
        if river.has_usgs_site else asyncio.sleep(0, result=None)
    )
    obs_task = (
        NWISService.get_daily_averages(
            river.site_id, state["start_date"], state["end_date"], param_codes
        )
        if river.has_usgs_site else asyncio.sleep(0, result=None)
    )

    try:
        env_factors, flow_data, obs_data = await asyncio.gather(
            env_task, flow_task, obs_task
        )
    except Exception as exc:
        logger.error(f"[Agent {comid}] Data fetch failed: {exc}")
        env_factors, flow_data, obs_data = {}, None, None

    # ── 5. Build prompt ───────────────────────────────────────────────────
    target_params_block = _format_target_params_block(param_codes)
    env_block           = _format_env_block(env_factors)
    history_block       = _format_history_block(history_context)

    if river.has_usgs_site:
        # USGS sensor node: show real sensor readings
        # Resolve resampling interval: auto-select from date range or use manual override
        if state.get("resample_auto", True):
            interval_h = _auto_resample_hours(state["start_date"], state["end_date"])
            logger.info(
                f"[Agent {comid}] Auto resample: "
                f"{state['start_date']} to {state['end_date']} -> {interval_h}h"
            )
        else:
            interval_h = int(state.get("resample_freq_h") or 12)
            logger.info(f"[Agent {comid}] Manual resample: {interval_h}h")

        sensor_block = _format_sensor_data_block(obs_data, param_codes, interval_h)
        flow_block   = _format_flow_block(flow_data, interval_h)

        logger.info(f"[Agent {comid}] Using USGS prompt (site {river.site_id})")
        messages = HYDRO_USGS_PROMPT.format_messages(
            comid              = comid,
            FACILITY_INFO_STR  = FACILITY_INFO_STR,
            start_date         = state["start_date"],
            end_date           = state["end_date"],
            task_description   = state["task"],
            site_id            = river.site_id,
            target_params_block = target_params_block,
            sensor_data_block  = sensor_block,
            flow_block         = flow_block,
            env_block          = env_block,
            history_block      = history_block,
        )
    else:
        # Inference-only node: pass upstream agent outputs
        upstream_results = state.get("upstream_results", {})
        upstream_block   = _format_upstream_block(upstream_results)

        logger.info(f"[Agent {comid}] Using inference prompt (no local USGS site)")
        messages = HYDRO_INFER_PROMPT.format_messages(
            comid              = comid,
            FACILITY_INFO_STR  = FACILITY_INFO_STR,
            start_date         = state["start_date"],
            end_date           = state["end_date"],
            task_description   = state["task"],
            target_params_block = target_params_block,
            upstream_block     = upstream_block,
            env_block          = env_block,
            history_block      = history_block,
        )

    system_content = messages[0].content
    user_content   = messages[1].content
    full_prompt    = f"{system_content}\n\n{user_content}"

    # ── 6. Call LLM ───────────────────────────────────────────────────────
    llm_output = await LLMService.call_llm(
        prompt     = full_prompt,
        model_name = state.get("selected_model") or settings.DEFAULT_LLM_MODEL,
    )

    # ── 7. Parse and normalize LLM output ─────────────────────────────────
    # llama3-family models frequently deviate from the schema:
    #   - "parameters" as a dict instead of a list
    #   - field names like "parameter_name", "COMID" inside entries
    #   - "confidence" / "analysis" inside parameter entries, not at root
    #   - "analysis" as a dict or list instead of a string
    # We normalize all of these before Pydantic validation.
    inference_result: Optional[Dict] = None
    try:
        # Extract outermost JSON object from raw LLM text
        match    = re.search(r"(\{.*\})", llm_output, re.DOTALL)
        json_str = match.group(1) if match else llm_output
        raw      = json.loads(json_str)

        # Normalize parameters list
        params_raw = raw.get("parameters", [])
        normalized, rescued_confidence, rescued_analysis = _normalize_parameters(
            params_raw, comid
        )

        # Inject any parameters the LLM completely omitted
        normalized = _inject_missing_params(normalized, param_codes, comid)
        raw["parameters"] = normalized

        # Rescue or default top-level "confidence"
        if not isinstance(raw.get("confidence"), (int, float)):
            if rescued_confidence is not None:
                raw["confidence"] = rescued_confidence
                logger.warning(
                    f"[Agent {comid}] 'confidence' rescued from parameter entry: "
                    f"{rescued_confidence}"
                )
            else:
                # Derive a sensible default from data availability
                default_conf = 0.75 if river.has_usgs_site else 0.50
                raw["confidence"] = default_conf
                logger.warning(
                    f"[Agent {comid}] 'confidence' missing — defaulting to {default_conf}"
                )

        # Rescue or default top-level "analysis"
        raw_analysis = raw.get("analysis", "")
        clean_analysis = _normalize_analysis(raw_analysis, comid)
        if not clean_analysis and rescued_analysis:
            clean_analysis = rescued_analysis
            logger.warning(f"[Agent {comid}] 'analysis' rescued from parameter entry")
        if not clean_analysis:
            clean_analysis = "Analysis not provided by model."
            logger.warning(f"[Agent {comid}] 'analysis' missing — using placeholder")
        raw["analysis"] = clean_analysis

        # Validate with Pydantic
        parsed_data      = HydrologyInference.model_validate(raw)
        inference_result = parsed_data.model_dump()

        # Persist LLM I/O for Inspector display
        river.output         = inference_result
        river.prompt_used    = full_prompt
        river.llm_raw_output = llm_output

        logger.info(
            f"[Agent {comid}] Parsed successfully: "
            f"{len(inference_result.get('parameters', []))} parameters, "
            f"confidence={inference_result.get('confidence')}"
        )

    except Exception as exc:
        raw_preview = str(llm_output)[:300]
        logger.error(
            f"[Agent {comid}] JSON validation failed: {exc}\n"
            f"Output preview: {raw_preview}..."
        )
        inference_result = {
            "error":    "JSON parsing failed",
            "analysis": f"Raw output: {llm_output}",
        }
        river.output         = inference_result
        river.prompt_used    = full_prompt
        river.llm_raw_output = llm_output

    # ── 8. Persist memory ─────────────────────────────────────────────────
    memory.setdefault("past_inferences", []).append({
        "timestamp":            datetime.now().isoformat(),
        "task":                 state["task"],
        "structured_inference": inference_result,
        "llm_result":           llm_output,
    })
    # Keep only the last 10 inferences to prevent unbounded file growth
    memory["past_inferences"] = memory["past_inferences"][-10:]
    await MemoryService.save_memory(file_path, memory)

    logger.info(f"[Agent {comid}] Inference complete")
    return {"global_data": {comid: river.model_dump()}}