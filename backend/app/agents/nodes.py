import json
import re
import asyncio
import logging
from datetime import datetime

from app.agents.state import AgentSubState
from app.services.nldi_service import NLDIService
from app.services.nwis_service import NWISService
from app.services.environment_service import EnvironmentService
from app.services.memory_service import MemoryService
from app.services.llm_service import LLMService
from app.core.config import settings
from app.models.river import RiverAgentData, HydrologyInference
from app.services.river_manager import RiverManager
from app.agents.prompts import HYDRO_AGENT_PROMPT, FACILITY_INFO_STR

logger = logging.getLogger(__name__)


async def inference_node(state: AgentSubState):
    """
    Inference node for a single river segment (COMID).
    Runs in parallel with sibling nodes that share no upstream dependency.
    """
    comid = state["comid"]
    logger.info(f"[Agent {comid}] Task started")

    # Runtime settings forwarded from frontend (fall back to server defaults)
    provider      = state.get("provider")      or "ollama"
    api_key       = state.get("api_key")       or None
    resample_freq = state.get("resample_freq") or settings.WATER_DATA_RESAMPLE_FREQ
    weather_freq  = state.get("weather_freq")  or settings.WEATHER_FREQ

    # Initialize river data model
    river = RiverAgentData(comid=comid)

    # Load agent memory (last 2 inference records used as context)
    file_path = f"./data/memory/{comid}.json"
    memory = await MemoryService.load_memory(file_path)
    history_context = memory.get("past_inferences", [])[-2:]
    river.memory = memory

    # Resolve geographic metadata: memory cache first, then network
    cached_metadata = memory.get("static_metadata", {})
    if cached_metadata.get("lat") and cached_metadata.get("site_id"):
        logger.info(f"[Agent {comid}] Metadata from memory cache")
        river.lat           = cached_metadata["lat"]
        river.lon           = cached_metadata["lon"]
        river.site_id       = cached_metadata["site_id"]
        river.has_usgs_site = True
    else:
        logger.info(f"[Agent {comid}] Fetching metadata via RiverManager")
        metadata = await RiverManager.get_river_metadata(comid)
        river.lat           = metadata["lat"]
        river.lon           = metadata["lon"]
        river.site_id       = metadata["site_id"]
        river.has_usgs_site = metadata["has_usgs_site"]
        memory["static_metadata"] = metadata

    # Concurrently fetch EPA facilities + USGS flow + USGS observations
    param_codes = state["current_params"]

    env_task = EnvironmentService.get_environmental_factors(
        comid, river.lat, river.lon,
        state["start_date"], state["end_date"],
        state["facility_types"],
    )

    if river.has_usgs_site:
        flow_task = NWISService.get_flow_data(
            river.site_id, state["start_date"], state["end_date"],
            resample_freq=resample_freq,
        )
        obs_task = NWISService.get_daily_averages(
            river.site_id, state["start_date"], state["end_date"],
            param_codes, resample_freq=resample_freq,
        )
    else:
        flow_task = asyncio.sleep(0, result=None)
        obs_task  = asyncio.sleep(0, result=None)

    try:
        env_factors, flow_context, current_obs = await asyncio.gather(
            env_task, flow_task, obs_task
        )
    except Exception as e:
        logger.error(f"[Agent {comid}] Data fetch failed: {e}")
        env_factors, flow_context, current_obs = {}, None, None

    # Build the data block injected into the prompt
    if current_obs and current_obs.get("averages"):
        data_block = (
            f"[REAL-TIME USGS OBSERVATIONS]: {json.dumps(current_obs, ensure_ascii=False)}\n"
            f"[RULE]: If a parameter has no OBSERVATIONS entry, set estimate_value to null.\n"
            f"TASK: Integrate real-time observations with the environmental background."
        )
    else:
        upstream_info = state.get("upstream_results", {})

        if upstream_info:
            # Format each upstream segment as a clearly labelled section so the
            # LLM cannot overlook any of them.  Extracting structured parameter
            # rows (instead of raw JSON) also reduces noise and ambiguity.
            upstream_count = len(upstream_info)
            upstream_sections = []

            for idx, (uid, output) in enumerate(upstream_info.items(), start=1):
                lines = [f"[Upstream {idx}/{upstream_count}] COMID {uid}"]

                if output is None:
                    lines.append("  Status: inference not yet available or failed.")
                else:
                    # Estimated flow
                    flow_val = output.get("estimated_flow")
                    lines.append(
                        f"  Estimated flow : {flow_val} cfs" if flow_val is not None
                        else "  Estimated flow : unknown"
                    )

                    # Confidence
                    conf = output.get("confidence")
                    if conf is not None:
                        lines.append(f"  Confidence     : {conf:.2f}")

                    # Parameter estimates — one line each for clarity
                    params_out = output.get("parameters", [])
                    if params_out:
                        lines.append("  Parameter estimates:")
                        for p in params_out:
                            val = p.get("estimate_value")
                            unit = p.get("unit") or ""
                            val_str = (
                                f"{val} {unit}".strip() if val is not None
                                else "null (no data)"
                            )
                            lines.append(
                                f"    * {p.get('name', '?')} ({p.get('code', '?')}): {val_str}"
                            )
                    else:
                        lines.append("  Parameter estimates: none available")

                    # Include upstream analysis summary as reasoning context.
                    # Truncated to 300 chars to avoid context bloat.
                    analysis_text = output.get("analysis", "")
                    if analysis_text:
                        '''
                        truncated = (
                            analysis_text[:300] + "..."
                            if len(analysis_text) > 300 else analysis_text
                        )
                        '''
                        lines.append(f"  Reasoning: {analysis_text}")

                upstream_sections.append("\n".join(lines))

            formatted_upstream = "\n\n".join(upstream_sections)
            data_block = (
                f"[UPSTREAM INFERRED DATA - {upstream_count} upstream segment(s)]:\n"
                f"{formatted_upstream}\n\n"
                f"TASK: No local sensor. Reference ALL {upstream_count} upstream(s) above. "
                f"Apply mass balance. Do not fabricate null parameters."
            )
        else:
            # No upstream data at all — true headwater or isolated segment
            data_block = (
                f"[UPSTREAM INFERRED DATA]: None - this is a headwater segment with no upstream agents.\n"
                f"[RULE]: Do not fabricate parameter values. Set estimate_value to null if no basis exists.\n"
                f"TASK: No local sensor data and no upstream data. Estimate only from environmental background."
            )

    # Format prompt
    messages = HYDRO_AGENT_PROMPT.format_messages(
        comid=comid,
        FACILITY_INFO_STR=FACILITY_INFO_STR,
        start_date=state["start_date"],
        end_date=state["end_date"],
        task_description=state["task"],
        params=json.dumps(state["current_params"], ensure_ascii=False),
        history_context=json.dumps(history_context, ensure_ascii=False),
        flow_context=json.dumps(flow_context) if flow_context else "No local gauge.",
        env_factors=json.dumps(env_factors, ensure_ascii=False),
        data_block=data_block,
    )

    full_prompt = f"{messages[0].content}\n\n{messages[1].content}"
    river.prompt_used = full_prompt  # stored for Inspector display

    # Call LLM with runtime-selected provider/model/key
    llm_output = await LLMService.call_llm(
        prompt=full_prompt,
        model_name=state.get("selected_model") or settings.DEFAULT_LLM_MODEL,
        provider=provider,
        api_key=api_key,
    )
    river.llm_raw_output = str(llm_output)

    # Parse and validate JSON output
    inference_result = None
    try:
        match = re.search(r"(\{.*\})", llm_output, re.DOTALL)
        json_str = match.group(1) if match else llm_output
        parsed_data = HydrologyInference.model_validate_json(json_str)
        inference_result = parsed_data.model_dump()
        river.output = inference_result
    except Exception as e:
        preview = str(llm_output)[:200]
        logger.error(
            f"[Agent {comid}] JSON validation failed: {e}\n"
            f"Output preview: {preview}..."
        )
        inference_result = {"error": "JSON parsing failed", "analysis": f"Raw output: {llm_output}"}
        river.output = inference_result

    # Persist memory
    memory.setdefault("past_inferences", []).append({
        "timestamp":            datetime.now().isoformat(),
        "task":                 state["task"],
        "structured_inference": inference_result,
        "llm_result":           llm_output,
    })
    await MemoryService.save_memory(file_path, memory)

    logger.info(f"[Agent {comid}] Inference complete")
    return {"global_data": {comid: river.model_dump()}}