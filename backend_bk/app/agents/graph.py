import asyncio
import logging
from collections import deque
from functools import partial
from typing import Dict, List, AsyncGenerator, Any, Optional, Set

from langgraph.graph import StateGraph
from app.agents.state import WaterSystemState, AgentSubState
from app.agents.nodes import inference_node
from app.models.river import RiverAgentData
from app.services.asker_service import AskerService
from app.services.nldi_service import NLDIService

logger = logging.getLogger(__name__)


# ── Shared BFS helper ─────────────────────────────────────────────────────────

def _compute_active_nodes(
    agents_dict: Dict[str, RiverAgentData],
    entry_points: List[str],
) -> Set[str]:
    """
    BFS downstream from every USGS inlet to find all nodes reachable by data flow.
    Nodes NOT in the returned set are "excluded" — they exist in the topology but
    have no data path connecting them to a real measurement station.
    """
    active: Set[str] = set()
    queue = deque(entry_points)
    while queue:
        cid = queue.popleft()
        if cid in active:
            continue
        active.add(cid)
        for did in agents_dict[cid].downstream_ids:
            if did in agents_dict:
                queue.append(did)
    return active


# ── Graph builder ─────────────────────────────────────────────────────────────

def create_hydrological_graph(
    agents_dict:  Dict[str, RiverAgentData],
    entry_points: List[str],
):
    """
    Build and compile a LangGraph state machine from the river topology.
    Only nodes reachable by data flow (active_nodes) are added as graph nodes.
    """
    active_nodes = _compute_active_nodes(agents_dict, entry_points)
    logger.info(
        f"[Graph] BFS complete: {len(active_nodes)} active nodes, "
        f"inlets: {entry_points}"
    )

    workflow = StateGraph(WaterSystemState)

    # Node wrapper: translates WaterSystemState → AgentSubState for each agent
    async def node_wrapper(state: WaterSystemState, comid: str):
        logger.info(f"[Agent {comid}] Building inference prompt...")
        sub_state: AgentSubState = {
            "comid":           comid,
            "task":            state["task"],
            "current_params":  state["current_params"],
            "facility_types":  state["facility_types"],
            "start_date":      state["start_date"],
            "end_date":        state["end_date"],
            "selected_model":  state.get("selected_model"),
            "resample_auto":   state.get("resample_auto",   True),
            "resample_freq_h": state.get("resample_freq_h", 12),
            "mode":            state.get("mode",      "gap_filling"),
            "time_mode":       state.get("time_mode", "date_range"),
            "segments":        state.get("segments",  []),
            "upstream_results": {
                uid: state["global_data"][uid].get("output")
                for uid in agents_dict[comid].upstream_ids
                if uid in state["global_data"]
            },
        }
        return await inference_node(sub_state)

    for cid in active_nodes:
        workflow.add_node(cid, partial(node_wrapper, comid=cid))

    for cid in active_nodes:
        for up_id in agents_dict[cid].upstream_ids:
            if up_id in active_nodes:
                workflow.add_edge(up_id, cid)
                logger.debug(f"[Graph] Edge: {up_id} -> {cid}")

    for ep in entry_points:
        workflow.set_entry_point(ep)

    logger.info(
        f"[Graph] Compiled: {len(active_nodes)} active nodes, "
        f"inlets: {entry_points}"
    )
    return workflow.compile()


# ── WebSocket streaming runner ────────────────────────────────────────────────

