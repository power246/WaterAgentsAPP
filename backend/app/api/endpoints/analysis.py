from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
from app.core.config import settings

router = APIRouter()

# Define the request body model
class AnalysisRequest(BaseModel):
    target_comid: str
    # Allow the frontend to override the default model. If not provided, use the default value from settings
    model_name: Optional[str] = settings.OLLAMA_MODEL
    provider: Optional[str] = "ollama"
    # Allow the front end to select facility types
    selected_facility_types: List[str] = settings.FACILITY_TYPES

@router.post("/run-analysis")
async def run_analysis(request: AnalysisRequest):
    print(f"Using model: {request.model_name}")
    print(f"Analyzed facility types:{request.selected_facility_types}")
    
    # Pass these parameters to the LangGraph run function
    # result = await run_hydro_graph(request)
    return {"status": "started", "config": request}