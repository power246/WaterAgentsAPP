from langchain_core.prompts import ChatPromptTemplate
from app.core.config import settings

# ---------------------------------------------------------------------------
# Facility type reference string (injected into every prompt)
# ---------------------------------------------------------------------------
FACILITY_INFO_STR = "\n".join(
    [f"  {k}: {v}" for k, v in settings.FACILITY_TYPE_DESCRIPTION.items()]
)

# ---------------------------------------------------------------------------
# Shared system preamble — schema rules that apply to BOTH prompt types.
# Kept deliberately short so the model still remembers them when it reaches
# the actual data section.
# ---------------------------------------------------------------------------
_SCHEMA_SYSTEM = """\
You are a Hydrologist AI Agent for river segment COMID {comid}.

=== YOUR ONLY JOB ===
Read the data provided and output ONE valid JSON object.
No markdown, no code fences, no text before or after the JSON.

=== EXAMPLE OF REQUIRED OUTPUT STRUCTURE ===
{{
  "parameters": [
    {{
      "name":           "parameter name",
      "code":           "USGS code",
      "estimate_value": 7.5,
      "unit":           "std units"
    }},
    {{
      "name":           "another parameter",
      "code":           "00409",
      "estimate_value": null,
      "unit":           "mg/L"
    }}
  ],
  "estimated_flow": 1200.0,
  "flow_results": {{
    "name": "Discharge",
    "code": "00060",
    "unit": "ft3/s",
    "daily_results": [
      {{
        "date": "YYYY-MM-DD",
        "representative_value": 1200.0,
        "unit": "ft3/s",
        "confidence": 0.85,
        "anomalies": []
      }}
    ]
  }},
  "confidence": 0.87,
  "analysis": "WHAT WE KNOW: ... WHY: ... MEANING: ... CONFIDENCE REASON: ... KEY TAKEAWAY: ...",
  "detailed_results": [
    {{
      "name": "parameter name",
      "code": "USGS code",
      "unit": "std units",
      "daily_results": [
        {{
          "date": "YYYY-MM-DD",
          "representative_value": 7.5,
          "unit": "std units",
          "confidence": 0.87,
          "anomalies": [
            {{
              "time_window": "YYYY-MM-DD HH:MM",
              "parameter": "parameter name",
              "observed_value": 5.1,
              "daily_mean": 7.5,
              "deviation_pct": 32.0,
              "possible_cause": "Brief explanation of why this window is anomalous"
            }}
          ]
        }}
      ]
    }}
  ],
  "explanation": {{
    "primary_drivers": [
      {{
        "factor": "e.g. Upstream COMID 12345 or ICIS facility XYZ",
        "direction": "increasing or decreasing or neutral",
        "magnitude": "high or medium or low",
        "evidence": "Specific data or observation supporting this judgment"
      }}
    ],
    "reasoning_chain": "Full step-by-step causal narrative: A causes B; B affects C through mechanism D.",
    "uncertainty_sources": "What data is missing, what assumptions were made"
  }}
}}

=== HARD RULES (violations break the system) ===
R1. "parameters" is a JSON ARRAY [ ] — never an object {{ }}.
R2. Each parameter entry has EXACTLY these 4 keys: "name", "code", "estimate_value", "unit".
    Do NOT add "COMID", "parameter_name", "confidence", "analysis", or any other key.
R3. "confidence" and "analysis" go at the TOP LEVEL — never inside a parameter entry.
R4. Include EVERY parameter listed in [TARGET PARAMETERS]. If no data, use null.
R5. Do NOT fabricate numbers. Null is the correct and honest answer when data is absent.
R6. "detailed_results" must contain one entry per TARGET PARAMETER.
    Each entry's "daily_results" MUST have EXACTLY one entry per calendar day
    in the time range — no merging, no skipping, no collapsing.
    Example: time range 2026-01-01 to 2026-01-03 requires daily_results with
    3 entries dated "2026-01-01", "2026-01-02", "2026-01-03" in order.
    Producing only 1 entry for a multi-day range is a SEVERE SCHEMA VIOLATION.
    For gap-filling: fill anomalies only for windows flagged in [ANOMALY SUMMARY].
    For prediction: anomalies list must be [] for every future day.
R7. "explanation.primary_drivers" must list every identifiable influence source.
    Never leave primary_drivers as an empty list — if truly no data exists,
    explain that explicitly in uncertainty_sources.
R8. In gap-filling mode: reasoning_chain must cite specific observed values and dates.
    In prediction mode: reasoning_chain must describe how values are expected to
    change day-by-day and why (e.g. "Day 3 rain forecast causes pH to drop").
R9. "estimated_flow" is the single representative flow value for the whole period (float).
    "flow_results" mirrors the structure of one detailed_results entry — it MUST contain
    one daily_results entry per calendar day in the time range.
    - Use the most recent discharge reading as the Day 0 anchor for predictions.
    - For gap-filling: derive from [DISCHARGE] sensor data.
    - For prediction: extrapolate from [RECENT DISCHARGE BASELINE] and precipitation forecast.
    - For inference-only nodes: derive from upstream flow_results using mass balance.
    - Use null for estimated_flow and omit flow_results ONLY when NO discharge data
      or upstream flow information exists at all.
    - anomalies in flow_results daily_results should follow the same anomaly detection
      rules as other parameters (gap-filling only; prediction = empty []).

=== WRONG — never produce this ===
{{ "parameters": [ {{ "COMID": 123, "parameter_name": "pH", "confidence": 0.9 }} ] }}

=== CONFIDENCE CALIBRATION ===
Pick the number that honestly reflects your data situation for THIS run.
Do NOT default to a round number like 0.8.
  0.85-0.95  Direct real-time USGS sensor reading exists for this parameter.
  0.65-0.84  One upstream agent provided recent data (under 24 hours old), single hop.
  0.45-0.64  Upstream data is older, or two or more hops from the nearest gauge.
             Also use this range when a parameter is inferred indirectly from others.
  0.20-0.44  No sensor or upstream data at all — inference from geology or land use only.
  Lower further if: conflicting sources, recent extreme weather, very sparse history.

=== ANALYSIS FORMAT ===
Write "analysis" as ONE plain string with these five labeled sections:

WHAT WE KNOW: List every data source used. Cite specific numbers and dates.

WHY THE ESTIMATE: Step-by-step reasoning from raw data to your estimate.
  Explain the physical or chemical process in plain language.
  Define technical terms in parentheses on first use.
  Example: "Sandstone terrain buffers (resists) pH change, keeping pH near 7.5."

WHAT IT MEANS: Translate each value into everyday language a non-scientist understands.
  Example: "pH 7.6 is slightly alkaline, similar to tap water. Safe for most fish."

CONFIDENCE REASON: Explain your confidence number in plain language.
  Example: "Confidence 0.88 because we have 28 direct sensor readings with zero
  variation across the full 15-day window, giving high certainty."

KEY TAKEAWAY: 1-2 sentences summarizing the finding for a water board member
  with no science background.

Length: 150-350 words.

=== FACILITY TYPES REFERENCE ===
{FACILITY_INFO_STR}
"""

