import { useEffect, useRef } from "react";
import "./LogPanel.css";

/**
 * Return the corresponding icon and color class name based on the event type
 */
function getEventStyle(type) {
  switch (type) {
    case "topology":   return { icon: "🗺️", cls: "log-topology" };
    case "agent_start": return { icon: "⚙️", cls: "log-start" };
    case "agent_done":  return { icon: "✅", cls: "log-done" };
    case "done":        return { icon: "🏆", cls: "log-finish" };
    case "error":       return { icon: "❌", cls: "log-error" };
    default:            return { icon: "ℹ️", cls: "log-info" };
  }
}

/**
 * Format a single event object into readable text
 */
function formatEventMessage(log) {
  switch (log.type) {
    case "topology":
      return `Topology ready: ${log.nodes?.length || 0} nodes, ${log.edges?.length || 0} edges`;

    case "agent_start":
      return `Agent [${log.comid}] Start reasoning ..`;

    case "agent_done": {
      const result = log.result;
      if (!result) return `Agent [${log.comid}] Completed (with no result)`;
      const paramStr = result.parameters
        ?.map((p) =>
          p.estimate_value !== null
            ? `${p.name}: ${p.estimate_value} ${p.unit || ""}`
            : `${p.name}: N/A`
        )
        .join(" | ");
      return `Agent [${log.comid}] completed → ${paramStr || "No parameter"} | Confidence: ${((result.confidence || 0) * 100).toFixed(0)}%`;
    }

    case "done":
      return `All reasoning completed, target node: ${log.target_id}`;

    case "error":
      return `Error: ${log.message}`;

    default:
      return log.message || JSON.stringify(log);
  }
}

/**
 * Log panel component
 * 
 * Props:
 *   logs   - Log list, each entry containing event data + timestamp field
 *   status - Current status: idle | running | done | error
 */
export default function LogPanel({ logs, status }) {
  // Used for automatically scrolling to the latest log
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  return (
    <div className="log-panel">
      {/* head */}
      <div className="panel-header">
        <span className="icon">📡</span>
        <h2>Real-time inference log</h2>
        <span className={`log-status-dot dot-${status}`} title={status} />
      </div>

      {/* Log List */}
      <div className="log-list">
        {logs.length === 0 ? (
          <div className="log-empty">Waiting for analysis to start ..</div>
        ) : (
          logs.map((log, i) => {
            const { icon, cls } = getEventStyle(log.type);
            return (
              <div key={i} className={`log-entry ${cls}`}>
                {/* timestamp */}
                <span className="log-time">{log.timestamp}</span>

                {/* icon */}
                <span className="log-icon">{icon}</span>

                {/* Main message text */}
                <span className="log-msg">{formatEventMessage(log)}</span>

                {/* If it is "agent_done", display the analysis text (collapsible) */}
                {log.type === "agent_done" && log.result?.analysis && (
                  <details className="log-detail">
                    <summary>View analysis details</summary>
                    <p>{log.result.analysis}</p>
                  </details>
                )}
              </div>
            );
          })
        )}
        {/* scroll anchor */}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
