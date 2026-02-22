import asyncio
import logging
from typing import List, Dict, Optional
from app.services.epa_service import EPAService
from app.services.weather_service import WeatherService
from app.services.nldi_service import NLDIService

logger = logging.getLogger(__name__)

class EnvironmentService:
    """
    Integrate environmental factors: concurrently obtain nearby facilities, NOAA weather updates, and early warnings
    """
    @staticmethod
    async def get_environmental_factors(
        comid: str,
        lat: float,
        lon: float,
        start_date: str,
        end_date: str,
        facility_types: List[str],
        radius_km: float = 1.0
    ) -> Dict:
        # If there are no coordinates, obtain them through NLDI first
        if lat == 0.0 or lon == 0.0:
            logger.info(f"COMID {comid} is missing coordinates and is automatically obtaining them...")
            coords = await NLDIService.get_coords_by_comid(comid)
            if coords:
                lat, lon = coords["lat"], coords["lon"]
            else:
                logger.error(f"Unable to obtain the coordinates of COMID {comid}")
                return {"facilities": [], "weather": [], "alerts": []}

        # Concurrently call the EPA and Weather services
        try:
            logger.info(f"Concurrently retrieving environmental data for COMID {comid}...")
            
            epa_task = EPAService.search_facilities(lat, lon, radius_km, facility_types)
            weather_task = WeatherService.get_weather_and_alerts(lat, lon, start_date, end_date)
            
            facilities, weather_data = await asyncio.gather(epa_task, weather_task)
            
            return {
                "facilities": facilities,
                "weather": weather_data.get("observations", []),
                "alerts": weather_data.get("alerts", [])
            }
            
        except Exception as e:
            logger.error(f"Error occurred during the integration of environmental data: {e}")
            return {"facilities": [], "weather": [], "alerts": []}