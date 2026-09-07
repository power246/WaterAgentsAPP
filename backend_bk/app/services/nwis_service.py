import atexit
import pandas as pd
import numpy as np
import logging
import os
from typing import Optional, Dict, List
from diskcache import Cache
from app.core.config import settings
from app.core.http_client import AsyncHttpClient
from app.models.river import ParameterResult, DailyParameterResult, AnomalyWindow

logger = logging.getLogger(__name__)
os.makedirs("./.cache/nwis_data", exist_ok=True)
nwis_cache = Cache("./.cache/nwis_data", size_limit=500 * 1024**2)
# Ensure the cache semaphore is released when any process (including uvicorn's
# reloader process) exits — prevents resource_tracker hang on Ctrl+C.
atexit.register(nwis_cache.close)

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
    

    """
    Compute daily representative values and detect sub-daily anomaly windows
    from the raw resampled data returned by get_daily_averages().

    Args:
        raw_data: dict returned by get_daily_averages(), containing
                    an "averages" list where each entry has:
                    - "parameter_code": str
                    - "parameter_name": str
                    - "unit": str
                    - "data_6h": list of {"date": "YYYY-MM-DD HH:MM", "avg_value": float|None}
        anomaly_threshold_pct: a sub-daily window is flagged as anomalous
                                when abs(value - daily_mean) / daily_mean * 100
                                exceeds this threshold.

    Returns:
        List[ParameterResult] — one entry per parameter, each containing
        DailyParameterResult objects (one per calendar day) with any detected
        AnomalyWindow objects attached.
    """
    @staticmethod
    def compute_daily_summary(
        raw_data: Dict,
        anomaly_threshold_pct: float,
    ) -> List[ParameterResult]:
        results: List[ParameterResult] = []

        if not raw_data or not raw_data.get("averages"):
            logger.warning("[NWISService] compute_daily_summary: raw_data is empty or missing 'averages'")
            return results

        for param_entry in raw_data["averages"]:
            p_name = param_entry.get("parameter_name", "Unknown")
            p_code = param_entry.get("parameter_code", "")
            p_unit = param_entry.get("unit")
            data_points = param_entry.get("data_6h", [])

            # Build a DataFrame from the sub-daily data points
            rows = []
            for pt in data_points:
                if pt["avg_value"] is not None:
                    rows.append({
                        "datetime": pd.to_datetime(pt["date"]),
                        "value": float(pt["avg_value"]),
                    })
            if not rows:
                logger.info(f"[NWISService] {p_name}: no valid data points, skipping")
                continue

            df = pd.DataFrame(rows).set_index("datetime").sort_index()

            # Group sub-daily windows by calendar date
            df["date_str"] = df.index.strftime("%Y-%m-%d")
            daily_groups = df.groupby("date_str")

            daily_results: List[DailyParameterResult] = []
            total_anomalies = 0

            for date_str, group in daily_groups:
                values = group["value"].dropna()
                if values.empty:
                    # No valid data for this day: record None with zero confidence
                    daily_results.append(DailyParameterResult(
                        date=date_str,
                        representative_value=None,
                        unit=p_unit,
                        confidence=0.0,
                        anomalies=[],
                    ))
                    continue

                daily_mean = float(values.mean())
                # Round to 4 significant figures for readability
                representative_value = round(daily_mean, 4)

                # Detect anomalous sub-daily windows
                anomalies: List[AnomalyWindow] = []
                for ts, row in group.iterrows():
                    v = row["value"]
                    if pd.isna(v):
                        continue
                    if daily_mean == 0:
                        # Avoid division by zero; treat any non-zero value as anomalous
                        deviation_pct = 100.0 if v != 0 else 0.0
                    else:
                        deviation_pct = abs(v - daily_mean) / abs(daily_mean) * 100.0

                    if deviation_pct >= anomaly_threshold_pct:
                        # Format the window label using the timestamp
                        window_start = ts.strftime("%Y-%m-%d %H:%M")
                        # Infer window end by adding the interval inferred from data spacing
                        # (leave as open-ended label if only one point exists)
                        window_label = f"{window_start}"
                        anomalies.append(AnomalyWindow(
                            time_window=window_label,
                            parameter=p_name,
                            observed_value=round(float(v), 4),
                            daily_mean=round(daily_mean, 4),
                            deviation_pct=round(deviation_pct, 2),
                            # possible_cause is filled in by the LLM later;
                            # leave a descriptive placeholder here.
                            possible_cause="Pending LLM analysis",
                        ))

                total_anomalies += len(anomalies)

                # Confidence heuristic: penalise days with fewer than 4 valid windows
                n_valid = len(values)
                confidence = min(1.0, n_valid / 4.0)

                daily_results.append(DailyParameterResult(
                    date=date_str,
                    representative_value=representative_value,
                    unit=p_unit,
                    confidence=round(confidence, 3),
                    anomalies=anomalies,
                ))

            logger.info(
                f"[NWISService] Site {raw_data.get('site_id', '?')} | {p_name} | "
                f"{len(daily_results)} days processed | {total_anomalies} anomalies detected"
            )

            results.append(ParameterResult(
                name=p_name,
                code=p_code,
                unit=p_unit,
                daily_results=daily_results,
            ))

        return results



