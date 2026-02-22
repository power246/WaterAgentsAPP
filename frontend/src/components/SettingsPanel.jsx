
// Slide-out settings drawer. Opened by a gear icon in ControlPanel.
// On mount it fetches available options from GET /api/settings/defaults.
// All changes are local until the user clicks "Apply" (calls onApply).

import { useState, useEffect } from "react";
import "./SettingsPanel.css";

// ── Validation helpers ─────────────────────────────────────────────────────

function clamp(value, min, max) {
  const n = parseInt(value, 10);
  if (isNaN(n)) return min;
  return Math.max(min, Math.min(max, n));
}

// ── NumericField component ─────────────────────────────────────────────────

function NumericField({ label, value, onChange, limits, defaultValue, unit, disabled = false }) {
  const { min, max } = limits;
  const [raw, setRaw] = useState(String(value));
  const [error, setError] = useState("");

  // Keep raw input in sync when parent resets defaults
  useEffect(() => { setRaw(String(value)); setError(""); }, [value]);

  const handleChange = (e) => {
    if (disabled) return;
    const text = e.target.value;
    setRaw(text);
    const n = parseInt(text, 10);
    if (isNaN(n) || n < min || n > max) {
      setError(`Must be ${min}–${max} ${unit}`);
    } else {
      setError("");
      onChange(n);
    }
  };

  return (
    <div className={`setting-row ${disabled ? "setting-row-disabled" : ""}`}>
      <div className="setting-label-row">
        <span className="setting-label">{label}</span>
        <span className="setting-default">default: {defaultValue} {unit}</span>
      </div>
      <div className="numeric-input-wrap">
        <input
          type="number"
          className={`numeric-input ${error ? "input-error" : ""}`}
          value={raw}
          min={min}
          max={max}
          onChange={handleChange}
          disabled={disabled}
        />
        <span className="numeric-unit">{unit}</span>
        {disabled && <span className="manual-override-hint">auto mode on</span>}
      </div>
      {error && !disabled && <p className="field-error">{error}</p>}
      {!disabled && <p className="field-hint">Valid range: {min}–{max} {unit}</p>}
    </div>
  );
}

// ── Main SettingsPanel component ───────────────────────────────────────────

/**
 * Props:
 *   onApply(settings)  - called when user clicks Apply with the new settings
 *   onClose()          - called when user clicks X or the backdrop
 *   currentSettings    - current applied settings (to initialize form)
 */