async def run_hydro_graph(
    params:     Dict[str, Any],
    stop_event: Optional[asyncio.Event] = None,
) -> AsyncGenerator[Dict, None]:
    """
    Stream inference events over WebSocket.

    Event types emitted:
        topology    — full node/edge map including is_excluded flag and coordinates
        agent_start — a node has begun LLM inference
        agent_done  — a node finished; carries result + updated coordinates
        done        — all nodes finished
        error       — something went wrong
    """
    user_query         = params.get("query",              "")
    model_name         = params.get("model_name",         "llama3:8b")
    max_topology_depth = params.get("max_topology_depth", None)
    resample_auto      = params.get("resample_auto",      True)
    resample_freq_h    = int(params.get("resample_freq_h") or 12)

    logger.info(
        f"[WS] Query: {user_query!r} | Model: {model_name} | "
        f"Depth: {max_topology_depth} | "
        f"Resample: {'auto' if resample_auto else f'{resample_freq_h}h'}"
    )

    # ── Step 1: Parse intent and build topology ───────────────────────────────
    try:
        context = await AskerService.prepare_inference_context(
            user_query=user_query,
            selected_model=model_name,
            max_topology_depth=max_topology_depth,
            start_date=params.get("start_date"),   # frontend-provided dates take priority
            end_date=params.get("end_date"),
        )
    except Exception as exc:
        logger.error(f"[WS] AskerService failed: {exc}")
        yield {"type": "error", "message": f"Intent parsing failed: {str(exc)}"}
        return

    if not context:
        yield {"type": "error", "message": "Could not parse query intent or coordinates."}
        return

    agents_dict  = {
        cid: RiverAgentData(**data)
        for cid, data in context["global_data"].items()
    }
    target_id    = context["start_comid"]
    inlet_comids = context["inlet_comids"]

    # ── Step 2: Determine active vs excluded nodes ────────────────────────────
    # Do this BEFORE sending the topology event so we can set is_excluded correctly.
    active_nodes = _compute_active_nodes(agents_dict, inlet_comids)
    excluded_nodes = set(agents_dict.keys()) - active_nodes
    logger.info(
        f"[WS] Active: {len(active_nodes)}, Excluded: {len(excluded_nodes)}"
    )

    # ── Step 3: Pre-fetch coordinates for ALL nodes concurrently ─────────────
    # Coordinates are normally fetched lazily inside inference_node.  We fetch
    # them here upfront so the map can display ALL nodes immediately when the
    # topology event arrives — before any inference has started.
    # NLDIService uses a disk cache so these calls are fast on repeated runs.
    async def _fetch_coords(cid: str):
        """Return (cid, lat, lon) — falls back to (cid, 0.0, 0.0) on error."""
        river = agents_dict[cid]
        # If coordinates are already populated (e.g. from memory cache), use them.
        if river.lat and river.lon:
            return cid, river.lat, river.lon
        try:
            coords = await NLDIService.get_coords_by_comid(cid)
            if coords:
                return cid, coords["lat"], coords["lon"]
        except Exception as exc:
            logger.warning(f"[WS] Coord fetch failed for {cid}: {exc}")
        return cid, 0.0, 0.0

    coord_results = await asyncio.gather(*[_fetch_coords(cid) for cid in agents_dict])
    coord_map = {cid: (lat, lon) for cid, lat, lon in coord_results}

    # ── Step 4: Build and send topology event ────────────────────────────────
    topology_nodes = []
    topology_edges = []

    for cid, river in agents_dict.items():
        lat, lon = coord_map.get(cid, (river.lat, river.lon))
        topology_nodes.append({
            "comid":        cid,
            "lat":          lat,
            "lon":          lon,
            "has_usgs_site": river.has_usgs_site,
            "site_id":      river.site_id,
            "is_target":    cid == target_id,
            "is_inlet":     cid in inlet_comids,
            # is_excluded: node exists in topology but has no USGS data path
            "is_excluded":  cid in excluded_nodes,
        })
        for up_id in river.upstream_ids:
            topology_edges.append({
                "from":        up_id,
                "to":          cid,
                # Mark edge as excluded if either endpoint is excluded
                "is_excluded": (up_id in excluded_nodes) or (cid in excluded_nodes),
            })

    yield {
        "type":        "topology",
        "nodes":       topology_nodes,
        "edges":       topology_edges,
        "target_id":   target_id,
        "inlet_comids": inlet_comids,
        "params_used": context["current_params"],
        "time_range": {
            "start": context["start_date"],
            "end":   context["end_date"],
        },
        # Pass segments so the frontend can accurately classify historical vs
        # prediction days without relying on a potentially timezone-skewed date comparison.
        "segments": context.get("segments", []),
    }
    logger.info(
        f"[WS] Topology sent: {len(topology_nodes)} nodes "
        f"({len(active_nodes)} active, {len(excluded_nodes)} excluded), "
        f"{len(topology_edges)} edges"
    )

    # ── Step 5: Build LangGraph (active nodes only) ───────────────────────────
    try:
        graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)
    except Exception as exc:
        logger.error(f"[WS] Graph construction failed: {exc}")
        yield {"type": "error", "message": f"Graph construction failed: {str(exc)}"}
        return

    # ── Step 6: Run inference and stream events ───────────────────────────────
    initial_state = {
        "task":            user_query,
        "current_params":  context["current_params"],
        "facility_types":  ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
        "start_date":      context["start_date"],
        "end_date":        context["end_date"],
        "global_data":     context["global_data"],
        "inlet_comids":    inlet_comids,
        "selected_model":  model_name,
        "resample_auto":   resample_auto,
        "resample_freq_h": resample_freq_h,
        "mode":            context.get("mode",      "gap_filling"),
        "time_mode":       context.get("time_mode", "date_range"),
        "segments":        context.get("segments",  []),
    }

    try:
        async for event in graph_app.astream_events(initial_state, version="v2"):
            event_name = event.get("event", "")
            node_name  = event.get("name",  "")

            if event_name == "on_chain_start" and node_name in agents_dict:
                logger.info(f"[WS] Agent [{node_name}] started")
                yield {"type": "agent_start", "comid": node_name}

            elif event_name == "on_chain_end" and node_name in agents_dict:
                output_data  = event.get("data", {}).get("output", {})
                agent_result = output_data.get("global_data", {}).get(node_name, {})
                logger.info(f"[WS] Agent [{node_name}] done")
                # Flatten anomalies from all parameters' daily_results for the
                # AnomalyPanel (Sprint 10). Each anomaly is tagged with its comid.
                output_data_inner = agent_result.get("output") or {}
                flat_anomalies = [
                    {**anomaly, "comid": node_name}
                    for param in (output_data_inner.get("detailed_results") or [])
                    for day   in (param.get("daily_results") or [])
                    for anomaly in (day.get("anomalies") or [])
                ]
                yield {
                    "type":            "agent_done",
                    "comid":           node_name,
                    "result":          output_data_inner,
                    "lat":             agent_result.get("lat"),
                    "lon":             agent_result.get("lon"),
                    "has_usgs_site":   agent_result.get("has_usgs_site"),
                    "site_id":         agent_result.get("site_id"),
                    "prompt_used":     agent_result.get("prompt_used"),
                    "llm_raw_output":  agent_result.get("llm_raw_output"),
                    "explanation":     output_data_inner.get("explanation"),
                    "detailed_results": output_data_inner.get("detailed_results"),
                    "flow_results":    output_data_inner.get("flow_results"),
                    "anomalies":       flat_anomalies,
                }

                # Plan-A stop check: after each agent completes, see if the
                # user pressed Stop.  Current agent has already finished cleanly
                # (and written its memory); we simply stop dispatching new ones.
                if stop_event and stop_event.is_set():
                    logger.info(f"[WS] Stop signal detected after agent [{node_name}] — interrupting pipeline")
                    yield {
                        "type":    "interrupted",
                        "message": "Inference stopped by user. "
                                   "Completed agents are shown on the map.",
                    }
                    return

    except Exception as exc:
        logger.error(f"[WS] Graph run error: {exc}", exc_info=True)
        yield {"type": "error", "message": f"Inference engine error: {str(exc)}"}
        return

    # ── Step 7: Done ──────────────────────────────────────────────────────────
    logger.info(f"[WS] All inference complete. Target: {target_id}")
    yield {
        "type":      "done",
        "target_id": target_id,
        "message":   "All agent inference complete.",
    }