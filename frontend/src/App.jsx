import { useState } from "react";
import MapView from "./components/MapView";
import ControlPanel from "./components/ControlPanel";
import RightPanel from "./components/RightPanel";
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

  // Overall pipeline status: idle | running | done | error
  const [status, setStatus] = useState("idle");

  // ── Helpers ────────────────────────────────────────────────────────────────

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
  };

  /** User triggered a new analysis run from the ControlPanel */
  const handleStartAnalysis = ({ query, settings }) => {
    setLogs([]);
    setTopology({ nodes: [], edges: [] });
    setActiveComid(null);
    setSelectedAgent(null);
    setStatus("running");

    appendLog({
      type: "info",
      message: `Starting analysis... ${settings.provider}/${settings.model_name}`,
    });

    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/hydro-analysis`);

    ws.onopen = () => {
      appendLog({ type: "info", message: "WebSocket connected" });
      // Send query + all runtime settings to the backend
      ws.send(JSON.stringify({
        query,
        model_name:         settings.model_name,
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

      // Suppress ping from logs – it is just a keepalive, not user-visible
      if (data.type === "ping") return;

      appendLog(data);

      switch (data.type) {
        // "preparing" events carry a human-readable message shown in the log.
        // No map state changes needed at this stage.
        case "preparing":
          break;

        case "topology":
          setTopology({ nodes: data.nodes, edges: data.edges });
          break;

        case "agent_start":
          setActiveComid(data.comid);
          break;

        case "agent_done":
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
                    result:         data.result,
                    prompt_used:    data.prompt_used,
                    llm_raw_output: data.llm_raw_output,
                    done: true,
                  }
                : n
            ),
          }));

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
        />
      </aside>

      <main className="panel-map">
        <MapView
          onMapClick={handleMapClick}
          onNodeClick={handleNodeClick}
          selectedPoint={selectedPoint}
          topology={topology}
          activeComid={activeComid}
        />
      </main>

      <aside className="panel-right">
        <RightPanel
          logs={logs}
          status={status}
          selectedAgent={selectedAgent}
          onSelectAgent={setSelectedAgent}
        />
      </aside>
    </div>
  );
}