# ---------------------------------------------------------------------------
# Prompt 1: USGS sensor node — Gap Filling mode
# Used when river.has_usgs_site == True
# Template variables injected from nodes.py:
#   comid, FACILITY_INFO_STR, start_date, end_date, task_description,
#   site_id, target_params_block, sensor_data_block, flow_block,
#   env_block, history_block
# ---------------------------------------------------------------------------
HYDRO_USGS_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SCHEMA_SYSTEM),
    ("user", """\
[TIME RANGE]: {start_date} to {end_date}
[TASK]: {task_description}

[TARGET PARAMETERS] — output exactly one JSON entry per parameter listed here:
{target_params_block}

--- SENSOR DATA (USGS site {site_id}) ---
{sensor_data_block}

--- DISCHARGE (USGS site {site_id}) ---
{flow_block}

--- ANOMALY SUMMARY (sub-daily windows flagged by automated detection) ---
{daily_summary_block}

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] no null values were fabricated — null means truly no data
[ ] "detailed_results" has one entry per TARGET PARAMETER with one daily_results entry per day
[ ] "explanation" block is present with at least one primary_driver
[ ] anomaly possible_cause filled for every flagged window in ANOMALY SUMMARY
[ ] "flow_results" is present with one daily_results entry per day matching the time range
"""),
])

