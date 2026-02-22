import logging
from collections import deque
from functools import partial
from typing import Dict, List, AsyncGenerator, Any

from langgraph.graph import StateGraph
from app.agents.state import WaterSystemState, AgentSubState
from app.agents.nodes import inference_node
from app.models.river import RiverAgentData

logger = logging.getLogger(__name__)


def create_hydrological_graph(agents_dict: Dict[str, RiverAgentData], entry_points: List[str]):
    """
    Build a LangGraph state machine from the topology dictionary.
    Only keeps the active inference chain driven by USGS station entry points.

    Args:
        agents_dict:  all river agent nodes, keyed by comid
        entry_points: comids that have real USGS measurement data

    Returns:
        compiled LangGraph app
    """
    # ── 1. BFS: find all nodes reachable from entry points ──────────────────
    active_nodes = set()
    trace_queue = deque(entry_points)

    while trace_queue:
        curr_id = trace_queue.popleft()
        if curr_id not in active_nodes:
            active_nodes.add(curr_id)
            for did in agents_dict[curr_id].downstream_ids:
                if did in agents_dict:
                    trace_queue.append(did)

    logger.info(f"BFS complete: {len(active_nodes)} active nodes, entries: {entry_points}")

    # ── 2. Initialize LangGraph ──────────────────────────────────────────────
    workflow = StateGraph(WaterSystemState)

    # ── 3. Node wrapper: converts WaterSystemState -> AgentSubState ──────────
    async def node_wrapper(state: WaterSystemState, comid: str):
        logger.info(f"Agent [{comid}] building inference prompt...")
        sub_state: AgentSubState = {
            "comid":          comid,
            "task":           state["task"],
            "current_params": state["current_params"],
            "facility_types": state["facility_types"],
            "start_date":     state["start_date"],
            "end_date":       state["end_date"],
            "selected_model": state.get("selected_model"),
            "provider":       state.get("provider", "ollama"),
            "api_key":        state.get("api_key"),
            "resample_freq":  state.get("resample_freq", "6h"),
            "weather_freq":   state.get("weather_freq", "12h"),
            "upstream_results": {
                uid: state["global_data"][uid].get("output")
                for uid in agents_dict[comid].upstream_ids
                if uid in state["global_data"]
            }
        }
        return await inference_node(sub_state)

    # ── 4. Register active nodes ─────────────────────────────────────────────
    for cid in active_nodes:
        workflow.add_node(cid, partial(node_wrapper, comid=cid))

    # ── 5. Add dependency edges ──────────────────────────────────────────────
    for cid in active_nodes:
        for up_id in agents_dict[cid].upstream_ids:
            if up_id in active_nodes:
                workflow.add_edge(up_id, cid)
                logger.debug(f"  edge: {up_id} -> {cid}")

    # ── 6. Set entry points ──────────────────────────────────────────────────
    for ep in entry_points:
        workflow.set_entry_point(ep)

    logger.info(f"Graph compiled: {len(active_nodes)} active nodes")
    return workflow.compile()


