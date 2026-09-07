import atexit
import pandas as pd
import logging
import os
from datetime import timezone
from typing import Dict
from diskcache import Cache
import dateutil.parser
from app.core.config import settings
from app.core.http_client import AsyncHttpClient
import asyncio
import re

logger = logging.getLogger(__name__)
os.makedirs("./.cache/weather_data", exist_ok=True)
weather_cache = Cache("./.cache/weather_data", size_limit=500 * 1024**2)
# Ensure the cache semaphore is released when any process (including uvicorn's
# reloader process) exits — prevents resource_tracker hang on Ctrl+C.
atexit.register(weather_cache.close)

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
        


    """
    Parse a simple ISO 8601 duration string (e.g. 'PT1H', 'PT6H', 'P1DT6H')
    into a float number of hours.
    Only handles days (D) and hours (H); minutes and seconds are ignored
    since NOAA QPF intervals are always in whole hours.
    """
    @staticmethod
    def _parse_iso8601_duration_to_hours(duration_str: str) -> float:

        days  = re.search(r"(\d+)D", duration_str)
        hours = re.search(r"(\d+)H", duration_str)
        total = 0.0
        if days:
            total += float(days.group(1)) * 24
        if hours:
            total += float(hours.group(1))
        return total if total > 0 else 1.0   # fallback to 1h


    """
    Fetch NOAA weather forecast for a future date range.

    Combines two NOAA data sources so the LLM can reason about both:
        - probabilityOfPrecipitation (PoP, %)  from the hourly forecast endpoint
        - quantitativePrecipitation  (QPF, mm) from the gridpoints endpoint

    API call sequence:
        Step 1: GET /points/{lat},{lon}
            -> forecastHourly URL + gridId / gridX / gridY
        Step 2a: GET {forecastHourly}
            -> hourly periods with temp + PoP
        Step 2b: GET /gridpoints/{gridId}/{gridX},{gridY}
            -> QPF time-series in real mm values

    Both sources are fetched concurrently (asyncio.gather).
    Results are merged per timestamp, resampled to WEATHER_FREQ, and
    returned in the same list format as get_weather_and_alerts() so
    nodes.py needs no format-specific branching.

    Output observation dict format:
        {
        "time":           ISO string,
        "temp_c":         float | None,
        "precip_prob_pct": float,   # PoP  0-100; how likely precipitation occurs
        "precip_mm":      float,    # QPF  real mm; how much precipitation expected
        }

    Returns {"observations": [...], "alerts": []} on success.
    Returns {"observations": [], "alerts": []}   on any error (no exception raised).
    """
    @staticmethod
    async def get_weather_forecast(
        lat: float,
        lon: float,
        start_date: str,
        end_date: str,
    ) -> Dict:
        lat = round(float(lat), 4)
        lon = round(float(lon), 4)

        cache_key = f"forecast_v2_{lat}_{lon}_{start_date}_{end_date}"
        if cache_key in weather_cache:
            logger.info(
                f"[WeatherService] Forecast cache hit: ({lat}, {lon}) "
                f"{start_date} -> {end_date}"
            )
            return weather_cache[cache_key]

        try:
            # resolve grid point
            point_url = f"https://api.weather.gov/points/{lat},{lon}"
            point_res = await AsyncHttpClient.request("GET", point_url)
            if point_res.status_code != 200:
                logger.warning(
                    f"[WeatherService] /points returned {point_res.status_code} "
                    f"for ({lat}, {lon})"
                )
                return {"observations": [], "alerts": []}

            point_props      = point_res.json().get("properties", {})
            forecast_hourly_url = point_props.get("forecastHourly")
            grid_id          = point_props.get("gridId")
            grid_x           = point_props.get("gridX")
            grid_y           = point_props.get("gridY")

            if not forecast_hourly_url:
                logger.warning(f"[WeatherService] No forecastHourly URL for ({lat}, {lon})")
                return {"observations": [], "alerts": []}

            # fetch hourly forecast and QPF concurrently
            gridpoints_url = (
                f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}"
                if grid_id and grid_x is not None and grid_y is not None
                else None
            )

            hourly_task = AsyncHttpClient.request("GET", forecast_hourly_url)
            qpf_task    = (
                AsyncHttpClient.request("GET", gridpoints_url)
                if gridpoints_url
                else asyncio.sleep(0, result=None)
            )

            hourly_res, qpf_res = await asyncio.gather(hourly_task, qpf_task)

            # Parse hourly forecast: temp + PoP─
            periods = []
            if hourly_res and hourly_res.status_code == 200:
                periods = (
                    hourly_res.json()
                    .get("properties", {})
                    .get("periods", [])
                )
            else:
                logger.warning("[WeatherService] forecastHourly request failed or empty")

            start_dt = pd.Timestamp(start_date).tz_localize("UTC")
            end_dt   = pd.Timestamp(end_date).tz_localize("UTC") + pd.Timedelta(days=1)

            rows = {}   # timestamp -> dict, keyed by UTC hour for easy QPF merge
            for period in periods:
                t = pd.Timestamp(period["startTime"]).tz_convert("UTC")
                if t < start_dt or t >= end_dt:
                    continue

                temp_f = period.get("temperature")
                temp_c = round((temp_f - 32) * 5 / 9, 2) if temp_f is not None else None

                pop_obj  = period.get("probabilityOfPrecipitation", {})
                pop_val  = pop_obj.get("value") if isinstance(pop_obj, dict) else None
                pop_pct  = round(float(pop_val), 1) if pop_val is not None else 0.0

                rows[t] = {
                    "time":            t,
                    "temp":            temp_c,
                    "precip_prob_pct": pop_pct,
                    "precip_mm":       0.0,    # filled in below from QPF
                }

            if not rows:
                logger.warning(
                    f"[WeatherService] No forecast periods in range "
                    f"{start_date} -> {end_date} for ({lat}, {lon})"
                )
                return {"observations": [], "alerts": []}

            # Parse QPF: real mm values from gridpoints
            if qpf_res and hasattr(qpf_res, "status_code") and qpf_res.status_code == 200:
                qpf_data = (
                    qpf_res.json()
                    .get("properties", {})
                    .get("quantitativePrecipitation", {})
                )
                qpf_values = qpf_data.get("values", [])

                for entry in qpf_values:
                    # validTime format: "2026-03-23T00:00:00+00:00/PT6H"
                    valid_time_str = entry.get("validTime", "")
                    qpf_mm_raw     = entry.get("value")   # unit: mm (wmoUnit:mm)
                    if not valid_time_str or qpf_mm_raw is None:
                        continue

                    parts      = valid_time_str.split("/")
                    period_start = pd.Timestamp(parts[0]).tz_convert("UTC")
                    duration_h   = (
                        WeatherService._parse_iso8601_duration_to_hours(parts[1])
                        if len(parts) == 2 else 1.0
                    )

                    # Distribute QPF evenly across each hour in the interval
                    mm_per_hour = float(qpf_mm_raw) / duration_h
                    for h in range(int(duration_h)):
                        t_hour = period_start + pd.Timedelta(hours=h)
                        if t_hour in rows:
                            rows[t_hour]["precip_mm"] = round(
                                rows[t_hour]["precip_mm"] + mm_per_hour, 4
                            )
            else:
                logger.warning(
                    "[WeatherService] QPF (gridpoints) unavailable; "
                    "precip_mm will be 0 for all entries"
                )

            # Resample to WEATHER_FREQ
            df = pd.DataFrame(list(rows.values())).set_index("time").sort_index()
            resampled = df.resample(settings.WEATHER_FREQ).agg(
                {
                    "temp":            "mean",
                    "precip_prob_pct": "mean",   # avg PoP over the window
                    "precip_mm":       "sum",    # total QPF over the window
                }
            ).dropna(how="all")

            observations = []
            for t, row in resampled.iterrows():
                observations.append({
                    "time":            t.isoformat(),
                    "temp_c":          round(row["temp"], 2)           if pd.notna(row["temp"])            else None,
                    "precip_prob_pct": round(row["precip_prob_pct"], 1) if pd.notna(row["precip_prob_pct"]) else 0.0,
                    "precip_mm":       round(row["precip_mm"], 2)       if pd.notna(row["precip_mm"])       else 0.0,
                })

            logger.info(
                f"[WeatherService] Forecast fetched for ({lat}, {lon}): "
                f"{len(observations)} entries from {start_date} to {end_date}"
            )

            result = {"observations": observations, "alerts": []}
            weather_cache.set(cache_key, result, expire=3600)
            return result

        except Exception as e:
            logger.error(f"[WeatherService] get_weather_forecast error: {e}")
            return {"observations": [], "alerts": []}



