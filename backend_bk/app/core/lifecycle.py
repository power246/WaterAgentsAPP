import logging

from app.core.http_client import AsyncHttpClient
from app.services.nldi_service import nldi_cache
from app.services.nwis_service import nwis_cache
from app.services.weather_service import weather_cache


logger = logging.getLogger(__name__)


async def shutdown_all_resources():
    """
    Unify the management of all resources that need to be released
    """
    logger.info("Starting to close system resources ...")
    
    # Close network connection (HTTP Client)
    await AsyncHttpClient.close_client()
    
    # Close the disk cache handle (DiskCache)
    nldi_cache.close()
    nwis_cache.close()
    weather_cache.close()
    
    logger.info("All resources have been safely released.")