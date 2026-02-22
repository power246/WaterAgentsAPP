import httpx
import pandas as pd
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from diskcache import Cache
import dateutil.parser
from app.core.config import settings
from app.core.http_client import AsyncHttpClient
import asyncio

logger = logging.getLogger(__name__)
os.makedirs("./.cache/weather_data", exist_ok=True)
weather_cache = Cache("./.cache/weather_data", size_limit=500 * 1024**2)

class WeatherService:
    """
    Ensure that the time format is a UTC ISO string with a time zone
    """
    @staticmethod
    def ensure_utc_iso(dt_val) -> str:
        if isinstance(dt_val, str):
            dt = dateutil.parser.parse(dt_val)
        else:
            dt = dt_val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    """
    Aggregating and obtaining weather observations and meteorological warnings
    """
    @staticmethod
    async def get_weather_and_alerts(
        lat: float, 
        lon: float, 
        start_date: str, 
        end_date: str
    ) -> Dict:
        start_iso = WeatherService.ensure_utc_iso(start_date)
        end_iso = WeatherService.ensure_utc_iso(end_date)
        lat = round(float(lat), 4)
        lon = round(float(lon), 4)
        
        # Check cache
        cache_key = f"weather_{lat}_{lon}_{start_iso}_{end_iso}"
        if cache_key in weather_cache:
            return weather_cache[cache_key]

        try:
            # Obtain the nearest observation station
            point_url = f"https://api.weather.gov/points/{lat},{lon}"
            point_res = await AsyncHttpClient.request("GET", point_url)
            if point_res.status_code != 200:
                return {"observations": [], "alerts": []}
            stations_url = point_res.json()["properties"]["observationStations"]
            
            stations_res = await AsyncHttpClient.request("GET", stations_url)
            stations_data = stations_res.json().get("features", [])
            if not stations_data:
                return {"observations": [], "alerts": []}
            
            station_id = stations_data[0]["properties"]["stationIdentifier"]

            # Concurrently obtain observation data and early warning data
            obs_url = f"https://api.weather.gov/stations/{station_id}/observations"
            alerts_url = f"https://api.weather.gov/alerts?point={lat},{lon}&limit=100"

            obs_params = {"start": start_iso, "end": end_iso}
            alerts_params = {"point": f"{lat},{lon}", "status": "actual", "limit": 10}
            
            obs_task = AsyncHttpClient.request("GET", obs_url, params=obs_params)
            alerts_task = AsyncHttpClient.request("GET", alerts_url, params=alerts_params)
            
            obs_res, alerts_res = await asyncio.gather(obs_task, alerts_task)

            # Processing observational data (Pandas resampling)
            weather_results = []
            raw_obs = obs_res.json().get("features", [])
            if raw_obs:
                rows = []
                for obs in raw_obs:
                    p = obs["properties"]
                    rows.append({
                        "time": p["timestamp"],
                        "temp": p["temperature"]["value"],
                        "precip": p.get("precipitationLastHour", {}).get("value") or 0.0
                    })
                
                df = pd.DataFrame(rows)
                df['time'] = pd.to_datetime(df['time'])
                df.set_index('time', inplace=True)
                
                resampled = df.resample(settings.WEATHER_FREQ).agg({
                    'temp': 'mean',
                    'precip': 'sum'
                }).dropna(how='all')
                
                for t, row in resampled.iterrows():
                    weather_results.append({
                        "time": t.isoformat(),
                        "temp_c": round(row["temp"], 2) if pd.notna(row["temp"]) else None,
                        "precip_mm": round(row["precip"], 2)
                    })

            # Process warning data
            alerts_results = []
            raw_alerts = alerts_res.json().get("features", [])
            if raw_alerts:
                # Simple window filtering logic
                time_windows = pd.date_range(start=start_iso, end=end_iso, freq=settings.WEATHER_FREQ)
                for i in range(len(time_windows)-1):
                    w_start, w_end = time_windows[i], time_windows[i+1]
                    active = []
                    for a in raw_alerts:
                        p = a["properties"]
                        a_start = dateutil.parser.isoparse(p.get("onset") or p.get("sent"))
                        a_end_str = p.get("ends") or p.get("expires")
                        a_end = dateutil.parser.isoparse(a_end_str) if a_end_str else w_end
                        
                        if a_start < w_end and a_end > w_start:
                            active.append({"event": p.get("event"), "severity": p.get("severity")})
                    
                    if active:
                        alerts_results.append({
                            "window": f"{w_start.strftime('%m-%d %H:%M')} to {w_end.strftime('%m-%d %H:%M')}",
                            "active_alerts": active
                        })

            result = {"observations": weather_results, "alerts": alerts_results}
            weather_cache.set(cache_key, result, expire=7200)
            return result

        except Exception as e:
            logger.error(f"Weather API Error: {e}")
            return {"observations": [], "alerts": []}