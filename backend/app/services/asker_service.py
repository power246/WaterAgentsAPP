import pandas as pd
import logging
import json
from typing import Optional, Tuple, Dict, List, Any

from sentence_transformers import SentenceTransformer, util

from app.core.config import settings
from app.core.http_client import AsyncHttpClient
from app.core.user_keys_store import get_key
from app.agents.prompts import DATA_NEEDS_PROMPT
from app.services.llm_service import LLMService
from app.services.nldi_service import NLDIService
from app.services.topology_service import TopologyService

logger = logging.getLogger(__name__)


class AskerService:
    # Class-level singletons: loaded once, reused across requests.
    _embedder:            Optional[SentenceTransformer] = None
    _usgs_params_df:      Optional[pd.DataFrame]        = None
    _corpus_embeddings                                   = None
    _characteristic_names: Optional[List[str]]          = None

    # ── Geocoding ─────────────────────────────────────────────────────────────

    @staticmethod
    async def geocode_location(location_name: str) -> Optional[Tuple[float, float]]:
        """
        Convert a place-name string to (lat, lon) via the Google Maps Geocoding API.

        Key priority:
          1. GOOGLE_MAPS_API_KEY saved in user_keys_store (user entered in Settings)
          2. GOOGLE_MAPS_API_KEY from .env / settings
        """
        # Resolve the Maps key — distinct from GOOGLE_API_KEY (Gemini)
        maps_key = await get_key("GOOGLE_MAPS_API_KEY") or settings.GOOGLE_MAPS_API_KEY

        if not maps_key:
            logger.warning(
                "[AskerService] GOOGLE_MAPS_API_KEY is not configured. "
                "Geocoding by place name is unavailable. "
                "Use coordinates directly in the query instead."
            )
            return None

        url    = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": location_name, "key": maps_key}

        try:
            response = await AsyncHttpClient.request("GET", url, params=params)
            data     = response.json()
            if data.get("status") == "OK":
                loc = data["results"][0]["geometry"]["location"]
                logger.info(
                    f"[AskerService] Geocoded '{location_name}' → "
                    f"({loc['lat']}, {loc['lng']})"
                )
                return loc["lat"], loc["lng"]
            logger.warning(
                f"[AskerService] Geocoding returned status '{data.get('status')}' "
                f"for '{location_name}'"
            )
            return None
        except Exception as exc:
            logger.error(f"[AskerService] Geocoding error: {exc}")
            return None

    # ── Intent parsing ────────────────────────────────────────────────────────

    @staticmethod
    async def parse_user_intent(user_query: str, model_name: str = None) -> dict:
        """Use LLM to parse the natural-language query into structured JSON."""
        target_model = model_name or settings.DEFAULT_LLM_MODEL

        messages    = DATA_NEEDS_PROMPT.format_messages(user_query=user_query)
        full_prompt = f"{messages[0].content}\n\n{messages[1].content}"

        raw_response = await LLMService.call_llm(
            prompt=full_prompt,
            model_name=target_model,
        )

        # If the LLM already returned a parsed dict (shouldn't happen, but guard)
        if isinstance(raw_response, dict) and "location" in raw_response:
            return raw_response

        try:
            clean = str(raw_response).strip().replace("```json", "").replace("```", "")
            return json.loads(clean)
        except Exception as exc:
            logger.error(
                f"[AskerService] Intent JSON parse failed: {exc} | "
                f"Raw output: {raw_response}"
            )
            return {
                "location": None,
                "characteristic_names": [],
                "time_range": {"start": "2026-01-10", "end": "2026-01-20"},
            }

    # ── Semantic parameter mapping ────────────────────────────────────────────

    @classmethod
    def _lazy_load(cls) -> None:
        """Load the sentence-transformer model and USGS parameter CSV once."""
        if cls._embedder is not None:
            return
        logger.info("[AskerService] Loading semantic model (all-MiniLM-L6-v2)...")
        cls._embedder            = SentenceTransformer("all-MiniLM-L6-v2")
        cls._usgs_params_df      = pd.read_csv(
            "../../dataset/public_srsnames_December_2023.csv",
            dtype={"parm_cd": str},
        )
        cls._characteristic_names = cls._usgs_params_df["characteristicname"].unique().tolist()
        cls._corpus_embeddings    = cls._embedder.encode(
            cls._characteristic_names, convert_to_tensor=True
        )

    @classmethod
    async def get_parm_cd_from_name(cls, char_name: str, top_n: int = 2) -> List[List[str]]:
        """Semantic search: map a description to the top-N USGS parameter codes."""
        cls._lazy_load()
        query_embedding = cls._embedder.encode(char_name, convert_to_tensor=True)
        hits            = util.semantic_search(
            query_embedding, cls._corpus_embeddings, top_k=top_n
        )[0]

        results = []
        for hit in hits:
            name    = cls._characteristic_names[hit["corpus_id"]]
            parm_cd = cls._usgs_params_df.loc[
                cls._usgs_params_df["characteristicname"] == name, "parm_cd"
            ].iloc[0]
            results.append([name, parm_cd])
        return results

    # ── Main entry point ──────────────────────────────────────────────────────

    @classmethod
    async def prepare_inference_context(
        cls,
        user_query:         str,
        selected_model:     str,
        max_topology_depth: int = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Full pipeline: parse intent → resolve location → map parameters →
        build topology → return WaterSystemState-compatible context dict.

        max_topology_depth: overrides settings.MAX_TOPOLOGY_DEPTH when provided
                            (forwarded from the frontend Settings panel).
        """
        depth = max_topology_depth or settings.MAX_TOPOLOGY_DEPTH
        logger.info(
            f"[AskerService] Query: {user_query!r} | "
            f"Model: {selected_model} | BFS depth: {depth}"
        )

        # Step 1: LLM intent parsing
        data_needs = await cls.parse_user_intent(user_query, model_name=selected_model)
        if not data_needs or not data_needs.get("location"):
            logger.error("[AskerService] Could not extract a valid location from the query.")
            return None

        # Step 2: Resolve location to (lat, lon)
        location = data_needs["location"]
        lat, lon = None, None

        if isinstance(location, list) and len(location) == 2:
            lat, lon = float(location[0]), float(location[1])
        elif isinstance(location, str):
            lat, lon = await cls.geocode_location(location)

        if lat is None or lon is None:
            logger.error(f"[AskerService] Coordinate resolution failed for: {location!r}")
            return None

        # Step 3: COMID lookup
        start_comid = await NLDIService.get_main_comid(lat, lon)
        if not start_comid:
            logger.error(
                f"[AskerService] No COMID found near ({lat}, {lon})."
            )
            return None
        logger.info(f"[AskerService] Resolved COMID: {start_comid}")

        # Step 4: Semantic parameter mapping
        char_names  = data_needs.get("characteristic_names", [])
        final_params: List[List[str]] = []
        for name in char_names:
            final_params.extend(await cls.get_parm_cd_from_name(name, top_n=2))

        # Deduplicate by parameter code
        seen:         set               = set()
        unique_params: List[List[str]]  = []
        for p in final_params:
            if p[1] not in seen:
                unique_params.append(p)
                seen.add(p[1])

        # Step 5: Time range
        time_range = data_needs.get("time_range", {})
        start_date = time_range.get("start", "2026-02-01")
        end_date   = time_range.get("end",   "2026-02-10")

        # Step 6: Build topology
        agents_dict = await TopologyService.build_topology(
            start_comid=start_comid,
            max_depth=depth,
        )

        # Step 7: Find inlet nodes (river segments with real USGS gauge data)
        inlet_comids = [
            cid for cid, river in agents_dict.items() if river.has_usgs_site
        ]
        if not inlet_comids:
            inlet_comids = [str(start_comid)]
            logger.info(
                f"[AskerService] No upstream gauges found — "
                f"treating {start_comid} as isolated inference point."
            )

        logger.info(
            f"[AskerService] Ready: {len(agents_dict)} agents, "
            f"inlets: {inlet_comids}, params: {unique_params}"
        )

        return {
            "inlet_comids":  inlet_comids,
            "start_comid":   str(start_comid),
            "start_date":    start_date,
            "end_date":      end_date,
            "current_params": unique_params,       # [[name, code], ...]
            "global_data":   {cid: river.model_dump() for cid, river in agents_dict.items()},
            "task":          user_query,
            "selected_model": selected_model,
        }