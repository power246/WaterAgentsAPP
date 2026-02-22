import httpx
import pandas as pd
import numpy as np
import logging
import os
from typing import Optional, Dict, List
from diskcache import Cache
from app.core.config import settings
from app.core.http_client import AsyncHttpClient

logger = logging.getLogger(__name__)
os.makedirs("./.cache/nwis_data", exist_ok=True)
nwis_cache = Cache("./.cache/nwis_data", size_limit=500 * 1024**2)

class NWISService:
    """
    Obtain the average historical observation value of the parameter within the specified time range
    """
    @staticmethod
    async def get_daily_averages(
        site_id: str,
        start_date: str,
        end_date: str,
        parm_codes: List[List[str]],
        resample_freq: str = None,    # e.g. "6h"; defaults to settings value
    ) -> Optional[Dict]:
        freq = resample_freq or settings.WATER_DATA_RESAMPLE_FREQ
        # Include freq in cache key so different intervals don't share cached data
        codes_str = ",".join(sorted(list(set([item[1] for item in parm_codes]))))
        cache_key = f"mean_value_{site_id}_{start_date}_{end_date}_{codes_str}_{freq}"
        logger.info(f"^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^")
        logger.info(f"codes_str: {codes_str}")
        
        if cache_key in nwis_cache:
            logger.info(f"Cache Hit: {site_id}")
            return nwis_cache[cache_key]

        # Capture raw instantaneous data
        url = "https://waterservices.usgs.gov/nwis/iv/"
        params = {
            "format": "json",
            "sites": site_id,
            "parameterCd": codes_str,
            "startDT": start_date,
            "endDT": end_date,
            "siteStatus": "all"
        }

        try:
            response = await AsyncHttpClient.request("GET", url, params=params)
            data = response.json()
            
            time_series_list = data.get('value', {}).get('timeSeries', [])
            if not time_series_list:
                logger.info(f"No time_series_list of {codes_str} in: {site_id}")
                return None

            daily_summary = []

            # process data
            for ts in time_series_list:
                p_code = ts['variable']['variableCode'][0]['value']
                p_name = ts['variable']['variableName']
                unit = ts['variable']['unit']['unitCode']
                points = ts.get('values', [{}])[0].get('value', [])
                
                if not points: continue

                df = pd.DataFrame(points)
                df['dateTime'] = pd.to_datetime(df['dateTime'])
                df['value'] = pd.to_numeric(df['value'], errors='coerce')
                
                df.loc[df['value'] <= -900000, 'value'] = np.nan
                
                df.set_index('dateTime', inplace=True)
                daily_df = df['value'].resample(freq, label='right').mean().reset_index()

                processed_data = []
                for _, row in daily_df.iterrows():
                    if pd.notna(row['value']):
                        processed_data.append({
                            "date": row['dateTime'].strftime('%Y-%m-%d %H:%M'),
                            "avg_value": round(row['value'], 3)
                        })
                    else:
                        processed_data.append({
                            "date": row['dateTime'].strftime('%Y-%m-%d %H:%M'),
                            "avg_value": None 
                        })

                daily_summary.append({
                    "parameter_code": p_code,
                    "parameter_name": p_name,
                    "unit": unit,
                    "data_6h": processed_data
                })

            result = {
                "site_id": site_id,
                "time_range": {"start": start_date, "end": end_date},
                "averages": daily_summary
            }

            nwis_cache.set(cache_key, result, expire=3600)
            return result

        except Exception as e:
            logger.error(f"Failed to obtain NWIS data ({site_id}): {e}")
            return None
            

    """
    Obtain the daily average data specifically for discharge (Discharge, code 00060).
    If there is no USGS site, return None.
    """
    @staticmethod
    async def get_flow_data(
        site_id: str,
        start_date: str,
        end_date: str,
        resample_freq: str = None,
    ) -> Optional[Dict]:
        if not site_id:
            return None

        flow_params = [["Discharge", "00060"]]

        data = await NWISService.get_daily_averages(
            site_id=site_id,
            start_date=start_date,
            end_date=end_date,
            parm_codes=flow_params,
            resample_freq=resample_freq,
        )
        
        if not data or not data.get("averages"):
            logger.warning(f"Site {site_id} exists but has no discharge (00060) data")
            return None
        logger.info(f"======================================================================")
        logger.info(f"Site {site_id} exists and has discharge (00060) data!")
        return data