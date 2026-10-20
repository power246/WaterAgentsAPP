"""Read-only checks for the local, saved Water Agents runtime regression."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import httpx

BASE = "http://127.0.0.1:8000/api/analysis/runs"
PH_KEY = {"code_system": "usgs", "code": "00400"}
TARGET = "1777642"


def get(client: httpx.Client, url: str) -> dict:
    response = client.get(url, params={"view": "history"})
    response.raise_for_status()
    body = response.json()
    assert not body.get("issues"), body.get("issues")
    return body


def content(client: httpx.Client, run_id: str, item_id: str) -> tuple[str, dict]:
    envelope = get(client, f"{BASE}/{run_id}/transcript/{item_id}/content")
    return envelope["content_type"], json.loads(envelope["text"])


def main(run_id: str, *, expect_provider_flagged: bool = False) -> None:
    with httpx.Client(timeout=240, trust_env=False) as client:
        detail = get(client, f"{BASE}/{run_id}")
        run = detail["run"]
        assert detail["source"] == "postgresql"
        assert run["status"] == "completed", run["status"]
        summary = run["result_summary"]
        assert summary["summary_status"] == "completed", summary
        sessions = [s for s in detail["sessions"] if s["agent_role"] == "waterbody"]
        assert len(sessions) == 7, len(sessions)
        assert all(s["status"] == "completed" for s in sessions)

        content_type, top = content(client, run_id, run["result_ref"]["item_id"])
        assert content_type == "topology_run_result"
        assert top["target_comid"] == TARGET
        assert len(top["node_outcomes"]) == 7
        assert all(
            n["dependency_status"] == "usable" and n["session_status"] == "completed"
            for n in top["node_outcomes"]
        )

        target_session = next(s for s in sessions if s["target_comid"] == TARGET)
        result_item_id = target_session["result_ref"]["item_id"]
        content_type, result = content(client, run_id, result_item_id)
        assert content_type == "inference_result"
        assert result["target_comid"] == TARGET
        ph_series = next(s for s in result["series"] if s["parameter"]["key"] == PH_KEY)
        points = ph_series["points"]
        assert len(points) == 4, len(points)
        referenced_ids = {r["evidence_id"] for r in result["evidence_chain"]["references"]}
        for previous, current in pairwise(points):
            assert datetime.fromisoformat(previous["end_time"]) == datetime.fromisoformat(current["start_time"])
        for point in points:
            start = datetime.fromisoformat(point["start_time"])
            end = datetime.fromisoformat(point["end_time"])
            assert end - start == timedelta(hours=6)
            assert point["outcome_status"] == "quantified", point
            assert point["representative_value"] is not None, point
            assert Decimal(point["value_lower"]) <= Decimal(point["representative_value"]) <= Decimal(point["value_upper"])
            assert point["source"] == "inferred"
            assert point["evidence_refs"] and set(point["evidence_refs"]) <= referenced_ids
            assert point["measured_anchors"]
            if expect_provider_flagged:
                assert "provider_flagged" in point["quality_flags"], point
            for anchor in point["measured_anchors"]:
                assert anchor["parameter"] == PH_KEY
                assert anchor["path_comids"][0] == anchor["origin_comid"]
                assert anchor["path_comids"][-1] == TARGET
                assert anchor["source_record_refs"]

        transcript = get(client, f"{BASE}/{run_id}/sessions/{target_session['session_id']}/transcript")
        fragments: dict[str, dict] = {}
        for item in transcript["items"]:
            if item["content_type"] != "evidence_fragment":
                continue
            if item["content"]["kind"] == "inline":
                body = item["content"]["body"]
            else:
                _, body = content(client, run_id, item["item_id"])
            fragments[body["evidence_id"]] = body
        for point in points:
            assert set(point["evidence_refs"]) <= fragments.keys()
            assert all(fragments[eid]["target_comid"] == TARGET for eid in point["evidence_refs"])

        overlay = get(client, f"{BASE}/{run_id}/transcript/{result_item_id}/anchor-distance-estimates")
        overlay_by_anchor = {(e["cell_id"], e["anchor_index"]): e for e in overlay["estimates"]}
        missing_distances = 0
        for point in points:
            for index, anchor in enumerate(point["measured_anchors"]):
                if anchor["distance_km"] is None:
                    missing_distances += 1
                    estimate = overlay_by_anchor[(point["cell_id"], index)]
                    assert estimate["distance_basis"] == "mapped_channel_estimate"
                    assert Decimal(str(estimate["distance_km"])) >= 0

        print(json.dumps({
            "run_id": run_id,
            "status": run["status"],
            "waterbodies": len(sessions),
            "outcomes": summary["outcome_counts"],
            "target_comid": TARGET,
            "target_ph": [{
                "start": p["start_time"],
                "value": p["representative_value"],
                "range": [p["value_lower"], p["value_upper"]],
                "quality_flags": p["quality_flags"],
                "anchors": len(p["measured_anchors"]),
            } for p in points],
            "target_fragments": len(fragments),
            "distance_overlays": len(overlay_by_anchor),
            "missing_raw_distances": missing_distances,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], expect_provider_flagged="--expect-provider-flagged" in sys.argv[2:])