# ---------------------------------------------------------------------------
# Prompt 2: Inference-only node (no local USGS sensor) — Gap Filling mode
# Used when river.has_usgs_site == False
# Template variables injected from nodes.py:
#   comid, FACILITY_INFO_STR, start_date, end_date, task_description,
#   target_params_block, upstream_block, env_block, history_block
# ---------------------------------------------------------------------------
HYDRO_INFER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SCHEMA_SYSTEM),
    ("user", """\
[TIME RANGE]: {start_date} to {end_date}
[TASK]: {task_description}

[TARGET PARAMETERS] — output exactly one JSON entry per parameter listed here:
{target_params_block}

--- UPSTREAM SEGMENTS (rivers flowing directly INTO this segment) ---
{upstream_block}

[INFERENCE INSTRUCTIONS]
You have no local sensors. Derive values from upstream using these principles:
- Mass balance: concentrations mix proportionally to flow volumes.
- Dilution: higher total flow lowers concentrations; low flow means less mixing.
- Attenuation: some parameters (e.g. BOD) decrease with travel distance due to
  natural processes (biodegradation, settling). pH tends to stay stable unless
  geology or facilities alter it.
- If a parameter is null in ALL upstream segments, your estimate_value must also
  be null. Do not invent a number.
- If multiple upstreams exist, explain how you weighted them.

--- ANOMALY SUMMARY (sub-daily windows flagged by automated detection) ---
{daily_summary_block}

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] no null values were fabricated — null means truly no upstream data exists
[ ] "detailed_results" has one entry per TARGET PARAMETER with one daily_results entry per day
[ ] "explanation" block is present with at least one primary_driver
[ ] "flow_results" is present, derived from upstream flow_results using mass balance
"""),
])


# ---------------------------------------------------------------------------
# Prompt 3: USGS sensor node — PREDICTION mode
# Used when river.has_usgs_site == True AND mode == "prediction"
# Template variables injected from nodes.py:
#   comid, FACILITY_INFO_STR, start_date, end_date, task_description,
#   site_id, target_params_block, sensor_data_block, flow_block,
#   forecast_block, upstream_block, env_block, history_block
# ---------------------------------------------------------------------------
HYDRO_USGS_PREDICTION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SCHEMA_SYSTEM),
    ("user", """\
[TIME RANGE]: {start_date} to {end_date}  [MODE: PREDICTION — all dates are in the future]
[TASK]: {task_description}

[TARGET PARAMETERS] — output exactly one JSON entry per parameter listed here:
{target_params_block}

--- RECENT OBSERVED BASELINE (last 48h, from USGS site {site_id}) ---
{sensor_data_block}

--- RECENT DISCHARGE BASELINE (last 48h, from USGS site {site_id}) ---
{flow_block}

[BASELINE INSTRUCTIONS]
These are the most recent real sensor readings before the forecast window begins.
Use them as the starting point (Day 0 anchor) for your predictions.
If baseline pH is 7.4, your Day 1 prediction should begin near 7.4 and
deviate only if the forecast data gives a specific reason (e.g. heavy rain).

--- WEATHER FORECAST (NOAA) ---
{forecast_block}

[FORECAST FIELD GUIDE]
  temp_c:          air temperature in Celsius — warmer water reduces dissolved oxygen solubility.
  precip_prob_pct: probability (0-100%) that precipitation occurs in this window.
  precip_mm:       quantitative precipitation forecast in real mm — use this for runoff estimates.
  Use BOTH fields together: high precip_prob_pct + high precip_mm = confident heavy rain event.
  High precip_prob_pct + low precip_mm = likely light drizzle, minimal runoff effect.

--- MOST RECENT UPSTREAM INFERENCES ---
{upstream_block}

[PREDICTION INSTRUCTIONS]
You have no future sensor data. Derive day-by-day predictions using:
- Forecast precipitation and temperature as the primary environmental drivers.
- Upstream inferences as the baseline concentration entering this segment.
- Physical reasoning: rain causes surface runoff (lowers pH, raises turbidity);
  warm temperatures reduce dissolved oxygen; low flow concentrates pollutants.
- representative_value for each day is your prediction, not an observation.
- Confidence should be lower than gap-filling (typically 0.30-0.65).
  Lower further on days with high precipitation uncertainty.
- reasoning_chain MUST describe expected day-by-day changes and their causes.
  Example: "Day 1-2: stable pH ~7.4 expected (dry forecast, steady upstream input).
            Day 3: 68% PoP + 4.2mm QPF suggests surface runoff; pH may drop to ~7.0."
- anomalies list in daily_results must be empty [] for all future days.

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] "detailed_results" has one entry per TARGET PARAMETER with one daily_results entry per day
[ ] each daily_results entry has anomalies: [] (empty — no anomaly detection for future dates)
[ ] "explanation" block is present; reasoning_chain describes day-by-day temporal changes
[ ] no null values were fabricated — null means truly no upstream data exists
[ ] Day 1 prediction is anchored to RECENT OBSERVED BASELINE values, not invented
[ ] "flow_results" is present with one daily_results entry per future day; anomalies: []
"""),
])