export default function SettingsPanel({ onApply, onClose, currentSettings }) {
  // Available options fetched from the backend
  const [catalogue, setCatalogue] = useState(null);
  const [loading,   setLoading]   = useState(true);
  const [fetchErr,  setFetchErr]  = useState("");

  // Local form state – initialized from currentSettings then user can edit
  const [provider,          setProvider]          = useState(currentSettings.provider);
  const [modelName,         setModelName]          = useState(currentSettings.model_name);
  const [apiKey,            setApiKey]             = useState(currentSettings.api_key || "");
  const [facilityTypes,     setFacilityTypes]      = useState(currentSettings.facility_types);
  const [maxTopoDepth,      setMaxTopoDepth]       = useState(currentSettings.max_topology_depth);
  const [resampleAuto,      setResampleAuto]       = useState(currentSettings.resample_auto ?? true);
  const [resampleFreqH,     setResampleFreqH]      = useState(currentSettings.resample_freq_h);
  const [weatherFreqH,      setWeatherFreqH]       = useState(currentSettings.weather_freq_h);

  // ── Fetch catalogue on mount ─────────────────────────────────────────────
  useEffect(() => {
    fetch("/api/settings/defaults")
      .then((r) => r.json())
      .then((data) => {
        setCatalogue(data);
        setLoading(false);
      })
      .catch((e) => {
        setFetchErr(`Failed to load settings: ${e.message}`);
        setLoading(false);
      });
  }, []);

  // When provider changes, reset model to the first available for that provider
  const handleProviderChange = (newProvider) => {
    setProvider(newProvider);
    if (catalogue) {
      const prov = catalogue.providers.find((p) => p.id === newProvider);
      if (prov?.models?.length) setModelName(prov.models[0].id);
    }
  };

  // Restore all values to backend defaults
  const handleRestoreDefaults = () => {
    if (!catalogue) return;
    const d = catalogue.defaults;
    setProvider(d.provider);
    setModelName(d.model_name);
    setApiKey("");
    setFacilityTypes(d.facility_types);
    setMaxTopoDepth(d.max_topology_depth);
    setResampleAuto(d.resample_auto ?? true);
    setResampleFreqH(d.resample_freq_h);
    setWeatherFreqH(d.weather_freq_h);
  };

  const handleApply = () => {
    onApply({
      provider,
      model_name:         modelName,
      api_key:            apiKey.trim() || null,
      facility_types:     facilityTypes,
      max_topology_depth: maxTopoDepth,
      resample_auto:      resampleAuto,
      resample_freq_h:    resampleFreqH,
      weather_freq_h:     weatherFreqH,
    });
    onClose();
  };

  const toggleFacility = (id) => {
    setFacilityTypes((prev) =>
      prev.includes(id) ? prev.filter((f) => f !== id) : [...prev, id]
    );
  };

  // ── Derived helpers ──────────────────────────────────────────────────────
  const currentProviderInfo = catalogue?.providers.find((p) => p.id === provider);
  const availableModels     = currentProviderInfo?.models ?? [];
  const needsApiKey         = currentProviderInfo?.needs_api_key ?? false;
  const limits              = catalogue?.limits ?? {
    max_topology_depth: { min: 1, max: 20 },
    resample_freq_h:    { min: 1, max: 24 },
    weather_freq_h:     { min: 1, max: 24 },
  };
  const defaults = catalogue?.defaults ?? currentSettings;

  return (
    <div className="settings-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="settings-drawer">

        {/* Header */}
        <div className="settings-header">
          <span className="settings-title">Settings</span>
          <div className="settings-header-actions">
            <button className="btn-ghost" onClick={handleRestoreDefaults} title="Restore all defaults">
              Restore defaults
            </button>
            <button className="settings-close" onClick={onClose} title="Close">✕</button>
          </div>
        </div>

        <div className="settings-body">
          {loading && <p className="settings-loading">Loading options...</p>}
          {fetchErr && <p className="settings-error">{fetchErr}</p>}

          {!loading && !fetchErr && catalogue && (
            <>
              {/* ── Section 1: LLM Model ─────────────────────────────────── */}
              <section className="settings-section">
                <div className="section-title">LLM Model</div>

                {/* Provider selector */}
                <div className="setting-row">
                  <div className="setting-label-row">
                    <span className="setting-label">Provider</span>
                    <span className="setting-default">default: {defaults.provider}</span>
                  </div>
                  <div className="select-group">
                    {catalogue.providers.map((p) => (
                      <button
                        key={p.id}
                        className={`provider-btn ${provider === p.id ? "active" : ""}`}
                        onClick={() => handleProviderChange(p.id)}
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Model selector */}
                <div className="setting-row">
                  <div className="setting-label-row">
                    <span className="setting-label">Model</span>
                    <span className="setting-default">default: {defaults.model_name}</span>
                  </div>
                  <select
                    className="settings-select"
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                  >
                    {availableModels.map((m) => (
                      <option key={m.id} value={m.id}>{m.label}</option>
                    ))}
                  </select>
                </div>

                {/* API Key (only for cloud providers) */}
                {needsApiKey && (
                  <div className="setting-row">
                    <div className="setting-label-row">
                      <span className="setting-label">API Key</span>
                      <span className="setting-default">leave blank to use server .env</span>
                    </div>
                    <input
                      type="password"
                      className="settings-input"
                      placeholder={currentProviderInfo?.api_key_hint || "Enter API key..."}
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                    />
                    <p className="field-hint">
                      If blank, the server uses its configured environment variable.
                    </p>
                  </div>
                )}
              </section>

              {/* ── Section 2: Facility Types ────────────────────────────── */}
              <section className="settings-section">
                <div className="section-title">Facility Types</div>
                <p className="section-hint">
                  Select which EPA facility databases to query within 1 km of each river segment.
                </p>
                <div className="facility-list">
                  {catalogue.facility_types.map((ft) => {
                    const checked = facilityTypes.includes(ft.id);
                    return (
                      <label
                        key={ft.id}
                        className={`facility-item ${checked ? "checked" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleFacility(ft.id)}
                        />
                        <div className="facility-text">
                          <span className="facility-id">{ft.label}</span>
                          <span className="facility-desc">{ft.description}</span>
                        </div>
                      </label>
                    );
                  })}
                </div>
                {facilityTypes.length === 0 && (
                  <p className="field-error">Select at least one facility type.</p>
                )}
              </section>

              {/* ── Section 3: Analysis Parameters ──────────────────────── */}
              <section className="settings-section">
                <div className="section-title">Analysis Parameters</div>

                <NumericField
                  label="Topology BFS Depth"
                  value={maxTopoDepth}
                  onChange={setMaxTopoDepth}
                  limits={limits.max_topology_depth}
                  defaultValue={defaults.max_topology_depth}
                  unit="hops"
                />

                {/* ── Resample frequency: auto / manual toggle ─────── */}
                <div className="setting-row">
                  <div className="setting-label-row">
                    <span className="setting-label">Resample Interval</span>
                    <span className="setting-default">default: auto</span>
                  </div>

                  {/* Toggle switch */}
                  <label className="toggle-row">
                    <span className="toggle-label">
                      Auto-select based on time span
                    </span>
                    <button
                      className={`toggle-switch ${resampleAuto ? "on" : "off"}`}
                      onClick={() => setResampleAuto((v) => !v)}
                      role="switch"
                      aria-checked={resampleAuto}
                    >
                      <span className="toggle-thumb" />
                    </button>
                  </label>

                  {/* Auto preview table */}
                  {resampleAuto && catalogue?.auto_freq_rules && (
                    <div className="auto-freq-table">
                      <div className="auto-freq-header">
                        <span>Time span</span>
                        <span>USGS</span>
                        <span>Weather</span>
                      </div>
                      {catalogue.auto_freq_rules.map((r, i) => (
                        <div className="auto-freq-row" key={i}>
                          <span>{r.label}</span>
                          <span>{r.resample_h}h</span>
                          <span>{r.weather_h}h</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Manual fields — only active when auto is OFF */}
                <NumericField
                  label="USGS Data Resample Interval"
                  value={resampleFreqH}
                  onChange={setResampleFreqH}
                  limits={limits.resample_freq_h}
                  defaultValue={defaults.resample_freq_h}
                  unit="hours"
                  disabled={resampleAuto}
                />

                <NumericField
                  label="Weather Data Interval"
                  value={weatherFreqH}
                  onChange={setWeatherFreqH}
                  limits={limits.weather_freq_h}
                  defaultValue={defaults.weather_freq_h}
                  unit="hours"
                  disabled={resampleAuto}
                />
              </section>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="settings-footer">
          <button className="btn-secondary-sm" onClick={onClose}>Cancel</button>
          <button
            className="btn-apply"
            onClick={handleApply}
            disabled={facilityTypes.length === 0}
          >
            Apply &amp; Close
          </button>
        </div>
      </div>
    </div>
  );
}
