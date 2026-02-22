# backend/debug_main.py

import asyncio
import logging
from dotenv import load_dotenv

from app.services.asker_service import AskerService
from app.agents.graph import create_hydrological_graph
from app.core.env_manager import EnvManager
from app.core.http_client import AsyncHttpClient
from app.models.river import RiverAgentData

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def main():
    load_dotenv()

    # Full reset: wipe both memory and cache for a clean test run.
    # This is intentional here – debug runs should start fresh.
    # In normal uvicorn startup (main.py), only memory is reset.
    logger.info("--- [Step 0: Full workspace reset] ---")
    EnvManager.full_reset()

    # Test parameters
    user_query     = "Compute the pH at coordinates: -92.414165705, 46.682323895 from 2026-01-01 to 2026-01-15."
    selected_model = "llama3:8b"

    # AskerService – parse intent and build topology
    logger.info("--- [Step 1: Asker - intent parsing and topology build] ---")
    context = await AskerService.prepare_inference_context(
        user_query=user_query,
        selected_model=selected_model
    )

    if not context:
        logger.error("Could not build inference context. Check API keys or coordinates.")
        return

    agents_dict  = {cid: RiverAgentData(**data) for cid, data in context["global_data"].items()}
    target_id    = context["start_comid"]
    inlet_comids = context["inlet_comids"]

    logger.info(f"Topology ready: {len(agents_dict)} agents, inlets: {inlet_comids}")

    # Build LangGraph
    logger.info("--- [Step 2: Build LangGraph] ---")
    graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)

    if not graph_app:
        logger.error("Graph construction failed: no active nodes found.")
        return

    # Run inference chain
    logger.info(f"--- [Step 3: Run inference (inlets: {inlet_comids})] ---")
    initial_state = {
        "task":           user_query,
        "current_params": context["current_params"],
        "facility_types": ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
        "start_date":     context["start_date"],
        "end_date":       context["end_date"],
        "global_data":    context["global_data"],
        "inlet_comids":   inlet_comids,
        "selected_model": selected_model,
    }

    try:
        final_state = await graph_app.ainvoke(initial_state)

        # Inspect results
        logger.info("--- [Step 4: Results] ---")
        final_agent = final_state["global_data"].get(target_id)

        if final_agent and final_agent.get("output"):
            logger.info(f"[Success] Target COMID {target_id} inference output:")
            print("-" * 60)
            print(final_agent["output"])
            print("-" * 60)

            # Verify that the new Phase-2 fields are present
            prompt_len = len(final_agent.get("prompt_used") or "")
            raw_len    = len(final_agent.get("llm_raw_output") or "")

            if prompt_len > 0:
                logger.info(f"[OK] prompt_used is present ({prompt_len} chars)")
            else:
                logger.warning("[WARN] prompt_used is empty or missing")

            if raw_len > 0:
                logger.info(f"[OK] llm_raw_output is present ({raw_len} chars)")
            else:
                logger.warning("[WARN] llm_raw_output is empty or missing")

        else:
            logger.warning(
                "Inference chain completed but target node produced no output. "
                "This usually means no upstream data was available."
            )

    except Exception as e:
        logger.error(f"Exception during inference: {e}", exc_info=True)

    finally:
        await AsyncHttpClient.close_client()
        logger.info("HTTP client safely closed.")


if __name__ == "__main__":
    asyncio.run(main())