"""
test_sprint5.py — Sprint 5/6 prompt validation tests

Run from the project root:
    python test_sprint5.py

All output is printed to terminal AND saved to test_sprint5_output.txt
in the project root so that nothing is lost when the terminal scrolls.
"""

import asyncio
import re
import sys
import logging
from datetime import date, timedelta
from io import StringIO
from typing import Optional

from app.services.asker_service import AskerService
from app.agents.graph import create_hydrological_graph
from app.core.env_manager import EnvManager
from app.core.config import settings
from app.core.http_client import AsyncHttpClient
from app.models.river import RiverAgentData, HydrologyInference

# ---------------------------------------------------------------------------
# Output capture: every print() goes to both terminal and the txt file buffer
# ---------------------------------------------------------------------------
OUTPUT_FILE = "test_sprint5_output.txt"

class _Tee:
    """Write to both the original stream and a StringIO buffer."""
    def __init__(self, original):
        self._original = original
        self._buf = StringIO()

    def write(self, data):
        self._original.write(data)
        self._buf.write(data)

    def flush(self):
        self._original.flush()
    
    def isatty(self):
        return False

    def get_content(self) -> str:
        return self._buf.getvalue()

_tee = _Tee(sys.stdout)
sys.stdout = _tee

# Suppress noisy library logs; only WARNING+ goes to terminal/file
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
COORDS = "Compute the pH at coordinates: -92.414165705, 46.682323895"
MODEL  = "llama3:8b"
TODAY  = date.today()

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_inference_output(
    output: dict,
    expected_days: int,
    mode: str,
    case_label: str,
) -> bool:
    """
    Run V1-V9 checks. Print PASS/FAIL for each.
    Returns True only when every check passes.
    """
    all_pass = True

    def check(tag: str, condition: bool, detail: str = ""):
        nonlocal all_pass
        status = "PASS" if condition else "FAIL"
        if not condition:
            all_pass = False
        suffix = f"  ({detail})" if detail else ""
        print(f"  [{status}] {tag}{suffix}")

    if not output:
        print("  [FAIL] All checks — output is None or empty")
        return False

    # V1: parseable by Pydantic
    try:
        parsed = HydrologyInference.model_validate(output)
        check("V1 valid HydrologyInference schema", True)
    except Exception as exc:
        check("V1 valid HydrologyInference schema", False, str(exc))
        print(f"  [INFO] Raw output keys: {list(output.keys())}")
        return False

    # V2: explanation present
    check("V2 explanation field present", parsed.explanation is not None)

    if parsed.explanation:
        # V3: primary_drivers non-empty
        check(
            "V3 primary_drivers non-empty",
            len(parsed.explanation.primary_drivers) > 0,
            f"got {len(parsed.explanation.primary_drivers)} drivers",
        )

        # V9: prediction/mixed — reasoning_chain mentions temporal changes
        if mode in ("prediction", "mixed"):
            chain = parsed.explanation.reasoning_chain.lower()
            has_temporal = bool(
                re.search(r"day\s*\d", chain) or
                re.search(r"\d{4}-\d{2}-\d{2}", chain) or
                any(kw in chain for kw in ["forecast", "tomorrow", "next", "coming"])
            )
            v9_status = "PASS" if has_temporal else "WARN"
            print(
                f"  [{v9_status}] V9 reasoning_chain has temporal language "
                f"(prediction/mixed)"
                + (f"  (keywords not found — model may still be reasoning correctly)"
                   if not has_temporal else "")
            )
            if not has_temporal:
                print(f"  [DEBUG] reasoning_chain preview: "
                      f"{parsed.explanation.reasoning_chain[:300]}")
            # Do NOT set all_pass = False for V9 — this is informational only
    else:
        print("  [DEBUG] explanation is None — skipping V3/V9")

    # V4: detailed_results non-empty
    check("V4 detailed_results non-empty", len(parsed.detailed_results) > 0)

    if not parsed.detailed_results:
        print(f"  [DEBUG] output keys present: {list(output.keys())}")
        return all_pass

    for param_result in parsed.detailed_results:
        # V5: each param has at least one daily entry
        check(
            f"V5 daily_results non-empty for '{param_result.name}'",
            len(param_result.daily_results) > 0,
        )

        # V6: day count matches expected
        actual_days = len(param_result.daily_results)
        check(
            f"V6 day count for '{param_result.name}'",
            actual_days == expected_days,
            f"expected {expected_days}, got {actual_days}",
        )
        if actual_days != expected_days:
            # Show the actual dates so we can see what went wrong
            dates_found = [d.date for d in param_result.daily_results]
            print(f"  [DEBUG] dates in output: {dates_found}")

        for day_result in param_result.daily_results:
            if mode == "prediction":
                # V8: prediction anomalies must be empty
                check(
                    f"V8 anomalies empty on {day_result.date} (prediction)",
                    len(day_result.anomalies) == 0,
                    f"got {len(day_result.anomalies)} anomalies",
                )
            else:
                # V7: gap-filling anomalies field exists (content may vary)
                check(
                    f"V7 anomalies field present on {day_result.date}",
                    day_result.anomalies is not None,
                )
    
    # V10: flow_results present (soft check — warn only, not fail)
    # Some nodes may genuinely have no discharge data.
    flow_results = output.get("flow_results")
    if flow_results:
        print(f"  [PASS] V10 flow_results present")

        # V11: flow_results day count matches expected
        flow_daily = []
        if isinstance(flow_results, dict):
            flow_daily = flow_results.get("daily_results", [])
        elif hasattr(flow_results, "daily_results"):
            flow_daily = flow_results.daily_results

        actual_flow_days = len(flow_daily)
        flow_unit = (
            flow_results.get("unit") if isinstance(flow_results, dict)
            else getattr(flow_results, "unit", "?")
        )
        check(
            f"V11 flow_results day count ({flow_unit})",
            actual_flow_days == expected_days,
            f"expected {expected_days}, got {actual_flow_days}",
        )
        if actual_flow_days != expected_days:
            flow_dates = [
                (d.get("date") if isinstance(d, dict) else getattr(d, "date", "?"))
                for d in flow_daily
            ]
            print(f"  [DEBUG] flow_results dates: {flow_dates}")
    else:
        print(f"  [WARN] V10 flow_results absent — node may have no discharge data")
        # Do NOT set all_pass = False — absence is acceptable

    return all_pass


