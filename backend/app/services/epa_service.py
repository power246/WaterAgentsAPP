import httpx
import asyncio
import logging
from typing import List, Dict, Optional
from app.core.config import settings
from app.core.http_client import AsyncHttpClient


logger = logging.getLogger(__name__)

class EPAService:
    @staticmethod
    def km_to_miles(km: float) -> float:
        return km * 0.621371


    """
    Asynchronous query task for a single facility type
    """
    @staticmethod
    async def fetch_by_program(
        lat: float, 
        lon: float, 
        radius_mile: float, 
        fac_type: str
    ) -> List[Dict]:
        url = "https://ofmpub.epa.gov/frs_public2/frs_rest_services.get_facilities"
        params = {
            "output": "JSON",
            "program_output": "yes",
            "latitude83": lat,
            "longitude83": lon,
            "search_radius": radius_mile,
            "pgm_sys_acrnm": fac_type
        }
        
        try:
            response = await AsyncHttpClient.request("GET", url, params=params)
            if response.status_code == 200:
                data = response.json()
                return data.get("Results", {}).get("FRSFacility", [])
            return []
        except Exception as e:
            logger.error(f"EPA {fac_type} query failed: {e}")
            return []



    """
    Full-scale asynchronous facility query and aggregation
    """
    @staticmethod
    async def search_facilities(
        lat: float, 
        lon: float, 
        radius_km: float, 
        facility_types: Optional[List[str]] = None
    ) -> List[Dict]:
        if not facility_types:
            facility_types = settings.FACILITY_TYPES

        radius_mile = EPAService.km_to_miles(radius_km)
        results = {}

        valid_types = [ft for ft in facility_types if ft in settings.FACILITY_TYPE_DESCRIPTION]
        
        # The EPA API may sometimes become unresponsive, so set a longer timeout
        try:
            # concurrent requests
            tasks = [
                EPAService.fetch_by_program(lat, lon, radius_mile, ft) 
                for ft in valid_types
            ]
            pages = await asyncio.gather(*tasks)
            
            # aggregate deduplication
            for idx, facilities in enumerate(pages):
                current_type = valid_types[idx]
                
                for fac in facilities:
                    reg_id = fac.get("RegistryId")
                    if not reg_id: continue
                    
                    if reg_id in results:
                        if current_type not in results[reg_id]["program_types"]:
                            results[reg_id]["program_types"].append(current_type)
                    else:
                        results[reg_id] = {
                            "registryId": reg_id,
                            "name": fac.get("FacilityName"),
                            "lat": fac.get("Latitude83"),
                            "lon": fac.get("Longitude83"),
                            "address": fac.get("LocationAddress"),
                            "program_types": [current_type]
                        }
        except Exception as e:
            logger.error(f"Failed to search for facilities with ({lon}, {lat}): {e}")
            return None
        
        logger.info(f"{len(results)} unique facilities found within {radius_km}km")
        return list(results.values())