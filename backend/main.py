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
from app.api.keys_router import router as keys_router
from app.api.settings_router import router as settings_router
from app.api.ws.agent_ws import router as ws_router

# Configuration management
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: clear agent memory (safe with --reload) and flush all
    # HTTP caches so the new run starts with fresh data.
    # reset_memory_only() deletes ./data/memory but does NOT touch ./.cache
    # directories (which uvicorn's reloader holds open).
    # reset_cache_contents() uses cache.clear() instead of rmtree, so it works
    # even when the reloader process has the cache directories open.
    EnvManager.reset_memory_only()
    EnvManager.reset_cache_contents()
    yield
    # When closed: release the connection
    await shutdown_all_resources()
    logger.info("The HTTP Client has been safely shut down")

app = FastAPI(title="Water-Agent API", lifespan=lifespan)

# Allow cross-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(keys_router,      prefix="/api/keys")
app.include_router(settings_router,  prefix="/api/settings")
app.include_router(ws_router,        prefix="/ws")


# Schemas
class AnalyzeRequest(BaseModel):
    query: str = Field(
        ...,
        json_schema_extra={
            "example": "Compare the pH at coordinates: -92.4141, 46.6823"
        }
    )
    model_name: Optional[str] = Field("llama3:8b", description="Name of the LLM model used")
    # Dates are required — the frontend date picker is the authoritative source.
    # The query text should NOT contain date strings.
    start_date: str = Field(..., description="Analysis start date (YYYY-MM-DD), provided by the frontend date picker")
    end_date:   str = Field(..., description="Analysis end date (YYYY-MM-DD), provided by the frontend date picker")

class AnalyzeResponse(BaseModel):
    target_id: str
    summary: Optional[Any] = None
    all_agents_data: Dict[str, Any] = Field(..., description="Inference results for all river segments")
    params_used: List[List[str]]
    time_range: Dict[str, str]
    mode:      str = Field(..., description="Inference mode: gap_filling | prediction | mixed")
    time_mode: str = Field(..., description="Time granularity: single_day | date_range")
    segments:  List[Dict] = Field(..., description="Ordered time segments (one for pure modes, two for mixed)")


# Routing implementation
@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_water_system(request: AnalyzeRequest):
    logger.info(f"Received query request: {request.query}")
    
    # Asker analyzes the context (including topology construction here)
    context = await AskerService.prepare_inference_context(
        user_query=request.query,
        selected_model=request.model_name,
        # Sprint 7: frontend-provided dates take priority over anything in the query text
        start_date=request.start_date,
        end_date=request.end_date,
    )
    
    if not context:
        raise HTTPException(status_code=400, detail="Unable to parse the query intent or the coordinates are invalid.")

    # Prepare Agent dictionary
    agents_dict = {
        cid: RiverAgentData(**data) 
        for cid, data in context["global_data"].items()
    }
    target_id = context["start_comid"]
    inlet_comids = context["inlet_comids"]

    # Building LangGraph
    graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)
    
    if not graph_app:
        raise HTTPException(status_code=500, detail="The computation graph construction failed.")

    # run inference
    initial_state = {
        "task":            request.query,
        "current_params":  context["current_params"],
        "facility_types":  ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
        "start_date":      context["start_date"],
        "end_date":        context["end_date"],
        "global_data":     context["global_data"],
        "inlet_comids":    inlet_comids,
        "selected_model":  request.model_name,
        # Sprint 7: pass mode/time_mode/segments so nodes.py selects the correct prompt
        "mode":            context.get("mode",      "gap_filling"),
        "time_mode":       context.get("time_mode", "date_range"),
        "segments":        context.get("segments",  []),
        # Use auto resampling by default for REST endpoint
        "resample_auto":   True,
        "resample_freq_h": 6,
    }

    try:
        final_state = await graph_app.ainvoke(initial_state)
        
        # Obtain the output of the target site (i.e., the HydrologyInference dictionary you previously modified)
        target_agent = final_state["global_data"].get(target_id)
        
        return AnalyzeResponse(
            target_id=target_id,
            summary=target_agent.get("output") if target_agent else "No data",
            all_agents_data=final_state["global_data"],
            params_used=context["current_params"],
            time_range={"start": context["start_date"], "end": context["end_date"]},
            # Sprint 7: expose mode/time_mode/segments
            mode=context.get("mode",      "gap_filling"),
            time_mode=context.get("time_mode", "date_range"),
            segments=context.get("segments",  []),
        )

    except Exception as e:
        logger.error(f"Reasoning failure: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Reasoning engine run error: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "engine": "LangGraph"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)