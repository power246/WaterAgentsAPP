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

=== REQUIRED OUTPUT STRUCTURE ===
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
  "estimated_flow": null,
  "confidence": 0.87,
  "analysis": "WHAT WE KNOW: ... WHY: ... MEANING: ... CONFIDENCE REASON: ... KEY TAKEAWAY: ..."
}}

=== HARD RULES (violations break the system) ===
R1. "parameters" is a JSON ARRAY [ ] — never an object {{ }}.
R2. Each parameter entry has EXACTLY these 4 keys: "name", "code", "estimate_value", "unit".
    Do NOT add "COMID", "parameter_name", "confidence", "analysis", or any other key.
R3. "confidence" and "analysis" go at the TOP LEVEL — never inside a parameter entry.
R4. Include EVERY parameter listed in [TARGET PARAMETERS]. If no data, use null.
R5. Do NOT fabricate numbers. Null is the correct and honest answer when data is absent.

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
# Prompt 1: USGS sensor node
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

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] no null values were fabricated — null means truly no data
"""),
])

# ---------------------------------------------------------------------------
# Prompt 2: Inference-only node (no local USGS sensor)
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

--- NEARBY FACILITIES & WEATHER ---
{env_block}

--- YOUR PAST 2 INFERENCES (reference only, do not copy structure) ---
{history_block}

Now output your JSON. Checklist before you write:
[ ] "parameters" is an ARRAY [ ] with one entry per TARGET PARAMETER above
[ ] each entry has only: name, code, estimate_value, unit
[ ] "confidence" and "analysis" are at the top level
[ ] no null values were fabricated — null means truly no upstream data exists
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
3. Time range: suggest a logical range, maximum 1 month.

STRICT OUTPUT RULE: Return ONLY a valid JSON object. No preamble, no markdown.
"""),
    ("user", """\
User Query: "{user_query}"

Return exactly this JSON structure:
{{
    "location": [lon, lat] or "place name string" or null,
    "characteristic_names": ["USGS characteristic name", ...],
    "time_range": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}
}}
"""),
])