import { useEffect, useRef, useState } from "react";
import "./RightPanel.css";

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Map event type to icon + CSS class for the log entry border color */
function getEventStyle(type) {
  switch (type) {
    case "preparing":   return { icon: "⏳", cls: "log-preparing" };
    case "topology":    return { icon: "🗺️",  cls: "log-topology" };
    case "agent_start": return { icon: "⚙️",  cls: "log-start" };
    case "agent_done":  return { icon: "✅",  cls: "log-done" };
    case "done":        return { icon: "🏆",  cls: "log-finish" };
    case "interrupted": return { icon: "⏹",   cls: "log-interrupted" };
    case "error":       return { icon: "❌",  cls: "log-error" };
    default:            return { icon: "ℹ️",  cls: "log-info" };
  }
}

/** Format a log event object into a human-readable single line */
function formatLogMessage(log) {
  switch (log.type) {
    case "preparing":
      // Use the backend-supplied message directly; it already says e.g.
      // "Building upstream topology (this may take 1-2 minutes)..."
      return log.message || `Preparing: ${log.step}`;

    case "topology": {
      // Count active vs excluded using the is_excluded flag set by graph.py BFS.
      const allNodes    = log.nodes ?? [];
      const allEdges    = log.edges ?? [];
      const activeNodes = allNodes.filter((n) => !n.is_excluded);
      const activeEdges = allEdges.filter((e) => !e.is_excluded);
      const exclCount   = allNodes.length - activeNodes.length;
      const exStr = exclCount > 0 ? ` (+${exclCount} excluded, no data path)` : "";
      return `Topology ready: ${activeNodes.length} active nodes${exStr}, ${activeEdges.length} active edges`;
    }

    case "agent_start":
      return `Agent [${log.comid}] started inference...`;

    case "agent_done": {
      const r = log.result;
      if (!r) return `Agent [${log.comid}] done (no result)`;
      const params = r.parameters
        ?.map((p) =>
          p.estimate_value != null
            ? `${p.name}: ${p.estimate_value} ${p.unit || ""}`
            : `${p.name}: N/A`
        )
        .join(" | ");
      return `Agent [${log.comid}] -> ${params || "no params"} | confidence: ${((r.confidence || 0) * 100).toFixed(0)}%`;
    }

    case "done":
      return `All agents done. Target: ${log.target_id}`;

    case "interrupted":
      return `Inference interrupted by user. ${log.message || ""}`;

    case "error":
      return `Error: ${log.message}`;

    default:
      return log.message || JSON.stringify(log);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Logs Tab
// ─────────────────────────────────────────────────────────────────────────────

function LogsTab({ logs, onSelectAgent }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  return (
    <div className="tab-content log-list">
      {logs.length === 0 ? (
        <div className="empty-hint">Waiting for analysis to start...</div>
      ) : (
        logs.map((log, i) => {
          const { icon, cls } = getEventStyle(log.type);
          const clickable = log.type === "agent_done";
          return (
            <div
              key={i}
              className={`log-entry ${cls} ${clickable ? "clickable" : ""}`}
              onClick={() => clickable && onSelectAgent(log)}
              title={clickable ? "Click to inspect this agent" : ""}
            >
              <span className="log-time">{log.timestamp}</span>
              <span className="log-icon">{icon}</span>
              <span className="log-msg">{formatLogMessage(log)}</span>

              {log.type === "agent_done" && log.result?.analysis && (
                <details className="log-detail" onClick={(e) => e.stopPropagation()}>
                  <summary>Analysis summary</summary>
                  <p>{log.result.analysis}</p>
                </details>
              )}
            </div>
          );
        })
      )}
      <div ref={bottomRef} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Anomaly Tab
// ─────────────────────────────────────────────────────────────────────────────

/**
 * AnomalyTab
 * Groups all detected anomaly windows by COMID.
 * Each entry shows time_window, parameter, observed vs mean, deviation, and cause.
 */
function AnomalyTab({ anomalies }) {
  if (!anomalies || anomalies.length === 0) {
    return (
      <div className="tab-content inspector-empty">
        <div className="inspector-empty-icon">📊</div>
        <p>No anomalies detected<br />in any completed agent</p>
      </div>
    );
  }

  // Group anomalies by comid
  const grouped = anomalies.reduce((acc, a) => {
    const key = a.comid || "Unknown";
    if (!acc[key]) acc[key] = [];
    acc[key].push(a);
    return acc;
  }, {});

  return (
    <div className="tab-content inspector-content">
      <div className="anomaly-summary">
        {anomalies.length} anomaly window{anomalies.length !== 1 ? "s" : ""} across {Object.keys(grouped).length} node{Object.keys(grouped).length !== 1 ? "s" : ""}
      </div>

      {Object.entries(grouped).map(([comid, items]) => (
        <section key={comid} className="insp-section">
          <div className="insp-section-title">COMID {comid}</div>
          {items.map((a, i) => (
            <div key={i} className="anomaly-entry">
              <div className="anomaly-header">
                <span className="anomaly-param">{a.parameter}</span>
                <span className="anomaly-time">{a.time_window}</span>
              </div>
              <div className="anomaly-values">
                <span>
                  Observed: <strong className="num-val">{a.observed_value}</strong>
                </span>
                <span>
                  Daily mean: <strong>{a.daily_mean}</strong>
                </span>
                <span className={`anomaly-dev ${a.deviation_pct > 50 ? "dev-high" : "dev-med"}`}>
                  Deviation: {a.deviation_pct?.toFixed(1)}%
                </span>
              </div>
              {a.possible_cause && a.possible_cause !== "Pending LLM analysis" && (
                <div className="anomaly-cause">{a.possible_cause}</div>
              )}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// Inspector Tab
// ─────────────────────────────────────────────────────────────────────────────

function InspectorTab({ agent }) {
  const [showPrompt,   setShowPrompt]   = useState(false);
  const [showRawOut,   setShowRawOut]   = useState(false);
  const [showAnalysis, setShowAnalysis] = useState(true);

  if (!agent) {
    return (
      <div className="tab-content inspector-empty">
        <div className="inspector-empty-icon">🔍</div>
        <p>Click a node on the map<br />or a log entry<br />to inspect it here</p>
      </div>
    );
  }

  const result = agent.result || {};
  const params = result.parameters || [];

  return (
    <div className="tab-content inspector-content">

      {/* Node info */}
      <section className="insp-section">
        <div className="insp-section-title">Node Info</div>
        <div className="insp-row"><span>COMID</span><code>{agent.comid}</code></div>
        <div className="insp-row">
          <span>Coordinates</span>
          <code>{agent.lat?.toFixed(5)}, {agent.lon?.toFixed(5)}</code>
        </div>
        <div className="insp-row">
          <span>USGS site</span>
          <code>{agent.has_usgs_site ? (agent.site_id || "yes") : "none (inferred node)"}</code>
        </div>
      </section>

      {/* Parameter table */}
      {params.length > 0 && (
        <section className="insp-section">
          <div className="insp-section-title">Inference Results</div>
          <table className="param-table">
            <thead>
              <tr><th>Parameter</th><th>Estimated</th><th>Unit</th></tr>
            </thead>
            <tbody>
              {params.map((p, i) => (
                <tr key={i}>
                  <td>{p.name}</td>
                  <td className={p.estimate_value == null ? "null-val" : "num-val"}>
                    {p.estimate_value != null ? p.estimate_value : "N/A"}
                  </td>
                  <td>{p.unit || "—"}</td>
                </tr>
              ))}
              {result.estimated_flow != null && (
                <tr>
                  <td>Flow</td>
                  <td className="num-val">{result.estimated_flow}</td>
                  <td>cfs</td>
                </tr>
              )}
            </tbody>
          </table>
          {result.confidence != null && (
            <div className="confidence-bar-wrap">
              <span>Confidence</span>
              <div className="confidence-bar">
                <div
                  className="confidence-fill"
                  style={{ width: `${(result.confidence * 100).toFixed(0)}%` }}
                />
              </div>
              <span className="confidence-pct">
                {(result.confidence * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </section>
      )}

      {/* Analysis text — collapsible with scroll */}
      {result.analysis && (
        <section className="insp-section collapsible">
          <button
            className="collapse-toggle"
            onClick={() => setShowAnalysis((v) => !v)}
          >
            {showAnalysis ? "▾" : "▸"} Analysis
          </button>
          {showAnalysis && (
            <div className="analysis-scroll">
              <p className="analysis-text">{result.analysis}</p>
            </div>
          )}
        </section>
      )}

      {/* LLM Prompt (collapsible) */}
      {agent.prompt_used && (
        <section className="insp-section collapsible">
          <button
            className="collapse-toggle"
            onClick={() => setShowPrompt((v) => !v)}
          >
            {showPrompt ? "▾" : "▸"} LLM Prompt (full input)
          </button>
          {showPrompt && <pre className="code-block">{agent.prompt_used}</pre>}
        </section>
      )}

      {/* LLM Raw Output (collapsible) */}
      {agent.llm_raw_output && (
        <section className="insp-section collapsible">
          <button
            className="collapse-toggle"
            onClick={() => setShowRawOut((v) => !v)}
          >
            {showRawOut ? "▾" : "▸"} LLM Raw Output
          </button>
          {showRawOut && <pre className="code-block">{agent.llm_raw_output}</pre>}
        </section>
      )}
    </div>
  );
}

// Inline style for the log-interrupted class (amber left border).
// Appended here to avoid requiring a CSS file edit.
const INTERRUPTED_STYLE = `
  .log-interrupted { border-left-color: #f59e0b; }
  .log-interrupted .log-msg { color: #fcd34d; }
  .dot-stopping { background: #f59e0b; opacity: 0.7; animation: pulse 1s infinite; }
`;

// ─────────────────────────────────────────────────────────────────────────────
// RightPanel (tab switcher)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Props:
 *   logs          - log entry list
 *   status        - idle | running | done | error
 *   selectedAgent - agent data to show in Inspector (maintained in App.jsx)
 *   onSelectAgent - callback to update selectedAgent from the log tab
 */
export default function RightPanel({ logs, status, selectedAgent, onSelectAgent, anomalies }) {
  const [activeTab, setActiveTab] = useState("logs");

  // Auto-switch to Inspector when a new agent is selected
  useEffect(() => {
    if (selectedAgent) setActiveTab("inspector");
  }, [selectedAgent]);

  return (
    <div className="right-panel">
      {/* Inline styles for "interrupted" elements not in RightPanel.css */}
      <style>{INTERRUPTED_STYLE}</style>
      <div className="panel-header">
        <span className="icon">📡</span>
        <h2>Live Log & Inspector</h2>
        <span
          className={`status-dot dot-${status}`}
          title={status}
          style={
            status === "interrupted" ? { background: "#f59e0b" } :
            status === "stopping"    ? { background: "#f59e0b", opacity: 0.7 } :
            {}
          }
        />
      </div>

      <div className="tab-bar">
        <button
          className={`tab-btn ${activeTab === "logs" ? "active" : ""}`}
          onClick={() => setActiveTab("logs")}
        >
          Log
          {logs.length > 0 && <span className="tab-badge">{logs.length}</span>}
        </button>
        <button
          className={`tab-btn ${activeTab === "inspector" ? "active" : ""}`}
          onClick={() => setActiveTab("inspector")}
        >
          Inspector
          {selectedAgent && <span className="tab-badge inspector-badge">●</span>}
        </button>
        <button
          className={`tab-btn ${activeTab === "anomalies" ? "active" : ""}`}
          onClick={() => setActiveTab("anomalies")}
        >
          Anomalies
          {anomalies?.length > 0 && (
            <span className="tab-badge anomaly-badge">{anomalies.length}</span>
          )}
        </button>
      </div>

      {activeTab === "logs" ? (
        <LogsTab
          logs={logs}
          onSelectAgent={(log) => {
            onSelectAgent({
              comid:          log.comid,
              lat:            log.lat,
              lon:            log.lon,
              has_usgs_site:  log.has_usgs_site,
              site_id:        log.site_id,
              result:         log.result,
              prompt_used:    log.prompt_used,
              llm_raw_output: log.llm_raw_output,
            });
          }}
        />
      ) : activeTab === "anomalies" ? (
        <AnomalyTab anomalies={anomalies} />
      ) : (
        <InspectorTab agent={selectedAgent} />
      )}
    </div>
  );
}
