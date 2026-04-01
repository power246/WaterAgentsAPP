import { useState, useEffect, useRef } from "react";
import MapView from "./components/MapView";
import ControlPanel from "./components/ControlPanel";
import RightPanel from "./components/RightPanel";
import TimeSeriesChart from "./components/TimeSeriesChart";
import "./App.css";

/**
 * Root application component.
 * Layout: left control panel | center map | right log + inspector panel
 */
export default function App() {
  // Coordinates selected by clicking the map
  const [selectedPoint, setSelectedPoint] = useState(null);

  // Live log entries (every WebSocket event appended here)
  const [logs, setLogs] = useState([]);

  // Map topology: nodes and edges received from the backend
  const [topology, setTopology] = useState({ nodes: [], edges: [] });

  // comid of the agent currently running inference (drives pulse animation)
  const [activeComid, setActiveComid] = useState(null);

  // Agent data shown in the right-panel Inspector tab
  // Shape: { comid, lat, lon, has_usgs_site, site_id, result, prompt_used, llm_raw_output }
  const [selectedAgent, setSelectedAgent] = useState(null);

  // Overall pipeline status: idle | running | stopping | done | error | interrupted
  // "stopping": stop signal sent, waiting for backend_idle confirmation
  const [status, setStatus] = useState("idle");

  // Ref to the live WebSocket so handleStopAnalysis can send a stop message
  // without needing to be defined inside handleStartAnalysis.
  const wsRef = useRef(null);

  // Ref to the 10-second safety timeout used during "stopping" state.
  // If backend_idle is not received in time, we force-unlock the Start button.
  const stopTimeoutRef = useRef(null);

  // Google Maps API key fetched from the backend key store.
  // If null, MapView falls back to OpenStreetMap (no key required).
  const [googleMapsKey, setGoogleMapsKey] = useState(null);

  // Segments from the most recent query, used by TimeSeriesChart to accurately
  // classify historical vs prediction days (avoids timezone-related date bugs).
  const [querySegments, setQuerySegments] = useState([]);

  // Time-series chart data built from agent_done events.
  // Shape: { comid -> { paramName -> [{date, value, confidence, hasAnomaly, unit}] } }
  const [timeSeriesData,    setTimeSeriesData]    = useState({});

  // comid of the node whose chart is currently open (null = hidden)
  const [selectedChartComid, setSelectedChartComid] = useState(null);

  // Flat list of anomaly windows collected from all completed agents.
  // Each entry is tagged with comid for grouping in the AnomalyTab.
  // Shape: [{comid, time_window, parameter, observed_value, daily_mean, deviation_pct, possible_cause}, ...]
  const [anomalies, setAnomalies] = useState([]);

  // Fetch the Google Maps key once on mount
  useEffect(() => {
    fetch("/api/keys/map-key")
      .then((r) => r.json())
      .then((data) => {
        if (data.key) setGoogleMapsKey(data.key);
      })
      .catch(() => { /* silently fall back to OSM */ });
  }, []);

  // ── Helpers ────────────────────────────────────────────────────────────────

  /**
   * Parse an agent's output into the flat time-series structure used by TimeSeriesChart.
   * Handles both detailed_results (per-parameter daily values) and
   * flow_results (discharge daily values).
   */
  const _parseTimeSeries = (result) => {
    const data = {};
    if (!result) return data;

    // Parse each parameter's daily breakdown
    for (const param of result.detailed_results || []) {
      data[param.name] = (param.daily_results || []).map((d) => ({
        date:       d.date,
        value:      d.representative_value,
        confidence: d.confidence,
        hasAnomaly: (d.anomalies || []).length > 0,
        unit:       d.unit || param.unit || "",
      }));
    }

    // Parse discharge time-series as an additional pseudo-parameter
    const flow = result.flow_results;
    if (flow) {
      const name = flow.name || "Discharge";
      data[name] = (flow.daily_results || []).map((d) => ({
        date:       d.date,
        value:      d.representative_value,
        confidence: d.confidence,
        hasAnomaly: (d.anomalies || []).length > 0,
        unit:       d.unit || flow.unit || "ft3/s",
      }));
    }

    return data;
  };

  const appendLog = (event) => {
    setLogs((prev) => [
      ...prev,
      { ...event, timestamp: new Date().toLocaleTimeString() },
    ]);
  };

  // ── Event handlers ─────────────────────────────────────────────────────────

  /** User clicked the map background */
  const handleMapClick = (latlng) => {
    setSelectedPoint(latlng);
    appendLog({
      type: "info",
      message: `Selected coordinates: ${latlng.lat.toFixed(6)}, ${latlng.lng.toFixed(6)}`,
    });
  };

  /**
   * User clicked an Agent node on the map → open it in the Inspector.
   * The node object already contains result/prompt_used/llm_raw_output if
   * inference has completed (merged in during agent_done handling).
   */
  const handleNodeClick = (node) => {
    setSelectedAgent(node);
    appendLog({
      type: "info",
      message: `Selected node: ${node.comid}${node.result ? " (done)" : " (pending)"}`,
    });
    // Open time-series chart if the node has inference results
    if (node.result) {
      setSelectedChartComid(node.comid);
      // Ensure the chart data is populated (may not be present if this is
      // a page reload with stale topology, or the node is from a cached run)
      setTimeSeriesData((prev) => {
        if (prev[node.comid]) return prev;  // already parsed
        const parsed = _parseTimeSeries(node.result);
        return { ...prev, [node.comid]: parsed };
      });
    } else {
      setSelectedChartComid(null);
    }
  };

  /**
   * Called when backend_idle arrives OR the 10-second safety timeout fires.
   * Tears down the WebSocket and transitions to "interrupted".
   * Keeps handlers alive during the "stopping" window so backend_idle can arrive.
   */
  const _finalizeStop = () => {
    if (stopTimeoutRef.current) {
      clearTimeout(stopTimeoutRef.current);
      stopTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.onmessage = null;
      wsRef.current.onclose   = null;
      wsRef.current.onerror   = null;
      try { wsRef.current.close(); } catch (_) {}
      wsRef.current = null;
    }
    setActiveComid(null);
    setStatus("interrupted");
  };

  /**
   * User clicked the Stop button — send a stop signal and enter "stopping" state.
   * The backend cancels all in-flight HTTP tasks and replies with backend_idle
   * once fully clean.  The Start button stays disabled until that arrives.
   */
  const handleStopAnalysis = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "stop" }));
    }
    // Switch to "stopping": Start button disabled, handlers still live to
    // receive the coming backend_idle confirmation.
    setStatus("stopping");

    // Safety fallback: if backend_idle is not received within 10 s
    // (e.g. network issue), force-unlock the Start button anyway.
    if (stopTimeoutRef.current) clearTimeout(stopTimeoutRef.current);
    stopTimeoutRef.current = setTimeout(() => {
      stopTimeoutRef.current = null;
      // Only act if we are still in the "stopping" state
      setStatus((prev) => {
        if (prev === "stopping") {
          _finalizeStop();
        }
        return prev;
      });
    }, 10000);
  };

  /** User triggered a new analysis run from the ControlPanel */
  const handleStartAnalysis = ({ query, startDate, endDate, settings }) => {
    // Cancel any pending stop-fallback timeout from a previous run.
    if (stopTimeoutRef.current) {
      clearTimeout(stopTimeoutRef.current);
      stopTimeoutRef.current = null;
    }
    

    // Tear down any existing WebSocket BEFORE resetting state.
    // If the previous run was stopped early, its backend may still be running
    // and sending events.  Nulling the handlers prevents stale events from
    // the old connection from appearing in this new run's log.
    if (wsRef.current) {
      const stale = wsRef.current;
      wsRef.current = null;
      stale.onmessage = null;  // most important: stops stale events entering state
      stale.onclose   = null;  // prevents "WebSocket disconnected" from old run
      stale.onerror   = null;
      try { stale.close(); } catch (_) {}
    }

    setLogs([]);
    setTopology({ nodes: [], edges: [] });
    setActiveComid(null);
    setSelectedAgent(null);
    setAnomalies([]);
    setSelectedChartComid(null);
    setTimeSeriesData({});
    setStatus("running");

    appendLog({
      type: "info",
      message: `Starting analysis... ${settings.provider}/${settings.model_name}`,
    });

    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/hydro-analysis`);
    wsRef.current = ws;  // store so handleStopAnalysis can reach it

    ws.onopen = () => {
      appendLog({ type: "info", message: "WebSocket connected" });
      // Send query + all runtime settings to the backend
      ws.send(JSON.stringify({
        query,
        model_name:         settings.model_name,
        start_date:         startDate,
        end_date:           endDate,
        provider:           settings.provider,
        api_key:            settings.api_key || null,
        facility_types:     settings.facility_types,
        max_topology_depth: settings.max_topology_depth,
        resample_auto:      settings.resample_auto ?? true,
        resample_freq_h:    settings.resample_freq_h,
        weather_freq_h:     settings.weather_freq_h,
      }));
    };

    ws.onmessage = (msgEvent) => {
      const data = JSON.parse(msgEvent.data);

      // Suppress ping from logs — it is just a keepalive, not user-visible
      if (data.type === "ping") return;

      // backend_idle is a system signal that all backend tasks have stopped.
      // Handle it silently (no log entry) and re-enable the Start button.
      if (data.type === "backend_idle") {
        _finalizeStop();
        return;
      }

      appendLog(data);

      switch (data.type) {
        // "preparing" events carry a human-readable message shown in the log.
        // No map state changes needed at this stage.
        case "preparing":
          break;

        case "topology":
          setTopology({ nodes: data.nodes, edges: data.edges });
          // Store segments for TimeSeriesChart classification
          setQuerySegments(data.segments || []);
          break;

        case "agent_start":
          setActiveComid(data.comid);
          break;

        case "agent_done":
          // Collect anomalies from this agent's output and append to the global list.
          // data.anomalies is the flat list pre-computed in graph.py (Sprint 7).
          if (data.anomalies && data.anomalies.length > 0) {
            setAnomalies((prev) => [...prev, ...data.anomalies]);
          }
          // Merge result + coordinates + LLM I/O into the topology node.
          // IMPORTANT: lat/lon are only known after inference_node runs
          // (RiverManager.get_river_metadata is called there, not during
          // topology building). The topology event sends lat:0 / lon:0 for
          // all nodes, so we must update them here when agent_done arrives.
          setTopology((prev) => ({
            ...prev,
            nodes: prev.nodes.map((n) =>
              n.comid === data.comid
                ? {
                    ...n,
                    // Coordinates: use what the backend just returned; fall
                    // back to whatever was already stored if the new value is 0.
                    lat:            data.lat  || n.lat,
                    lon:            data.lon  || n.lon,
                    site_id:        data.site_id  || n.site_id,
                    result:         data.result,
                    prompt_used:    data.prompt_used,
                    llm_raw_output: data.llm_raw_output,
                    done: true,
                  }
                : n
            ),
          }));

          // Build time-series data from this agent's output for the chart
          if (data.result) {
            const parsed = _parseTimeSeries(data.result);
            setTimeSeriesData((prev) => ({ ...prev, [data.comid]: parsed }));
          }

          // If this node is already open in the Inspector, update it live
          setSelectedAgent((prev) =>
            prev?.comid === data.comid
              ? {
                  ...prev,
                  lat:            data.lat || prev.lat,
                  lon:            data.lon || prev.lon,
                  result:         data.result,
                  prompt_used:    data.prompt_used,
                  llm_raw_output: data.llm_raw_output,
                }
              : prev
          );
          break;

        case "done":
          setStatus("done");
          setActiveComid(null);
          break;

        case "interrupted":
          setStatus("interrupted");
          setActiveComid(null);
          break;

        case "error":
          setStatus("error");
          setActiveComid(null);
          break;

        default:
          break;
      }
    };

    ws.onerror = () => {
      appendLog({ type: "error", message: "WebSocket connection error" });
      setStatus("error");
    };

    ws.onclose = () => {
      wsRef.current = null;  // clear ref once connection is gone
      appendLog({ type: "info", message: "WebSocket disconnected" });
    };
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="app-layout">
      <aside className="panel-left">
        <ControlPanel
          selectedPoint={selectedPoint}
          status={status}
          onStartAnalysis={handleStartAnalysis}
          onStopAnalysis={handleStopAnalysis}
        />
      </aside>

      <main className="panel-map">
        <MapView
          onMapClick={handleMapClick}
          onNodeClick={handleNodeClick}
          selectedPoint={selectedPoint}
          topology={topology}
          activeComid={activeComid}
          googleMapsKey={googleMapsKey}
        />
        {selectedChartComid && timeSeriesData[selectedChartComid] && (
          <TimeSeriesChart
            comid={selectedChartComid}
            data={timeSeriesData[selectedChartComid]}
            segments={querySegments}
            onClose={() => setSelectedChartComid(null)}
          />
        )}
      </main>

      <aside className="panel-right">
        <RightPanel
          logs={logs}
          status={status}
          selectedAgent={selectedAgent}
          onSelectAgent={setSelectedAgent}
          anomalies={anomalies}
        />
      </aside>
    </div>
  );
}
