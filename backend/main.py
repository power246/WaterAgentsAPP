import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

from app.services.asker_service import AskerService
from app.agents.graph import create_hydrological_graph
from app.core.env_manager import EnvManager
from app.core.lifecycle import shutdown_all_resources
from app.models.river import RiverAgentData

# Import routers
from app.api.ws.agent_ws import router as ws_router
from app.api.settings_router import router as settings_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Server startup / shutdown lifecycle.

    Startup:  reset_memory_only() – clears ./data/memory but keeps ./.cache intact.
              This avoids the race condition where uvicorn --reload's file watcher
              scans .cache/ while it is being deleted and recreated.

    Shutdown: release HTTP client and DiskCache handles.
    """
    EnvManager.reset_memory_only()
    logger.info("Server startup complete.")
    yield
    await shutdown_all_resources()
    logger.info("HTTP client safely shut down.")


app = FastAPI(title="Water-Agent API", lifespan=lifespan)

# Allow all origins for development (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register WebSocket router: ws://host/ws/hydro-analysis
app.include_router(ws_router, prefix="/ws")

# Register settings REST router: GET /api/settings/defaults
app.include_router(settings_router, prefix="/api/settings")


# ── Request / Response schemas ─────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    query: str = Field(
        ...,
        json_schema_extra={
            "example": "Compare the pH at coordinates: -92.4141, 46.6823 from 2026-01-01 to 2026-01-15."
        }
    )
    model_name: Optional[str] = Field("llama3:8b", description="LLM model name")


class AnalyzeResponse(BaseModel):
    target_id: str
    summary: Optional[Any] = None
    all_agents_data: Dict[str, Any] = Field(
        ..., description="Full inference results for all river segments (used for map rendering)"
    )
    params_used: List[List[str]]
    time_range: Dict[str, str]


# ── Routes ─────────────────────────────────────────────────────────────────

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_water_system(request: AnalyzeRequest):
    """
    REST endpoint (non-streaming).
    Runs the full inference pipeline and returns when all agents finish.
    For real-time streaming, use the WebSocket endpoint at /ws/hydro-analysis.
    """
    logger.info(f"[REST] Received query: {request.query!r}")

    # Step 1: parse intent and build topology
    context = await AskerService.prepare_inference_context(
        user_query=request.query,
        selected_model=request.model_name
    )
    if not context:
        raise HTTPException(
            status_code=400,
            detail="Unable to parse query intent or coordinates are invalid."
        )

    # Step 2: build agent dict and LangGraph
    agents_dict = {
        cid: RiverAgentData(**data)
        for cid, data in context["global_data"].items()
    }
    target_id    = context["start_comid"]
    inlet_comids = context["inlet_comids"]

    graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)
    if not graph_app:
        raise HTTPException(status_code=500, detail="Computation graph construction failed.")

    # Step 3: run inference
    initial_state = {
        "task":           request.query,
        "current_params": context["current_params"],
        "facility_types": ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
        "start_date":     context["start_date"],
        "end_date":       context["end_date"],
        "global_data":    context["global_data"],
        "inlet_comids":   inlet_comids,
        "selected_model": request.model_name,
    }

    try:
        final_state   = await graph_app.ainvoke(initial_state)
        target_agent  = final_state["global_data"].get(target_id)

        return AnalyzeResponse(
            target_id=target_id,
            summary=target_agent.get("output") if target_agent else "No data",
            all_agents_data=final_state["global_data"],
            params_used=context["current_params"],
            time_range={"start": context["start_date"], "end": context["end_date"]},
        )

    except Exception as e:
        logger.error(f"[REST] Inference error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Inference engine error: {str(e)}")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "engine": "LangGraph"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["app"])