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
from app.models.river import RiverAgentData, HydrologyInference, ParameterResult
from app.services.river_manager import RiverManager
from app.services.weather_service import WeatherService
from app.agents.prompts import (
    HYDRO_USGS_PROMPT,
    HYDRO_INFER_PROMPT,
    HYDRO_USGS_PREDICTION_PROMPT,
    HYDRO_INFER_PREDICTION_PROMPT,
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


def _format_daily_summary_block(daily_summary: List[ParameterResult]) -> str:
    """
    Format the anomaly summary computed by NWISService.compute_daily_summary()
    into a readable text block for the LLM.

    Only anomaly windows are shown — normal days are omitted to keep the
    prompt concise. If no anomalies exist, a brief "none detected" note is shown.

    daily_summary: List[ParameterResult] returned by compute_daily_summary()
    """
    if not daily_summary:
        return "  [No sub-daily data available for anomaly detection]"

    parts = []
    total_anomalies = 0

    for param_result in daily_summary:
        anomaly_lines = []
        for day in param_result.daily_results:
            for anomaly in day.anomalies:
                anomaly_lines.append(
                    f"  {anomaly.time_window} | "
                    f"observed={anomaly.observed_value} {param_result.unit or ''} | "
                    f"daily_mean={anomaly.daily_mean} | "
                    f"deviation={anomaly.deviation_pct:.1f}%"
                )
                total_anomalies += 1

        if anomaly_lines:
            parts.append(f"\n{param_result.name} (code {param_result.code}) anomalies:")
            parts.extend(anomaly_lines)

    if not parts:
        return "  [No anomalies detected — all sub-daily readings within normal range]"

    header = f"Total anomalies detected: {total_anomalies}"
    return header + "\n" + "\n".join(parts)


def _format_forecast_block(forecast_data: Optional[Dict]) -> str:
    """
    Format NOAA weather forecast data (from WeatherService.get_weather_forecast())
    into a readable text block for prediction-mode prompts.

    Each entry shows temp_c, precip_prob_pct (PoP), and precip_mm (QPF).
    Both precipitation fields are included so the LLM can reason about
    the difference between rainfall probability and actual rainfall amount.

    forecast_data: dict returned by get_weather_forecast(), same structure as
                   get_weather_and_alerts() — {"observations": [...], "alerts": []}
    """
    if not forecast_data:
        return "  [No forecast data available — use upstream values and general seasonal reasoning]"

    observations = forecast_data.get("observations", [])
    if not observations:
        return "  [Forecast returned empty — NOAA may not cover this location]"

    lines = ["Weather forecast (NOAA):"]
    for obs in observations:
        temp    = f"{obs.get('temp_c')} C" if obs.get("temp_c") is not None else "N/A"
        pop     = f"{obs.get('precip_prob_pct', 0.0)}%"
        qpf     = f"{obs.get('precip_mm', 0.0)} mm"
        lines.append(
            f"  {obs.get('time', '')}  "
            f"temp={temp}  "
            f"precip_prob={pop}  "
            f"precip_mm={qpf}"
        )

    return "\n".join(lines)


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

        # Show the data period this inference covered, not just when it ran.
        # Without this, the LLM cannot tell whether a past result is relevant
        # to the current query's time range.
        start_d = record.get("start_date", "")
        end_d   = record.get("end_date",   "")
        period_str = f"{start_d} to {end_d}" if start_d and end_d else "unknown period"

        parts.append(f"Inference {i} (run at {ts}, data period: {period_str}):")
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

        # Flow — single representative value for mass balance reference
        flow = result.get("estimated_flow")
        parts.append(
            f"  Estimated flow (summary): {flow} ft3/s"
            if flow is not None
            else "  Estimated flow (summary): null"
        )

        # Flow — daily time-series for day-by-day mass balance reasoning
        flow_results = result.get("flow_results")
        if flow_results and isinstance(flow_results, dict):
            daily = flow_results.get("daily_results", [])
            unit  = flow_results.get("unit", "ft3/s")
            if daily:
                parts.append(f"  Flow by day ({unit}):")
                for day in daily:
                    val     = day.get("representative_value")
                    val_str = f"{val}" if val is not None else "null"
                    parts.append(f"    {day.get('date', '?')}: {val_str} {unit}")
            else:
                parts.append("  Flow by day: [no daily data]")
        else:
            parts.append("  Flow by day: [not provided by upstream]")

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


async def _call_llm_and_normalize(
    full_prompt: str,
    param_codes: List[List[str]],
    river: RiverAgentData,
    comid: str,
    selected_model: str,
) -> Tuple[Optional[Dict], str]:
    """
    Call the LLM with a pre-built prompt, normalize the output, validate with
    Pydantic.

    Returns:
        (inference_result_dict, llm_raw_output)
        inference_result_dict is None if JSON parsing fails entirely.
    """
    llm_output = await LLMService.call_llm(
        prompt=full_prompt,
        model_name=selected_model or settings.DEFAULT_LLM_MODEL,
    )

    try:
        match    = re.search(r"(\{.*\})", llm_output, re.DOTALL)
        json_str = match.group(1) if match else llm_output
        raw      = json.loads(json_str)

        # Normalize parameters list (existing logic)
        params_raw = raw.get("parameters", [])
        normalized, rescued_conf, rescued_analysis = _normalize_parameters(params_raw, comid)
        normalized = _inject_missing_params(normalized, param_codes, comid)
        raw["parameters"] = normalized

        # Rescue / default confidence
        if not isinstance(raw.get("confidence"), (int, float)):
            if rescued_conf is not None:
                raw["confidence"] = rescued_conf
                logger.warning(f"[Agent {comid}] confidence rescued from parameter entry: {rescued_conf}")
            else:
                default_conf = 0.75 if river.has_usgs_site else 0.50
                raw["confidence"] = default_conf
                logger.warning(f"[Agent {comid}] confidence missing — defaulting to {default_conf}")

        # Rescue / default analysis
        raw_analysis  = raw.get("analysis", "")
        clean_analysis = _normalize_analysis(raw_analysis, comid)
        if not clean_analysis and rescued_analysis:
            clean_analysis = rescued_analysis
            logger.warning(f"[Agent {comid}] analysis rescued from parameter entry")
        if not clean_analysis:
            clean_analysis = "Analysis not provided by model."
            logger.warning(f"[Agent {comid}] analysis missing — using placeholder")
        raw["analysis"] = clean_analysis

        # Sprint 6: validate explanation field — insert placeholder if absent
        if not raw.get("explanation"):
            logger.error(
                f"[Agent {comid}] 'explanation' field missing from LLM output — "
                f"inserting placeholder"
            )
            raw["explanation"] = {
                "primary_drivers": [
                    {
                        "factor":    "Data limitation",
                        "direction": "neutral",
                        "magnitude": "low",
                        "evidence":  "LLM did not produce a structured explanation block.",
                    }
                ],
                "reasoning_chain":     clean_analysis[:300] if clean_analysis else "No reasoning provided.",
                "uncertainty_sources": "Explanation block was absent from LLM output.",
            }

        # Warn if flow_results is missing — do not fail validation,
        # as some nodes may genuinely have no discharge data.
        if raw.get("flow_results") is None:
            logger.warning(
                f"[Agent {comid}] 'flow_results' missing from LLM output — "
                f"frontend will show no discharge time-series for this node"
            )

        # Pydantic validation
        parsed = HydrologyInference.model_validate(raw)
        result = parsed.model_dump()

        n_drivers = len((result.get("explanation") or {}).get("primary_drivers", []))
        logger.info(
            f"[Agent {comid}] Parsed: {len(result.get('parameters', []))} params | "
            f"confidence={result.get('confidence')} | "
            f"explanation_drivers={n_drivers}"
        )
        return result, llm_output

    except Exception as exc:
        logger.error(
            f"[Agent {comid}] JSON validation failed: {exc}\n"
            f"Preview: {str(llm_output)[:300]}..."
        )
        return None, llm_output


def _merge_inference_results(
    result_a: Optional[Dict],
    result_b: Optional[Dict],
    comid: str,
) -> Dict:
    """
    Merge gap_filling (result_a) and prediction (result_b) outputs for mixed mode.

    Merge strategy:
      detailed_results : combine daily_results lists per parameter, sorted by date.
      explanation      : concatenate primary_drivers; join reasoning_chains with
                         section labels; join uncertainty_sources.
      confidence       : min(conf_a, conf_b) — prediction is always less certain.
      parameters       : taken from result_a (gap_filling values used for
                         upstream agent-to-agent passing).
      estimated_flow   : taken from result_a.
      analysis         : both analyses concatenated with a section separator.
    """
    if not result_a and not result_b:
        return {"error": "Both passes failed", "analysis": "Mixed mode inference failed."}
    if not result_a:
        logger.warning(f"[Agent {comid}] Mixed merge: gap_filling pass failed — using prediction only")
        return result_b
    if not result_b:
        logger.warning(f"[Agent {comid}] Mixed merge: prediction pass failed — using gap_filling only")
        return result_a

    # Merge detailed_results by parameter name
    merged_detailed: Dict[str, Dict] = {}
    for pr in (result_a.get("detailed_results") or []):
        name = pr.get("name", "")
        if name:
            merged_detailed[name] = {
                "name":          pr.get("name"),
                "code":          pr.get("code"),
                "unit":          pr.get("unit"),
                "daily_results": list(pr.get("daily_results") or []),
            }

    for pr in (result_b.get("detailed_results") or []):
        name = pr.get("name", "")
        if not name:
            continue
        if name in merged_detailed:
            existing_dates = {d["date"] for d in merged_detailed[name]["daily_results"]}
            for day in (pr.get("daily_results") or []):
                if day.get("date") not in existing_dates:
                    merged_detailed[name]["daily_results"].append(day)
        else:
            merged_detailed[name] = dict(pr)

    # Sort each parameter's daily_results chronologically
    for pr in merged_detailed.values():
        pr["daily_results"] = sorted(
            pr["daily_results"], key=lambda d: d.get("date", "")
        )

    # Merge explanation
    exp_a = result_a.get("explanation") or {}
    exp_b = result_b.get("explanation") or {}
    drivers_a = exp_a.get("primary_drivers") or []
    drivers_b = exp_b.get("primary_drivers") or []
    chain_a   = exp_a.get("reasoning_chain", "")
    chain_b   = exp_b.get("reasoning_chain", "")
    unc_a     = exp_a.get("uncertainty_sources", "")
    unc_b     = exp_b.get("uncertainty_sources", "")

    merged_explanation = {
        "primary_drivers":    drivers_a + drivers_b,
        "reasoning_chain":    "\n\n".join(filter(None, [
            f"[HISTORICAL SEGMENT] {chain_a}" if chain_a else "",
            f"[PREDICTION SEGMENT] {chain_b}" if chain_b else "",
        ])),
        "uncertainty_sources": "\n".join(filter(None, [unc_a, unc_b])),
    }

    analysis_a = result_a.get("analysis", "")
    analysis_b = result_b.get("analysis", "")
    merged_analysis = "\n\n--- PREDICTION SEGMENT ---\n\n".join(
        filter(None, [analysis_a, analysis_b])
    )

    conf_a = result_a.get("confidence", 0.5)
    conf_b = result_b.get("confidence", 0.3)

    logger.info(
        f"[Agent {comid}] Mixed merge complete: "
        f"{len(merged_detailed)} params, "
        f"conf={min(conf_a, conf_b)}, "
        f"drivers={len(merged_explanation['primary_drivers'])}"
    )

    # Merge flow_results: combine daily_results from both passes, sorted by date
    flow_a = result_a.get("flow_results") or {}
    flow_b = result_b.get("flow_results") or {}
    merged_flow: Optional[Dict] = None
    if flow_a or flow_b:
        name = flow_a.get("name") or flow_b.get("name") or "Discharge"
        code = flow_a.get("code") or flow_b.get("code") or "00060"
        unit = flow_a.get("unit") or flow_b.get("unit") or "ft3/s"
        days_a = flow_a.get("daily_results") or []
        days_b = flow_b.get("daily_results") or []
        # Merge by date, avoiding duplicates
        existing_dates = {d["date"] for d in days_a if d.get("date")}
        merged_days = list(days_a) + [
            d for d in days_b if d.get("date") not in existing_dates
        ]
        merged_days = sorted(merged_days, key=lambda d: d.get("date", ""))
        merged_flow = {
            "name":          name,
            "code":          code,
            "unit":          unit,
            "daily_results": merged_days,
        }

    return {
        "parameters":       result_a.get("parameters", []),
        "estimated_flow":   result_a.get("estimated_flow"),
        "confidence":       min(conf_a, conf_b),
        "analysis":         merged_analysis,
        "detailed_results": list(merged_detailed.values()),
        "explanation":      merged_explanation,
        "flow_results":     merged_flow,
    }


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
    # Defaults to gap_filling for backward compatibility when not provided.
    mode      = state.get("mode", "gap_filling")
    time_mode = state.get("time_mode", "date_range")
    segments  = state.get("segments") or [
        {"start": state["start_date"], "end": state["end_date"], "mode": mode}
    ]
    logger.info(f"[Agent {comid}] mode={mode} | time_mode={time_mode}")

    param_codes = state["current_params"]  # [[name, code], ...]
    logger.info(f"[Agent {comid}] Fetching external data (EPA, weather, USGS)...")

    env_task  = EnvironmentService.get_environmental_factors(
        comid, river.lat, river.lon,
        state["start_date"], state["end_date"],
        state["facility_types"],
    )
    # For prediction mode, future dates yield no USGS data.
    # Use the most recent 48h of observations as the baseline anchor instead,
    # so the LLM has concrete starting values to extrapolate from.
    from datetime import date as _date, timedelta as _td
    if mode == "prediction" and river.has_usgs_site:
        _obs_start = (_date.today() - _td(days=2)).isoformat()
        _obs_end   = _date.today().isoformat()
        logger.info(
            f"[Agent {comid}] Prediction mode: fetching baseline obs "
            f"{_obs_start} -> {_obs_end} instead of future dates"
        )
    else:
        _obs_start = state["start_date"]
        _obs_end   = state["end_date"]

    flow_task = (
        NWISService.get_flow_data(river.site_id, _obs_start, _obs_end)
        if river.has_usgs_site else asyncio.sleep(0, result=None)
    )
    obs_task = (
        NWISService.get_daily_averages(
            river.site_id, _obs_start, _obs_end, param_codes
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

    # Forecast is only meaningful for future dates; gap_filling skips this.
    forecast_data: Optional[Dict] = None
    if mode in ("prediction", "mixed") and river.lat and river.lon:
        # For mixed mode use segments[-1] (the prediction segment);
        # for pure prediction use the full state date range.
        pred_start = segments[-1]["start"] if mode == "mixed" else state["start_date"]
        pred_end   = segments[-1]["end"]   if mode == "mixed" else state["end_date"]
        try:
            forecast_data = await WeatherService.get_weather_forecast(
                river.lat, river.lon, pred_start, pred_end
            )
            n_entries = len(forecast_data.get("observations", []))
            logger.info(f"[Agent {comid}] Forecast fetched: {n_entries} entries ({pred_start} -> {pred_end})")
        except Exception as exc:
            logger.error(f"[Agent {comid}] Forecast fetch failed: {exc}")
            forecast_data = {"observations": [], "alerts": []}

    # Fetch recent 24h USGS observations as a prediction baseline.
    # Future-date queries return no data, so prediction mode needs a recent
    # anchor point so the LLM can extrapolate instead of reasoning from zero.
    # Only fetched for USGS-equipped nodes in prediction or mixed mode.
    recent_obs: Optional[Dict] = None
    if mode in ("prediction", "mixed") and river.has_usgs_site and river.site_id:
        from datetime import date as _date, timedelta as _td
        baseline_end   = _date.today().isoformat()
        baseline_start = (_date.today() - _td(days=2)).isoformat()
        try:
            recent_obs = await NWISService.get_daily_averages(
                river.site_id, baseline_start, baseline_end, param_codes
            )
            logger.info(
                f"[Agent {comid}] Prediction baseline fetched: "
                f"{baseline_start} -> {baseline_end}"
            )
        except Exception as exc:
            logger.warning(f"[Agent {comid}] Prediction baseline fetch failed: {exc}")

    # Only makes sense for gap_filling segments that have real USGS data.
    daily_summary: List[ParameterResult] = []
    if mode in ("gap_filling", "mixed") and obs_data:
        daily_summary = NWISService.compute_daily_summary(
            obs_data, settings.ANOMALY_THRESHOLD_PCT
        )
        total_anomalies = sum(
            len(day.anomalies)
            for pr in daily_summary
            for day in pr.daily_results
        )
        logger.info(
            f"[Agent {comid}] Daily summary: "
            f"{len(daily_summary)} params, {total_anomalies} anomalies"
        )


    # ── 5 / 6 / 7. Build prompt, call LLM, normalize output ──────────────
    # Shared blocks used by all prompt variants
    target_params_block = _format_target_params_block(param_codes)
    env_block           = _format_env_block(env_factors)
    history_block       = _format_history_block(history_context)

    if mode == "mixed":
        # ── Two-pass inference for mixed mode ─────────────────────────────
        # Pass A: gap_filling for the historical segment (segments[0])
        daily_summary_block_a = _format_daily_summary_block(daily_summary)
        seg_a = segments[0]

        if river.has_usgs_site:
            if state.get("resample_auto", True):
                interval_h = _auto_resample_hours(seg_a["start"], seg_a["end"])
            else:
                interval_h = int(state.get("resample_freq_h") or 12)
            sensor_block = _format_sensor_data_block(obs_data, param_codes, interval_h)
            flow_block   = _format_flow_block(flow_data, interval_h)
            msgs_a = HYDRO_USGS_PROMPT.format_messages(
                comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                start_date=seg_a["start"], end_date=seg_a["end"],
                task_description=state["task"], site_id=river.site_id,
                target_params_block=target_params_block,
                sensor_data_block=sensor_block, flow_block=flow_block,
                env_block=env_block, history_block=history_block,
                daily_summary_block=daily_summary_block_a,
            )
        else:
            upstream_block = _format_upstream_block(state.get("upstream_results", {}))
            msgs_a = HYDRO_INFER_PROMPT.format_messages(
                comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                start_date=seg_a["start"], end_date=seg_a["end"],
                task_description=state["task"],
                target_params_block=target_params_block,
                upstream_block=upstream_block, env_block=env_block,
                history_block=history_block,
                daily_summary_block=daily_summary_block_a,
            )

        prompt_a = f"{msgs_a[0].content}\n\n{msgs_a[1].content}"
        logger.info(
            f"[Agent {comid}] Mixed mode pass A (gap_filling): "
            f"{seg_a['start']} -> {seg_a['end']}"
        )
        result_a, llm_out_a = await _call_llm_and_normalize(
            prompt_a, param_codes, river, comid,
            state.get("selected_model") or settings.DEFAULT_LLM_MODEL,
        )

        # Pass B: prediction for the future segment (segments[1])
        forecast_block   = _format_forecast_block(forecast_data)
        upstream_block_b = _format_upstream_block(state.get("upstream_results", {}))
        seg_b = segments[1]

        if river.has_usgs_site:
            # Reuse obs_data/flow_data which already hold 48h baseline for prediction
            sensor_block_b = _format_sensor_data_block(obs_data, param_codes, interval_h)
            flow_block_b   = _format_flow_block(flow_data, interval_h)
            msgs_b = HYDRO_USGS_PREDICTION_PROMPT.format_messages(
                comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                start_date=seg_b["start"], end_date=seg_b["end"],
                task_description=state["task"], site_id=river.site_id,
                target_params_block=target_params_block,
                sensor_data_block=sensor_block_b, flow_block=flow_block_b,
                forecast_block=forecast_block, upstream_block=upstream_block_b,
                env_block=env_block, history_block=history_block,
            )
        else:
            msgs_b = HYDRO_INFER_PREDICTION_PROMPT.format_messages(
                comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                start_date=seg_b["start"], end_date=seg_b["end"],
                task_description=state["task"],
                target_params_block=target_params_block,
                forecast_block=forecast_block, upstream_block=upstream_block_b,
                env_block=env_block, history_block=history_block,
            )

        prompt_b = f"{msgs_b[0].content}\n\n{msgs_b[1].content}"
        logger.info(
            f"[Agent {comid}] Mixed mode pass B (prediction): "
            f"{seg_b['start']} -> {seg_b['end']}"
        )
        result_b, llm_out_b = await _call_llm_and_normalize(
            prompt_b, param_codes, river, comid,
            state.get("selected_model") or settings.DEFAULT_LLM_MODEL,
        )

        inference_result = _merge_inference_results(result_a, result_b, comid)
        llm_output  = f"=== PASS A (gap_filling) ===\n{llm_out_a}\n\n=== PASS B (prediction) ===\n{llm_out_b}"
        full_prompt = f"=== PASS A ===\n{prompt_a}\n\n=== PASS B ===\n{prompt_b}"

    else:
        # ── Single-pass inference for gap_filling or prediction ────────────
        daily_summary_block = (
            _format_daily_summary_block(daily_summary)
            if mode == "gap_filling"
            else "N/A - prediction mode (no historical anomaly detection)"
        )
        forecast_block = (
            _format_forecast_block(forecast_data)
            if mode == "prediction"
            else "N/A - gap-filling mode"
        )

        if river.has_usgs_site:
            if state.get("resample_auto", True):
                interval_h = _auto_resample_hours(state["start_date"], state["end_date"])
                logger.info(f"[Agent {comid}] Auto resample -> {interval_h}h")
            else:
                interval_h = int(state.get("resample_freq_h") or 12)
                logger.info(f"[Agent {comid}] Manual resample -> {interval_h}h")

            if mode == "prediction":
                upstream_block = _format_upstream_block(state.get("upstream_results", {}))
                logger.info(f"[Agent {comid}] Using USGS prediction prompt (site {river.site_id})")
                # Build sensor/flow blocks from the 48h baseline data
                sensor_block = _format_sensor_data_block(obs_data, param_codes, interval_h)
                flow_block   = _format_flow_block(flow_data, interval_h)
                messages = HYDRO_USGS_PREDICTION_PROMPT.format_messages(
                    comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                    start_date=state["start_date"], end_date=state["end_date"],
                    task_description=state["task"], site_id=river.site_id,
                    target_params_block=target_params_block,
                    sensor_data_block=sensor_block, flow_block=flow_block,
                    forecast_block=forecast_block, upstream_block=upstream_block,
                    env_block=env_block, history_block=history_block,
                )
            else:
                # gap_filling — existing logic, now includes daily_summary_block
                sensor_block = _format_sensor_data_block(obs_data, param_codes, interval_h)
                flow_block   = _format_flow_block(flow_data, interval_h)
                logger.info(f"[Agent {comid}] Using USGS gap-filling prompt (site {river.site_id})")
                messages = HYDRO_USGS_PROMPT.format_messages(
                    comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                    start_date=state["start_date"], end_date=state["end_date"],
                    task_description=state["task"], site_id=river.site_id,
                    target_params_block=target_params_block,
                    sensor_data_block=sensor_block, flow_block=flow_block,
                    env_block=env_block, history_block=history_block,
                    daily_summary_block=daily_summary_block,
                )
        else:
            upstream_results = state.get("upstream_results", {})
            upstream_block   = _format_upstream_block(upstream_results)

            if mode == "prediction":
                logger.info(f"[Agent {comid}] Using inference prediction prompt (no local USGS site)")
                messages = HYDRO_INFER_PREDICTION_PROMPT.format_messages(
                    comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                    start_date=state["start_date"], end_date=state["end_date"],
                    task_description=state["task"],
                    target_params_block=target_params_block,
                    forecast_block=forecast_block, upstream_block=upstream_block,
                    env_block=env_block, history_block=history_block,
                )
            else:
                # gap_filling — existing logic, now includes daily_summary_block
                logger.info(f"[Agent {comid}] Using inference gap-filling prompt (no local USGS site)")
                messages = HYDRO_INFER_PROMPT.format_messages(
                    comid=comid, FACILITY_INFO_STR=FACILITY_INFO_STR,
                    start_date=state["start_date"], end_date=state["end_date"],
                    task_description=state["task"],
                    target_params_block=target_params_block,
                    upstream_block=upstream_block, env_block=env_block,
                    history_block=history_block,
                    daily_summary_block=daily_summary_block,
                )

        full_prompt = f"{messages[0].content}\n\n{messages[1].content}"

        inference_result, llm_output = await _call_llm_and_normalize(
            full_prompt, param_codes, river, comid,
            state.get("selected_model") or settings.DEFAULT_LLM_MODEL,
        )
        if inference_result is None:
            inference_result = {
                "error":    "JSON parsing failed",
                "analysis": f"Raw output: {llm_output}",
            }

    # Store LLM I/O on river for Inspector display (used by all branches above)
    river.output         = inference_result
    river.prompt_used    = full_prompt
    river.llm_raw_output = llm_output

    # ── 8. Persist memory ─────────────────────────────────────────────────
    memory.setdefault("past_inferences", []).append({
        "timestamp":            datetime.now().isoformat(),
        "start_date":           state["start_date"],
        "end_date":             state["end_date"],
        "task":                 state["task"],
        "structured_inference": inference_result,
        "llm_result":           llm_output,
    })
    # Keep only the last 10 inferences to prevent unbounded file growth
    memory["past_inferences"] = memory["past_inferences"][-10:]
    await MemoryService.save_memory(file_path, memory)

    logger.info(f"[Agent {comid}] Inference complete")
    return {"global_data": {comid: river.model_dump()}}


