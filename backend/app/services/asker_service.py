import pandas as pd
import logging
import json
from typing import Optional, Tuple, Dict, List, Any
from sentence_transformers import SentenceTransformer, util
from app.core.config import settings
from app.core.http_client import AsyncHttpClient
from app.agents.prompts import DATA_NEEDS_PROMPT
from app.services.llm_service import LLMService
from app.services.nldi_service import NLDIService
from app.services.topology_service import TopologyService

logger = logging.getLogger(__name__)

class AskerService:
    # 类变量，确保模型只加载一次
    _embedder = None
    _usgs_params_df = None
    _corpus_embeddings = None
    _characteristic_names = None

    @staticmethod
    async def geocode_location(location_name: str) -> Optional[Tuple[float, float]]:
        """Geocoding: Supports Google or other APIs (asynchronous implementation demonstrated here)"""
        if not settings.GOOGLE_API_KEY:
            logger.warning("The Google API Key has not been configured, thus geocoding cannot be performed")
            return None

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": location_name, "key": settings.GOOGLE_API_KEY}
        
        try:
            response = await AsyncHttpClient.request("GET", url, params=params)
            data = response.json()
            if data['status'] == 'OK':
                loc = data['results'][0]['geometry']['location']
                return loc['lat'], loc['lng']
            return None
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
            return None
        

    @staticmethod
    async def parse_user_intent(user_query: str, model_name: str = None) -> dict:
        """ Utilize LLM to parse user queries into structured JSON requirements """
        target_model = model_name or settings.DEFAULT_LLM_MODEL
        # Format Prompt
        messages = DATA_NEEDS_PROMPT.format_messages(user_query=user_query)
        # Extract the content of User messages
        full_prompt = f"{messages[0].content}\n\n{messages[1].content}"

        # Call LLM
        raw_response = await LLMService.call_llm(
            prompt=full_prompt,
            model_name=target_model
        )

        # Robust JSON parsing
        if isinstance(raw_response, dict) and "location" in raw_response:
            return raw_response
        
        # If LLMService directly returned the original string
        try:
            clean_json = str(raw_response).strip().replace("```json", "").replace("```", "")
            return json.loads(clean_json)
        except Exception as e:
            logger.error(f"Failed to parse Intent JSON: {e} | Original Output: {raw_response}")
            return {
                "location": None,
                "characteristic_names": [],
                "time_range": {"start": "2026-01-10", "end": "2026-01-20"}
            }


    @classmethod
    def _lazy_load(cls):
        if cls._embedder is None:
            logger.info("Semantic model loading in progress: all-MiniLM-L6-v2...")
            cls._embedder = SentenceTransformer('all-MiniLM-L6-v2')
            cls._usgs_params_df = pd.read_csv('../../dataset/public_srsnames_December_2023.csv', dtype={'parm_cd': str})
            cls._characteristic_names = cls._usgs_params_df['characteristicname'].unique().tolist()
            cls._corpus_embeddings = cls._embedder.encode(cls._characteristic_names, convert_to_tensor=True)

    @classmethod
    async def get_parm_cd_from_name(cls, char_name: str, top_n: int = 2) -> List[List[str]]:
        """Semantic search: Mapping user descriptions to USGS parameter codes"""
        cls._lazy_load()
        
        query_embedding = cls._embedder.encode(char_name, convert_to_tensor=True)
        hits = util.semantic_search(query_embedding, cls._corpus_embeddings, top_k=top_n)[0]
        
        results = []
        for hit in hits:
            name = cls._characteristic_names[hit['corpus_id']]
            parm_cd = cls._usgs_params_df.loc[
                cls._usgs_params_df['characteristicname'] == name, 'parm_cd'
            ].iloc[0]
            results.append([name, parm_cd])
        
        return results
        

    @classmethod
    async def prepare_inference_context(cls, user_query: str, selected_model: str) -> Optional[Dict[str, Any]]:
        """
        Full-process scheduling: Parse intent -> Geolocation -> Parameter mapping -> Build topology
        Return: Context data used to initialize WaterSystemState
        """
        logger.info(f"Task initiated | Model: {selected_model} | Query: {user_query}")

        # LLM Intent Parsing
        # This step converts the spoken language into: {"location": ..., "characteristic_names": [...], "time_range": ...}
        data_needs = await cls.parse_user_intent(user_query, model_name=selected_model)
        if not data_needs or not data_needs.get("location"):
            logger.error("No valid geographic location or intention can be parsed from the query.")
            return None

        # Location Resolution
        location = data_needs["location"]
        lat, lon = None, None

        if isinstance(location, list) and len(location) == 2:
            lat, lon = location[0], location[1]
        elif isinstance(location, str):
            lat, lon = await cls.geocode_location(location)

        if lat is None or lon is None:
            logger.error(f"Coordinate parsing failed: {location}")
            return None

        # Obtain the central COMID (NLDI Resolution)
        start_comid = await NLDIService.get_main_comid(lat, lon)
        if not start_comid:
            logger.error(f"No corresponding COMID was found near the coordinates ({lat}, {lon}).")
            return None
        logger.info(f"Positioning successful: COMID {start_comid}")

        # Semantic Parameter Mapping
        # eg: Map 'pH' to [['pH', '00400'], ...]
        char_names = data_needs.get("characteristic_names", [])
        final_params = []
        for name in char_names:
            matches = await cls.get_parm_cd_from_name(name, top_n=2)
            final_params.extend(matches)
        
        # Deduplication
        seen_params = set()
        unique_params = []
        for p in final_params:
            if p[1] not in seen_params:
                unique_params.append(p)
                seen_params.add(p[1])

        # Extract Time Range
        time_range = data_needs.get("time_range", {})
        start_date = time_range.get("start", "2026-02-01")
        end_date = time_range.get("end", "2026-02-10")

        # Topology Building and Agent Instantiation
        # This will return a Dict[str, RiverAgentData]
        agents_dict = await TopologyService.build_topology(
            start_comid=start_comid,
            max_depth=settings.MAX_TOPOLOGY_DEPTH
        )

        # Inlet detection
        # The river section without any upstream ID is the starting point of distribution
        inlet_comids = [
            cid for cid, river in agents_dict.items() 
            if river.has_usgs_site
        ]

        if not inlet_comids:
            inlet_comids = [str(start_comid)]
            logger.info(f"No upstream site was found, and the target river section {start_comid} is set as an isolated inference point.")

        logger.info(f"Ready! Instantiating a total of {len(agents_dict)} Agents, entry points: {inlet_comids}")
        logger.info(f"=================================================================================")
        logger.info(f"current_params {unique_params}")
        logger.info(f"=================================================================================")


        # Return a dictionary that meets the initial requirements of WaterSystemState
        return {
            "inlet_comids": inlet_comids,
            "start_comid": str(start_comid),
            "start_date": start_date,
            "end_date": end_date,
            "current_params": unique_params, # [[name, code], ...]
            "global_data": {cid: river.model_dump() for cid, river in agents_dict.items()},
            "task": user_query,
            "selected_model": selected_model
        }