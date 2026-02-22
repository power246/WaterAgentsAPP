import asyncio
import logging
from collections import deque
from typing import Dict, List, Set, Tuple

from app.models.river import RiverAgentData
from app.services.nldi_service import NLDIService
from app.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

class TopologyService:
    @staticmethod
    async def build_topology(
        start_comid: str, 
        max_depth: int = 10
    ) -> Dict[str, RiverAgentData]:
        """
        Construct hydrological topology: trace upstream from the starting point until a measured station is found or the maximum depth is reached.
        """
        agents: Dict[str, RiverAgentData] = {}
        queue = deque([(str(start_comid), 0)])
        visited: Set[str] = set()

        logger.info(f"Starting to construct hydrological topology (starting point: {start_comid}, maximum depth: {max_depth})")

        while queue:
            curr_id, depth = queue.popleft()
            if curr_id in visited or depth >= max_depth:
                continue
            visited.add(curr_id)

            # Initialize or obtain the RiverAgentData object
            if curr_id not in agents:
                agents[curr_id] = RiverAgentData(comid=curr_id)
            river = agents[curr_id]

            # Memory-First
            file_path = f"./data/memory/{curr_id}.json"
            memory = await MemoryService.load_memory(file_path)
            cached_meta = memory.get("static_metadata", {})

            # Determine the site ID (preferably obtain it from memory)
            site_id = cached_meta.get("site_id")
            if not site_id:
                # If there is no memory, check the network
                site_id = await NLDIService.get_associated_usgs_site(curr_id)
                # Update memories for future use
                if site_id:
                    cached_meta["site_id"] = site_id
                    memory["static_metadata"] = cached_meta
                    await MemoryService.save_memory(file_path, memory)

            # Stop condition: If there is a measured station in the current river section, stop searching upstream branches
            if site_id:
                river.site_id = site_id
                river.has_usgs_site = True
                logger.info(f"Layer {depth}: COMID {curr_id} has found site {site_id}, stopping tracing up this branch.")
                continue

            # Obtain direct upstream
            candidate_up_ids = await NLDIService.get_direct_upstreams(curr_id)
            up_ids_set = set(candidate_up_ids)
            
            # The logic here is: if B is upstream of A and C is also upstream of B,
            # then in the list of direct upstreams of A, if C appears, it should be removed and only B should be retained.
            for b in candidate_up_ids:
                b_upstreams = await NLDIService.get_direct_upstreams(b)
                for c in b_upstreams:
                    if c in up_ids_set:
                        up_ids_set.remove(c)
            
            final_up_ids = list(up_ids_set)
            river.upstream_ids = final_up_ids

            if final_up_ids:
                logger.info(f"Layer {depth}: COMID {curr_id} <- Upstreams {final_up_ids}")
            else:
                logger.info(f"COMID {curr_id} no more Upstreams {final_up_ids}")
            

            # Add upstream to the queue and establish downstream connections
            for uid in final_up_ids:
                if uid not in agents:
                    agents[uid] = RiverAgentData(comid=uid)
                
                # Establish reverse dependency: Record who is my downstream
                if curr_id not in agents[uid].downstream_ids:
                    agents[uid].downstream_ids.append(curr_id)
                
                if uid not in visited:
                    queue.append((uid, depth + 1))

        return agents