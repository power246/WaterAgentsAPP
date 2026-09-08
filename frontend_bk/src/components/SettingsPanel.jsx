// Slide-out settings drawer with four sections:
//   1. LLM Model       — provider, model, per-session API key override
//   2. API Keys        — persistent key storage (Google Maps, LLM providers)
//   3. Facility Types  — EPA database checkboxes
//   4. Analysis Params — topology depth, resample frequency

import { useState, useEffect } from "react";
import "./SettingsPanel.css";

// ── Validation helpers ────────────────────────────────────────────────────────

function clamp(value, min, max) {
  const n = parseInt(value, 10);
  if (isNaN(n)) return min;
  return Math.max(min, Math.min(max, n));
}

// ── NumericField ──────────────────────────────────────────────────────────────

function NumericField({ label, value, onChange, limits, defaultValue, unit, disabled = false }) {
  const { min, max } = limits;
  const [raw,   setRaw]   = useState(String(value));
  const [error, setError] = useState("");

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

// ── ApiKeyField — single persistent key row ───────────────────────────────────
//
// States:
//   saved   — key is stored in backend  (shows masked value + Clear button)
//   editing — user is typing a new key  (shows input + Save button)
//   empty   — not set                   (shows placeholder + Save button)

function ApiKeyField({ label, keyName, hint, status, onSave, onClear }) {
  const [inputVal, setInputVal] = useState("");
  const [saving,   setSaving]   = useState(false);
  const [clearing, setClearing] = useState(false);
  const [localMsg, setLocalMsg] = useState("");

  const isSaved = status?.set;

  const handleSave = async () => {
    if (!inputVal.trim()) return;
    setSaving(true);
    setLocalMsg("");
    try {
      await onSave(keyName, inputVal.trim());
      setInputVal("");
      setLocalMsg("Saved.");
    } catch (e) {
      setLocalMsg(`Error: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async () => {
    setClearing(true);
    setLocalMsg("");
    try {
      await onClear(keyName);
      setLocalMsg("Cleared.");
    } catch (e) {
      setLocalMsg(`Error: ${e.message}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="api-key-row">
      <div className="api-key-label-row">
        <span className="api-key-label">{label}</span>
        {isSaved && (
          <span className="api-key-badge saved">
            {status.masked}
          </span>
        )}
      </div>

      <div className="api-key-input-row">
        <input
          type="password"
          className="settings-input api-key-input"
          placeholder={isSaved ? "Enter new key to replace..." : (hint || "Enter API key...")}
          value={inputVal}
          onChange={(e) => setInputVal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSave()}
          autoComplete="new-password"
        />
        <button
          className="btn-save-key"
          onClick={handleSave}
          disabled={saving || !inputVal.trim()}
        >
          {saving ? "Saving..." : "Save"}
        </button>
        {isSaved && (
          <button
            className="btn-clear-key"
            onClick={handleClear}
            disabled={clearing}
            title="Remove saved key (falls back to server .env)"
          >
            {clearing ? "..." : "Clear"}
          </button>
        )}
      </div>

      {localMsg && (
        <p className={`api-key-msg ${localMsg.startsWith("Error") ? "error" : "ok"}`}>
          {localMsg}
        </p>
      )}
    </div>
  );
}

// ── Main SettingsPanel ────────────────────────────────────────────────────────

/**
 * Props:
 *   onApply(settings)  - called when user clicks Apply
 *   onClose()          - called when drawer is dismissed
 *   currentSettings    - current applied settings (to initialise form)
 */
export default function SettingsPanel({ onApply, onClose, currentSettings }) {
  // Catalogue fetched from backend
  const [catalogue,  setCatalogue]  = useState(null);
  const [loading,    setLoading]    = useState(true);
  const [fetchErr,   setFetchErr]   = useState("");

  // API key status fetched from backend (masked display)
  const [keyStatus,  setKeyStatus]  = useState({});
  const [ollamaErr,  setOllamaErr]  = useState("");

  // LLM section
  const [provider,       setProvider]       = useState(currentSettings.provider);
  const [modelName,      setModelName]      = useState(currentSettings.model_name);
  const [sessionApiKey,  setSessionApiKey]  = useState(currentSettings.api_key || "");

  // Facility types
  const [facilityTypes,  setFacilityTypes]  = useState(currentSettings.facility_types);

  // Analysis params
  const [maxTopoDepth,   setMaxTopoDepth]   = useState(currentSettings.max_topology_depth);
  const [resampleAuto,   setResampleAuto]   = useState(currentSettings.resample_auto ?? true);
  const [resampleFreqH,  setResampleFreqH]  = useState(currentSettings.resample_freq_h);
  const [weatherFreqH,   setWeatherFreqH]   = useState(currentSettings.weather_freq_h);

  // ── Fetch catalogue + key status + Ollama models on mount ──────────────────
  useEffect(() => {
    const fetchAll = async () => {
      try {
        // 1. Settings catalogue
        const catRes  = await fetch("/api/settings/defaults");
        const catData = await catRes.json();
        setCatalogue(catData);

        // 2. Try to replace Ollama model list with live local models
        const ollamaRes  = await fetch("/api/keys/ollama-models");
        const ollamaData = await ollamaRes.json();
        if (ollamaData.error) {
          setOllamaErr(ollamaData.error);
        } else if (ollamaData.models?.length) {
          // Patch Ollama provider models with the live list
          setCatalogue((prev) => ({
            ...prev,
            providers: prev.providers.map((p) =>
              p.id === "ollama"
                ? { ...p, models: ollamaData.models }
                : p
            ),
          }));
        }

        // 3. Saved API key status (masked)
        const keyRes  = await fetch("/api/keys");
        const keyData = await keyRes.json();
        setKeyStatus(keyData);

        setLoading(false);
      } catch (e) {
        setFetchErr(`Failed to load settings: ${e.message}`);
        setLoading(false);
      }
    };
    fetchAll();
  }, []);

  // ── Handlers ───────────────────────────────────────────────────────────────

  const handleProviderChange = (newProvider) => {
    setProvider(newProvider);
    if (catalogue) {
      const prov = catalogue.providers.find((p) => p.id === newProvider);
      if (prov?.models?.length) setModelName(prov.models[0].id);
    }
    setSessionApiKey("");
  };

  const handleRestoreDefaults = () => {
    if (!catalogue) return;
    const d = catalogue.defaults;
    setProvider(d.provider);
    setModelName(d.model_name);
    setSessionApiKey("");
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
      api_key:            sessionApiKey.trim() || null,
      facility_types:     facilityTypes,
      max_topology_depth: maxTopoDepth,
      resample_auto:      resampleAuto,
      resample_freq_h:    resampleFreqH,
      weather_freq_h:     weatherFreqH,
    });
    onClose();
  };

  const toggleFacility = (id) =>
    setFacilityTypes((prev) =>
      prev.includes(id) ? prev.filter((f) => f !== id) : [...prev, id]
    );

  // Persist a key to the backend store
  const handleSaveKey = async (name, value) => {
    const res = await fetch(`/api/keys/${name}`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ value }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Save failed");
    }
    // Refresh key status display
    const keyRes  = await fetch("/api/keys");
    setKeyStatus(await keyRes.json());
  };

  // Remove a key from the backend store
  const handleClearKey = async (name) => {
    const res = await fetch(`/api/keys/${name}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Clear failed");
    }
    const keyRes  = await fetch("/api/keys");
    setKeyStatus(await keyRes.json());
  };

  // ── Derived values ──────────────────────────────────────────────────────────
  const currentProviderInfo = catalogue?.providers.find((p) => p.id === provider);
  const availableModels     = currentProviderInfo?.models ?? [];
  const needsApiKey         = currentProviderInfo?.needs_api_key ?? false;
  const limits              = catalogue?.limits ?? {
    max_topology_depth: { min: 1, max: 20 },
    resample_freq_h:    { min: 1, max: 168 },
    weather_freq_h:     { min: 1, max: 168 },
  };
  const defaults = catalogue?.defaults ?? currentSettings;

  // Build the API keys config list (Google Maps + per-provider)
  const apiKeyConfigs = [
    {
      label:   "Google Maps API Key",
      keyName: "GOOGLE_MAPS_API_KEY",
      hint:    "AIza...",
    },
    ...(catalogue?.providers
      .filter((p) => p.needs_api_key && p.store_key)
      .map((p) => ({
        label:   `${p.label} API Key`,
        keyName: p.store_key,
        hint:    p.api_key_hint || "",
      })) ?? []),
  ];

  return (
    <div
      className="settings-overlay"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
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
          {loading  && <p className="settings-loading">Loading options...</p>}
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
                  {provider === "ollama" && ollamaErr && (
                    <p className="field-error" style={{ marginBottom: "6px" }}>
                      {ollamaErr}
                    </p>
                  )}
                  <select
                    className="settings-select"
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                  >
                    {availableModels.map((m) => (
                      <option key={m.id} value={m.id}>{m.label}</option>
                    ))}
                  </select>
                  {provider === "ollama" && !ollamaErr && (
                    <p className="field-hint">Live list from local Ollama instance.</p>
                  )}
                </div>

                {/* Session API key override (for cloud providers only) */}
                {needsApiKey && (
                  <div className="setting-row">
                    <div className="setting-label-row">
                      <span className="setting-label">Session Key Override</span>
                      <span className="setting-default">overrides saved key for this session</span>
                    </div>
                    <input
                      type="password"
                      className="settings-input"
                      placeholder={currentProviderInfo?.api_key_hint || "Leave blank to use saved key"}
                      value={sessionApiKey}
                      onChange={(e) => setSessionApiKey(e.target.value)}
                      autoComplete="new-password"
                    />
                    <p className="field-hint">
                      Temporary — not saved to disk. Use the API Keys section below to save permanently.
                    </p>
                  </div>
                )}
              </section>

              {/* ── Section 2: API Keys (persistent storage) ─────────────── */}
              <section className="settings-section">
                <div className="section-title">API Keys</div>
                <p className="section-hint">
                  Keys are saved to <code>data/user_keys.json</code> on the server and survive restarts.
                  Use <strong>Clear</strong> to remove a key and fall back to the server&apos;s .env file.
                </p>

                {apiKeyConfigs.map((cfg) => (
                  <ApiKeyField
                    key={cfg.keyName}
                    label={cfg.label}
                    keyName={cfg.keyName}
                    hint={cfg.hint}
                    status={keyStatus[cfg.keyName]}
                    onSave={handleSaveKey}
                    onClear={handleClearKey}
                  />
                ))}

                <p className="field-hint" style={{ marginTop: "8px" }}>
                  The Google Maps key is used by the map component.
                  If not set, the map falls back to OpenStreetMap (no key required).
                </p>
              </section>

              {/* ── Section 3: Facility Types ────────────────────────────── */}
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

              {/* ── Section 4: Analysis Parameters ──────────────────────── */}
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

                <div className="setting-row">
                  <div className="setting-label-row">
                    <span className="setting-label">Resample Interval</span>
                    <span className="setting-default">default: auto</span>
                  </div>

                  <label className="toggle-row">
                    <span className="toggle-label">Auto-select based on time span</span>
                    <button
                      className={`toggle-switch ${resampleAuto ? "on" : "off"}`}
                      onClick={() => setResampleAuto((v) => !v)}
                      role="switch"
                      aria-checked={resampleAuto}
                    >
                      <span className="toggle-thumb" />
                    </button>
                  </label>

                  {resampleAuto && catalogue?.auto_freq_rules && (
                    <div className="auto-freq-table">
                      <div className="auto-freq-header">
                        <span>Time span</span><span>USGS</span><span>Weather</span>
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