async def run_single_case(
    label: str,
    query: str,
    start_date: str,
    end_date: str,
    expected_days: int,
    mode: str,
) -> bool:
    """
    Build context, compile graph, run inference, validate output.
    Returns True if all checks pass.
    """
    print(f"\n{'='*70}")
    print(f"TEST CASE: {label}")
    print(f"  Query:      {query}")
    print(f"  Dates:      {start_date} -> {end_date}")
    print(f"  Mode:       {mode}  |  expected_days={expected_days}")
    print(f"{'='*70}")

    # Step 1: build context
    try:
        context = await AskerService.prepare_inference_context(
            user_query=query,
            selected_model=MODEL,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        print(f"  [FAIL] AskerService raised an exception: {exc}")
        return False

    if not context:
        print("  [FAIL] AskerService returned None")
        return False

    print(f"  Context OK: {len(context['global_data'])} agents, "
          f"inlets={context['inlet_comids']}, "
          f"mode={context.get('mode')}, "
          f"time_mode={context.get('time_mode')}")
    print(f"  Segments:   {context.get('segments')}")

    agents_dict  = {cid: RiverAgentData(**data) for cid, data in context["global_data"].items()}
    target_id    = context["start_comid"]
    inlet_comids = context["inlet_comids"]

    # Step 2: build graph
    try:
        graph_app = create_hydrological_graph(agents_dict, entry_points=inlet_comids)
    except Exception as exc:
        print(f"  [FAIL] Graph construction failed: {exc}")
        return False

    initial_state = {
        "task":            query,
        "current_params":  context["current_params"],
        "facility_types":  settings.FACILITY_TYPES,
        "start_date":      context["start_date"],
        "end_date":        context["end_date"],
        "global_data":     context["global_data"],
        "inlet_comids":    inlet_comids,
        "selected_model":  MODEL,
        "mode":            context.get("mode", mode),
        "time_mode":       context.get("time_mode", "date_range"),
        "segments":        context.get("segments", []),
        "resample_auto":   True,
        "resample_freq_h": 6,
    }

    # Step 3: run graph
    try:
        final_state = await graph_app.ainvoke(initial_state)
    except Exception as exc:
        import traceback
        print(f"  [FAIL] Graph execution raised an exception:")
        print(f"  {type(exc).__name__}: {exc}")
        print("  --- Traceback ---")
        traceback.print_exc(file=sys.stdout)
        print("  --- End Traceback ---")
        return False

    # Step 4: extract target agent output
    target_agent = final_state["global_data"].get(target_id)
    if not target_agent:
        print(f"  [FAIL] No data for target COMID {target_id}")
        print(f"  [DEBUG] Available COMIDs: {list(final_state['global_data'].keys())}")
        return False

    output = target_agent.get("output")
    if not output:
        print("  [WARN] Target agent ran but produced no output")
        print(f"  [DEBUG] target_agent keys: {list(target_agent.keys())}")
        return False

    # Print a longer preview so partial failures are easier to diagnose
    print(f"  Output preview (first 300 chars): {str(output)[:300]}")

    passed = validate_inference_output(output, expected_days, mode, label)
    print(f"\n  RESULT: {'ALL PASS' if passed else 'SOME CHECKS FAILED'}")
    return passed


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def main():
    EnvManager.reset_workspace()
    print(f"Test run date: {TODAY.isoformat()}")
    print(f"Model: {MODEL}")

    results = {}

    
    # C1: Gap-filling, date range, 15 days
    results["C1_gap_filling_range"] = await run_single_case(
        label         = "C1 — Gap-filling, date range, 15 days",
        query         = COORDS,
        start_date    = "2026-01-01",
        end_date      = "2026-01-15",
        expected_days = 15,
        mode          = "gap_filling",
    )

    # C2: Gap-filling, single day
    results["C2_gap_filling_single_day"] = await run_single_case(
        label         = "C2 — Gap-filling, single day",
        query         = COORDS,
        start_date    = "2026-01-10",
        end_date      = "2026-01-10",
        expected_days = 1,
        mode          = "gap_filling",
    )

    # C3: Prediction, date range, 5 days
    future_start = (TODAY + timedelta(days=1)).isoformat()
    future_end   = (TODAY + timedelta(days=5)).isoformat()
    results["C3_prediction_range"] = await run_single_case(
        label         = "C3 — Prediction, date range, 5 days",
        query         = COORDS,
        start_date    = future_start,
        end_date      = future_end,
        expected_days = 5,
        mode          = "prediction",
    )
    
    # C4: Prediction, single day
    future_single = (TODAY + timedelta(days=3)).isoformat()
    results["C4_prediction_single_day"] = await run_single_case(
        label         = "C4 — Prediction, single day",
        query         = COORDS,
        start_date    = future_single,
        end_date      = future_single,
        expected_days = 1,
        mode          = "prediction",
    )
    
    # C5: Mixed range (straddles today)
    mixed_start = (TODAY - timedelta(days=3)).isoformat()
    mixed_end   = (TODAY + timedelta(days=3)).isoformat()
    results["C5_mixed_range"] = await run_single_case(
        label         = "C5 — Mixed range (historical + prediction), 7 days",
        query         = COORDS,
        start_date    = mixed_start,
        end_date      = mixed_end,
        expected_days = 7,
        mode          = "mixed",
    )

    # C6: Multi-parameter gap-filling
    results["C6_multi_param"] = await run_single_case(
        label         = "C6 — Gap-filling, multiple parameters (pH + DO)",
        query         = "Compute pH and dissolved oxygen at coordinates: -92.414165705, 46.682323895",
        start_date    = "2026-01-01",
        end_date      = "2026-01-05",
        expected_days = 5,
        mode          = "gap_filling",
    )
    
    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    for case, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}]  {case}")
    total  = len(results)
    n_pass = sum(1 for v in results.values() if v)
    print(f"\n  {n_pass}/{total} cases passed")
    print(f"{'='*70}\n")

    await AsyncHttpClient.close_client()

    # Restore stdout and write everything to file
    sys.stdout = _tee._original
    content = _tee.get_content()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Full output saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())