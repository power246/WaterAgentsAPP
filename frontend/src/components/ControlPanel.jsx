import { useState } from "react";
import SettingsPanel from "./SettingsPanel";
import "./ControlPanel.css";

// Default settings matching the backend config.py defaults.
// These are overwritten when the user opens Settings and fetches /api/settings/defaults.
export const DEFAULT_SETTINGS = {
  provider:           "ollama",
  model_name:         "llama3:8b",
  api_key:            null,
  facility_types:     ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
  max_topology_depth: 10,
  resample_auto:      true,   // auto-select freq based on time span
  resample_freq_h:    6,
  weather_freq_h:     12,
};

/**
 * ControlPanel
 *
 * Props:
 *   selectedPoint     {lat, lng} | null  - coordinate chosen on the map
 *   status            "idle" | "running" | "done" | "error"
 *   onStartAnalysis   ({query, settings}) => void
 */
export default function ControlPanel({ selectedPoint, status, onStartAnalysis, onStopAnalysis }) {
  const [query,           setQuery]           = useState("");
  const [showSettings,    setShowSettings]    = useState(false);
  const [settings,        setSettings]        = useState(DEFAULT_SETTINGS);

  const isRunning  = status === "running";
  const isStopping = status === "stopping";
  // Start button + inputs are locked while running OR while waiting for
  // the backend to confirm that all tasks have stopped.
  const isBlocked  = isRunning || isStopping;

  // Auto-fill query box from map click
  const handleFillCoords = () => {
    if (!selectedPoint) return;
    const { lat, lng } = selectedPoint;
    setQuery(
      `Compute the pH at coordinates: ` +
      `${lng.toFixed(6)}, ${lat.toFixed(6)} from 2026-01-01 to 2026-01-15.`
    );
  };

  const handleSubmit = () => {
    if (!query.trim()) {
      alert("Please enter a query.");
      return;
    }
    // Pass both the natural-language query and the current runtime settings
    onStartAnalysis({ query: query.trim(), settings });
  };

  const handleApplySettings = (newSettings) => {
    setSettings(newSettings);
  };

  // Short summary label shown under the gear icon
  const settingsSummary = `${settings.provider} / ${settings.model_name}`;

  return (
    <>
      <div className="control-panel">
        {/* Header */}
        <div className="panel-header">
          <img src="/M_maroon.svg" className="icon" />
          <h2>Water Agent System</h2>
          <button
            className="settings-gear-btn"
            onClick={() => setShowSettings(true)}
            title="Open settings"
            disabled={isBlocked}
          >
            ⚙
          </button>
        </div>

        <div className="control-body">
          {/* ── Map point ── */}
          <section className="control-section">
            <label className="section-label">Map point</label>
            {selectedPoint ? (
              <div className="coord-display">
                <div>Lat: {selectedPoint.lat.toFixed(6)}</div>
                <div>Lng: {selectedPoint.lng.toFixed(6)}</div>
                <button className="btn-secondary" onClick={handleFillCoords}>
                  Fill query box
                </button>
              </div>
            ) : (
              <p className="hint-text">Click the map to select a water body location</p>
            )}
          </section>

          {/* ── Query input ── */}
          <section className="control-section">
            <label className="section-label">Query</label>
            <textarea
              className="query-input"
              rows={5}
              placeholder={
                "Example:\nCompute the pH at coordinates: -92.41, 46.68 " +
                "from 2026-01-01 to 2026-01-15."
              }
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              disabled={isBlocked}
            />
          </section>

          {/* ── Current model summary (click to open settings) ── */}
          <section className="control-section">
            <label className="section-label">Model</label>
            <button
              className="model-summary-btn"
              onClick={() => setShowSettings(true)}
              disabled={isBlocked}
              title="Click to change model and other settings"
            >
              <span className="model-summary-text">{settingsSummary}</span>
              <span className="model-summary-edit">Edit ⚙</span>
            </button>
          </section>

          {/* ── Status badge ── */}
          {status !== "idle" && (
            <div className={`status-badge status-${status}`}>
              {status === "running"     && "Inference in progress..."}
              {status === "stopping"    && "Stopping — waiting for backend..."}
              {status === "done"        && "Inference complete"}
              {status === "interrupted" && "Inference interrupted"}
              {status === "error"       && "An error occurred"}
            </div>
          )}

          {/* ── Start button ── */}
          <button
            className="btn-primary"
            onClick={handleSubmit}
            disabled={isBlocked}
          >
            {isRunning  ? "Running..."  :
             isStopping ? "Stopping..." :
             "Start Analysis"}
          </button>

          {/* ── Stop button (red when active, grey when idle) ── */}
          <button
            onClick={onStopAnalysis}
            disabled={!isRunning}
            title={
              isRunning  ? "Stop — cancels all in-flight requests immediately" :
              isStopping ? "Stop signal sent, waiting for backend..." :
              "No inference running"
            }
            style={{
              width:        "100%",
              padding:      "10px",
              marginTop:    "6px",
              background:   isRunning ? "#dc2626" : isStopping ? "#92400e" : "#1f2937",
              border:       `1px solid ${isRunning ? "#dc2626" : isStopping ? "#92400e" : "#374151"}`,
              borderRadius: "8px",
              color:        isRunning ? "#ffffff" : isStopping ? "#fcd34d" : "#4b5563",
              cursor:       isRunning ? "pointer" : "not-allowed",
              fontSize:     "14px",
              fontWeight:   "600",
              transition:   "background 0.2s, border-color 0.2s",
            }}
          >
            Stop
          </button>
          {/* Hint shown after a run that was not a clean completion */}
          {!isBlocked && status === "interrupted" && (
            <p style={{
              fontSize:   "11px",
              color:      "#6b7280",
              textAlign:  "center",
              margin:     "6px 0 0",
              fontStyle:  "italic",
            }}>
              Completed agents remain visible. Click "Start Analysis" to re-run.
            </p>
          )}

          {/* ── Usage guide ── */}
          <div className="usage-guide">
            <p className="guide-title">How to use</p>
            <ol>
              <li>Click the map to select a water body</li>
              <li>Click "Fill query box" to auto-generate a query</li>
              <li>Optionally edit the query or time range</li>
              <li>Open Settings (⚙) to choose model, facilities, and parameters</li>
              <li>Click "Start Analysis"</li>
            </ol>
            <p className="guide-note">
              Click any node on the map to inspect its inference result
            </p>
          </div>
        </div>
      </div>

      {/* Settings drawer – rendered as a portal over the whole screen */}
      {showSettings && (
        <SettingsPanel
          currentSettings={settings}
          onApply={handleApplySettings}
          onClose={() => setShowSettings(false)}
        />
      )}
    </>
  );
}