async def run_hydro_graph(params: Dict[str, Any]) -> AsyncGenerator[Dict, None]:
    """
    Streaming version of the inference pipeline for WebSocket delivery.

    Yields progress events at every major step so the client receives
    regular updates and the connection is never silently idle for long.

    Event types:
        "preparing"   : a long setup step has started (sent BEFORE the await)
        "topology"    : topology ready, contains nodes + edges for map rendering
        "agent_start" : an Agent node has started inference
        "agent_done"  : an Agent node finished (result + full LLM I/O included)
        "done"        : all agents finished successfully
        "error"       : something went wrong
        "ping"        : keepalive sent by agent_ws.py heartbeat (ignored by frontend)
    """
    from app.services.asker_service import AskerService
    from app.services.nldi_service import NLDIService
    from app.services.topology_service import TopologyService
    from app.core.config import settings

    user_query         = params.get("query", "")
    model_name         = params.get("model_name", "llama3:8b")
    provider           = params.get("provider", "ollama")
    api_key            = params.get("api_key") or None
    facility_types     = params.get("facility_types") or settings.FACILITY_TYPES
    max_topology_depth = int(params.get("max_topology_depth", settings.MAX_TOPOLOGY_DEPTH))
    resample_auto      = bool(params.get("resample_auto", True))
    resample_freq_h    = int(params.get("resample_freq_h", 6))
    weather_freq_h     = int(params.get("weather_freq_h", 12))
    # Note: if resample_auto is True the freq values will be overwritten below
    # after start_date/end_date are known (line ~210). The manual values are
    # kept as a fallback in case auto computation cannot run.
    resample_freq      = f"{resample_freq_h}h"
    weather_freq       = f"{weather_freq_h}h"

    logger.info(
        f"[WS] Query: {user_query!r} | provider: {provider}/{model_name} | "
        f"depth: {max_topology_depth} | resample: {resample_freq} | weather: {weather_freq}"
    )

    # ── Step 1a: LLM intent parsing ──────────────────────────────────────────
    # This calls the LLM to extract location, parameters, and time range.
    # It is the first major latency spike (~10-60s depending on model).
    yield {
        "type": "preparing",
        "step": "intent_parsing",
        "message": "Parsing query with LLM..."
    }
    try:
        data_needs = await AskerService.parse_user_intent(user_query, model_name=model_name)
    except Exception as e:
        logger.error(f"[WS] Intent parsing failed: {e}")
        yield {"type": "error", "message": f"Intent parsing failed: {str(e)}"}
        return

    if not data_needs or not data_needs.get("location"):
        yield {"type": "error", "message": "Could not parse a valid location from the query."}
        return

    # ── Step 1b: Geocoding / coordinate resolution ───────────────────────────
    yield {
        "type": "preparing",
        "step": "geocoding",
        "message": "Resolving coordinates..."
    }
    location = data_needs["location"]
    lat, lon = None, None
    try:
        if isinstance(location, list) and len(location) == 2:
            lat, lon = float(location[0]), float(location[1])
        elif isinstance(location, str):
            coords = await AskerService.geocode_location(location)
            if coords:
                lat, lon = coords
    except Exception as e:
        logger.error(f"[WS] Geocoding failed: {e}")
        yield {"type": "error", "message": f"Geocoding failed: {str(e)}"}
        return

    if lat is None or lon is None:
        yield {"type": "error", "message": f"Could not resolve coordinates for: {location}"}
        return

    # ── Step 1c: NLDI COMID lookup ───────────────────────────────────────────
    yield {
        "type": "preparing",
        "step": "comid_lookup",
        "message": f"Finding river segment at ({lat:.4f}, {lon:.4f})..."
    }
    try:
        start_comid = await NLDIService.get_main_comid(lat, lon)
    except Exception as e:
        logger.error(f"[WS] COMID lookup failed: {e}")
        yield {"type": "error", "message": f"COMID lookup failed: {str(e)}"}
        return

    if not start_comid:
        yield {"type": "error", "message": f"No river segment found near ({lat}, {lon})."}
        return
    logger.info(f"[WS] COMID resolved: {start_comid}")

    # ── Step 1d: Semantic parameter mapping ──────────────────────────────────
    yield {
        "type": "preparing",
        "step": "param_mapping",
        "message": "Mapping parameters to USGS codes..."
    }
    try:
        char_names   = data_needs.get("characteristic_names", [])
        final_params = []
        for name in char_names:
            matches = await AskerService.get_parm_cd_from_name(name, top_n=2)
            final_params.extend(matches)
        # Deduplicate by parameter code
        seen, unique_params = set(), []
        for p in final_params:
            if p[1] not in seen:
                unique_params.append(p)
                seen.add(p[1])
    except Exception as e:
        logger.error(f"[WS] Parameter mapping failed: {e}")
        yield {"type": "error", "message": f"Parameter mapping failed: {str(e)}"}
        return

    time_range = data_needs.get("time_range", {})
    start_date = time_range.get("start", "2026-02-01")
    end_date   = time_range.get("end",   "2026-02-10")

    # ── Auto-select resample frequency based on time span ────────────────────
    # Overrides manual values when resample_auto is True.
    if resample_auto:
        try:
            from datetime import date as _date
            _d0 = _date.fromisoformat(start_date)
            _d1 = _date.fromisoformat(end_date)
            span_days = (_d1 - _d0).days
            # Rules ordered from shortest to longest span — first match wins.
            _RULES = [
                (7,    6,  12),   # <= 7 days  : USGS 6h  / weather 12h
                (14,  12,  24),   # <= 14 days : USGS 12h / weather 24h
                (30,  24,  48),   # <= 30 days : USGS 24h / weather 48h
                (90,  48,  72),   # <= 90 days : USGS 48h / weather 72h
                (9999, 72, 168),  # > 90 days  : USGS 72h / weather 168h
            ]
            for max_d, r_h, w_h in _RULES:
                if span_days <= max_d:
                    resample_freq = f"{r_h}h"
                    weather_freq  = f"{w_h}h"
                    break
            logger.info(
                f"[WS] Auto-freq: span={span_days}d -> "                f"resample={resample_freq}, weather={weather_freq}"
            )
        except Exception as _e:
            logger.warning(f"[WS] Auto-freq fallback (parse error: {_e})")

    # ── Step 1e: Topology building (most time-consuming step) ────────────────
    # TopologyService does BFS upstream via NLDI, checking each COMID for
    # an associated USGS station. Each hop is a network request.
    yield {
        "type": "preparing",
        "step": "topology",
        "message": "Building upstream topology (this may take 1-2 minutes)..."
    }
    try:
        agents_dict_raw = await TopologyService.build_topology(
            start_comid=start_comid,
            max_depth=max_topology_depth
        )
    except Exception as e:
        logger.error(f"[WS] Topology build failed: {e}")
        yield {"type": "error", "message": f"Topology build failed: {str(e)}"}
        return

    # Detect inlet nodes (those with a real USGS station)
    inlet_comids = [cid for cid, r in agents_dict_raw.items() if r.has_usgs_site]
    if not inlet_comids:
        inlet_comids = [str(start_comid)]
        logger.info(f"[WS] No upstream stations found; using target {start_comid} as isolated node")

    target_id   = str(start_comid)
    agents_dict = agents_dict_raw  # Dict[str, RiverAgentData]

    # ── Step 1f: Pre-fetch coordinates for all nodes ──────────────────────────
    # Coordinates are normally fetched inside inference_node, which means the
    # topology event would carry lat:0/lon:0 for every node and MapView would
    # filter them all out (causing invisible nodes and no pulse animation).
    # We pre-fetch them here so the topology event contains real positions.
    # Most will be cache hits (NLDIService uses DiskCache) so this is fast.
    yield {
        "type":    "preparing",
        "step":    "coords",
        "message": f"Pre-fetching coordinates for {len(agents_dict)} nodes..."
    }

    from app.services.memory_service import MemoryService
    import asyncio as _asyncio

    async def _get_coords(cid: str, river) -> tuple:
        """Return (cid, lat, lon): try memory cache → NLDIService → (0,0)."""
        if river.lat and river.lon:
            return cid, river.lat, river.lon
        mem = await MemoryService.load_memory(f"./data/memory/{cid}.json")
        cached = mem.get("static_metadata", {})
        if cached.get("lat") and cached.get("lon"):
            return cid, cached["lat"], cached["lon"]
        coords = await NLDIService.get_coords_by_comid(cid)
        if coords:
            return cid, coords["lat"], coords["lon"]
        return cid, 0.0, 0.0

    coord_results = await _asyncio.gather(
        *[_get_coords(cid, river) for cid, river in agents_dict.items()]
    )
    for cid, lat, lon in coord_results:
        agents_dict[cid].lat = lat
        agents_dict[cid].lon = lon

    global_data = {cid: river.model_dump() for cid, river in agents_dict.items()}
    logger.info(f"[WS] Coordinates ready for {len(agents_dict)} nodes")

    # ── Compute active nodes via BFS (same logic as create_hydrological_graph) ─
    # Nodes that are NOT reachable from any inlet are excluded from inference.
    # We mark them with is_excluded=True so the frontend can render them
    # differently (dimmed / different color) and we exclude them from counts.
    from collections import deque as _deque
    active_nodes: set = set()
    _q = _deque(inlet_comids)
    while _q:
        _curr = _q.popleft()
        if _curr not in active_nodes:
            active_nodes.add(_curr)
            for _did in agents_dict[_curr].downstream_ids:
                if _did in agents_dict:
                    _q.append(_did)

    active_edges = set()  # "from->to" pairs that connect two active nodes
    for cid in active_nodes:
        for up_id in agents_dict[cid].upstream_ids:
            if up_id in active_nodes:
                active_edges.add((up_id, cid))

    logger.info(
        f"[WS] Active inference chain: {len(active_nodes)} nodes "
        f"(excluded: {len(agents_dict) - len(active_nodes)})"
    )

    # ── Step 2: Push topology to frontend ────────────────────────────────────
    topology_nodes = []
    topology_edges = []
    for cid, river in agents_dict.items():
        topology_nodes.append({
            "comid":         cid,
            "lat":           river.lat,
            "lon":           river.lon,
            "has_usgs_site": river.has_usgs_site,
            "site_id":       river.site_id,
            "is_target":     cid == target_id,
            "is_inlet":      cid in inlet_comids,
            # Nodes not reachable from any inlet are shown dimmed on the map
            "is_excluded":   cid not in active_nodes,
        })
        for up_id in river.upstream_ids:
            topology_edges.append({
                "from":        up_id,
                "to":          cid,
                # Edges between two excluded nodes (or crossing into excluded)
                # are shown dimmed; only fully-active edges are solid
                "is_excluded": (up_id, cid) not in active_edges,
            })

    # Count and log only active nodes / edges for the user-facing message
    active_node_count = len(active_nodes)
    active_edge_count = len(active_edges)

    yield {
        "type":              "topology",
        "nodes":             topology_nodes,
        "edges":             topology_edges,
        "target_id":         target_id,
        "inlet_comids":      inlet_comids,
        "params_used":       unique_params,
        "time_range":        {"start": start_date, "end": end_date},
        # Pass active counts separately so the frontend log shows the right numbers
        "active_node_count": active_node_count,
        "active_edge_count": active_edge_count,
    }
    logger.info(
        f"[WS] Topology pushed: {active_node_count} active nodes "
        f"+ {len(agents_dict) - active_node_count} excluded, "
        f"{active_edge_count} active edges"
    )

    # ── Step 3: Build LangGraph ───────────────────────────────────────────────
    yield {
        "type":    "preparing",
        "step":    "graph_build",
        "message": f"Building inference graph ({len(inlet_comids)} entry points)..."
    }
    try:
        graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)
    except Exception as e:
        logger.error(f"[WS] Graph build failed: {e}")
        yield {"type": "error", "message": f"Graph build failed: {str(e)}"}
        return

    # ── Step 4: Stream inference events ──────────────────────────────────────
    initial_state = {
        "task":           user_query,
        "current_params": unique_params,
        "facility_types": facility_types,
        "start_date":     start_date,
        "end_date":       end_date,
        "global_data":    global_data,
        "inlet_comids":   inlet_comids,
        "selected_model": model_name,
        "provider":       provider,
        "api_key":        api_key,
        "resample_freq":  resample_freq,
        "weather_freq":   weather_freq,
    }

    try:
        async for event in graph_app.astream_events(initial_state, version="v2"):
            event_name = event.get("event", "")
            node_name  = event.get("name", "")

            # Agent started
            if event_name == "on_chain_start" and node_name in agents_dict:
                logger.info(f"[WS] Agent [{node_name}] started")
                yield {"type": "agent_start", "comid": node_name}

            # Agent finished
            elif event_name == "on_chain_end" and node_name in agents_dict:
                output_data  = event.get("data", {}).get("output", {})
                agent_result = output_data.get("global_data", {}).get(node_name, {})
                logger.info(f"[WS] Agent [{node_name}] done")
                yield {
                    "type":           "agent_done",
                    "comid":          node_name,
                    "result":         agent_result.get("output"),
                    "lat":            agent_result.get("lat"),
                    "lon":            agent_result.get("lon"),
                    "has_usgs_site":  agent_result.get("has_usgs_site"),
                    "prompt_used":    agent_result.get("prompt_used"),
                    "llm_raw_output": agent_result.get("llm_raw_output"),
                }

    except Exception as e:
        logger.error(f"[WS] Graph run error: {e}", exc_info=True)
        yield {"type": "error", "message": f"Inference engine error: {str(e)}"}
        return

    # ── Step 5: Done ──────────────────────────────────────────────────────────
    logger.info(f"[WS] All agents done. Target: {target_id}")
    yield {"type": "done", "target_id": target_id, "message": "All agents finished"}