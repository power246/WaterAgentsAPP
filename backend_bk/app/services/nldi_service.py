import atexit
import logging
import os
from typing import Optional, Dict, List
from diskcache import Cache
from app.core.config import settings
from app.core.http_client import AsyncHttpClient


logger = logging.getLogger(__name__)
os.makedirs("./.cache/nldi_data", exist_ok=True)
nldi_cache = Cache("./.cache/nldi_data", size_limit=500 * 1024**2)
# Ensure the cache semaphore is released when any process (including uvicorn's
# reloader process) exits — prevents resource_tracker hang on Ctrl+C.
atexit.register(nldi_cache.close)

class NLDIService:
    """
    Asynchronously obtain representative longitude and latitude of river segments through USGS NLDI API
    """
    @staticmethod
    async def get_coords_by_comid(comid: str) -> Optional[Dict[str, float]]:
        # check the cache
        cache_key = f"coords_{comid}"
        if cache_key in nldi_cache:
            logger.info(f"Cache Hit: {comid}")
            return nldi_cache[cache_key]
        
        logger.info(f"Network Fetch: {comid}")
        url = f"https://api.water.usgs.gov/nldi/linked-data/comid/{comid}"
        
        # Implement non-blocking requests using AsyncClient
        try:
            response = await AsyncHttpClient.request("GET", url)
            data = response.json()
            
            # Robustness check: Ensure the existence of data structure
            if "features" in data and len(data["features"]) > 0:
                geometry = data["features"][0].get("geometry", {})
                coords = geometry.get("coordinates", [])
                
                if not coords:
                    return None
                
                if isinstance(coords[0], list):
                    representative_point = coords[0]
                else:
                    representative_point = coords
                    
                result = {
                    "lon": float(representative_point[0]), 
                    "lat": float(representative_point[1])
                }
                nldi_cache.set(cache_key, result, expire=3600 * 24 * 30)
                return result
            
            logger.warning(f"COMID {comid} No valid geographical features returned")
            return None
        
        except Exception as e:
            logger.error(f"An unexpected error occurred while parsing the COMID {comid} coordinates: {e}")
            return None
            
    """
    Find the associated USGS station through comID. 
    navigation_mode: 'UT' (upstream tributary), 'UM' (upstream main stream), 'DM' (downstream main stream), 'DD' (downstream distributary)
    """
    @staticmethod
    async def get_associated_usgs_site(
        comid: str, 
        navigation_mode: str = "UT", 
        distance_km: float = settings.USGS_SEARCH_DISTANCE
    ) -> Optional[Dict[str, str]]:
        # The cache key includes mode and distance, as different parameters may yield different results
        cache_key = f"usgs_site_{comid}_{navigation_mode}_{distance_km}"
        if cache_key in nldi_cache:
            return nldi_cache[cache_key]

        # Construct NLDI Navigation URL
        url = (
            f"https://api.water.usgs.gov/nldi/linked-data/comid/{comid}"
            f"/navigation/{navigation_mode}/nwissite"
        )
        params = {
            "f": "json",
            "distance": distance_km
        }

        try:
            response = await AsyncHttpClient.request("GET", url, params=params)
            
            if response.status_code != 200:
                logger.error(f"Failed to search for USGS site with COMID {comid}: {response.status_code}")
                return None
            
            if not response.content:
                logger.info(f"+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++")
                logger.info(f"No USGS data on {comid}")
                return None
            data = response.json()
            logger.info(f"+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++")
            logger.info(f"data {data}")
            features = data.get("features", [])


            props = features[0].get("properties", {})
            raw_id = props.get("identifier", "")
            
            clean_id = raw_id.replace("USGS-", "")
            
            '''
            site_info = {
                "station_id": clean_id,
                "full_id": f"USGS-{clean_id}",
                "name": props.get("name"),
            }
            '''

            nldi_cache.set(cache_key, clean_id, expire=3600 * 24 * 30)
            logger.info(f"Found site for COMID {comid}: {clean_id}")
            return clean_id

        except Exception as e:
            logger.error(f"Failed to search for USGS site with COMID {comid}: {e}")
            return None
        

    
    """
    Obtain COMID based on coordinates
    """
    @staticmethod
    async def get_main_comid(lat: float, lon: float) -> Optional[str]:
        cache_key = f"comid_pos_{lat}_{lon}"
        if cache_key in nldi_cache:
            return nldi_cache[cache_key]
        
        url = f"https://api.water.usgs.gov/nldi/linked-data/comid/position?coords=POINT({lat}%20{lon})"
        try:
            res = await AsyncHttpClient.request("GET", url)
            data = res.json()
            comid = data['features'][0]['properties']['comid']
            return str(comid)
        except Exception as e:
            logger.error(f"Failed to obtain the primary COMID: {e}")
            return None
        


    """
    Obtain the physically adjacent upstream river section
    """
    @staticmethod
    async def get_direct_upstreams(comid: str, distance: int = 5) -> List[str]:
        url = f"https://api.water.usgs.gov/nldi/linked-data/comid/{comid}/navigation/UT/flowlines"
        params = {"distance": distance}
        try:
            res = await AsyncHttpClient.request("GET", url, params=params)
            if res.status_code != 200: return []
            
            features = res.json().get('features', [])
            current_id = str(comid)
            return [
                str(f['properties'].get('nhdplus_comid')) 
                for f in features 
                if str(f['properties'].get('nhdplus_comid')) != current_id
            ]
        except Exception as e:
            logger.error(f"Error fetching upstreams for {comid}: {e}")
            return []