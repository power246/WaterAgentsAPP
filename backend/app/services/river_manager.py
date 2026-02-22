import asyncio
from typing import Dict, Any, Optional
from app.services.nldi_service import NLDIService

class RiverManager:
    @staticmethod
    async def get_river_metadata(comid: str) -> Dict[str, Any]:
        """
        Simultaneously obtain the spatial coordinates of the COMID and the associated site information.
        """
        coords_task = NLDIService.get_coords_by_comid(comid)
        site_task = NLDIService.get_associated_usgs_site(comid)
        
        coords, site_id = await asyncio.gather(coords_task, site_task)
        
        return {
            "lat": coords.get("lat") if coords else 0.0,
            "lon": coords.get("lon") if coords else 0.0,
            "site_id": site_id,
            "has_usgs_site": site_id is not None
        }