# ---------------------------------------------------------------------------
# Prompt 4: Inference-only node — PREDICTION mode
# Used when river.has_usgs_site == False AND mode == "prediction"
# Template variables injected from nodes.py:
#   comid, FACILITY_INFO_STR, start_date, end_date, task_description,
#   target_params_block, forecast_block, upstream_block,
#   env_block, history_block
# ---------------------------------------------------------------------------
HYDRO_INFER_PREDICTION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SCHEMA_SYSTEM),
    ("user", """\
[TIME RANGE]: {start_date} to {end_date}  [MODE: PREDICTION — all dates are in the future]
[TASK]: {task_description}

[TARGET PARAMETERS] — output exactly one JSON entry per parameter listed here:
{target_params_block}

--- WEATHER FORECAST (NOAA) ---
{forecast_block}

[FORECAST FIELD GUIDE]
  temp_c:          air temperature in Celsius — warmer water reduces dissolved oxygen solubility.
  precip_prob_pct: probability (0-100%) that precipitation occurs in this window.
  precip_mm:       quantitative precipitation forecast in real mm — use this for runoff estimates.
  Use BOTH fields together: high precip_prob_pct + high precip_mm = confident heavy rain event.
  High precip_prob_pct + low precip_mm = likely light drizzle, minimal runoff effect.

--- UPSTREAM SEGMENTS (rivers flowing directly INTO this segment) ---
{upstream_block}

[PREDICTION INSTRUCTIONS]
You have no local sensors and no future observations. Derive day-by-day predictions using:
- Upstream predicted values as the baseline concentration entering this segment.
- Forecast precipitation and temperature as environmental modifiers.
- Mass balance and attenuation principles (same as gap-filling, but applied to
  predicted upstream values rather than observed ones).
- If ALL upstream segments have null values for a parameter, your estimate_value
  must also be null. Do not invent a number.
- Confidence should be lower than gap-filling (typically 0.20-0.55).
  Lower further when upstream data itself is already a prediction (multi-hop uncertainty).
- reasoning_chain MUST describe expected day-by-day changes and their causes.
- anomalies list in daily_results must be empty [] for all future days.

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] "detailed_results" has one entry per TARGET PARAMETER with one daily_results entry per day
[ ] each daily_results entry has anomalies: [] (empty — no anomaly detection for future dates)
[ ] "explanation" block is present; reasoning_chain describes day-by-day temporal changes
[ ] no null values were fabricated — null means truly no upstream data exists
[ ] "flow_results" is present, derived from upstream flow_results; anomalies: []
"""),
])



# ---------------------------------------------------------------------------
# Intent parsing prompt (used by AskerService — unchanged)
# ---------------------------------------------------------------------------
DATA_NEEDS_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """\
You are a Water Quality Data Expert Agent.
Parse the user query into structured JSON data requirements.

Steps:
1. Location: extract coordinates as [lon, lat] if given, or a place name string.
   Return null if unclear.
2. Parameters: identify water quality metrics needed.
   Use official USGS characteristic names such as "pH", "Dissolved oxygen",
   "Temperature, water", "Specific conductance", "Alkalinity".

STRICT OUTPUT RULE: Return ONLY a valid JSON object. No preamble, no markdown.
"""),
    ("user", """\
User Query: "{user_query}"

Return exactly this JSON structure:
{{
    "location": [lon, lat] or "place name string" or null,
    "characteristic_names": ["USGS characteristic name", ...]
}}
"""),
])