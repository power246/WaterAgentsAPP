import { useState } from "react";
import {
  ComposedChart, Line, Area, Scatter,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend,
} from "recharts";
import "./TimeSeriesChart.css";

/**
 * TimeSeriesChart
 *
 * Renders a bottom-panel time-series chart for a single agent node.
 * Supports multiple parameters via tabs.
 * - Historical days: solid line
 * - Prediction days: dashed line  (date > today)
 * - Confidence band: shaded area around the line
 * - Anomaly days: red scatter dot overlaid on the line
 *
 * Props:
 *   comid    string                    — COMID of the agent
 *   data     { paramName: [{date, value, confidence, hasAnomaly, unit}] }
 *   onClose  () => void
 */
export default function TimeSeriesChart({ comid, data, segments, onClose }) {
  const paramNames = Object.keys(data || {});
  const [activeTab, setActiveTab] = useState(paramNames[0] || "");

  if (!paramNames.length) return null;

  // Use local date (not UTC) to match the backend's date.today() which is also local.
  // UTC date can be one day ahead in US timezones during evening hours.
  const todayLocal = new Date();
  const todayStr = [
    todayLocal.getFullYear(),
    String(todayLocal.getMonth() + 1).padStart(2, "0"),
    String(todayLocal.getDate()).padStart(2, "0"),
  ].join("-");

  // If segments info is available from the backend, use the prediction segment's
  // start date for accurate classification — more reliable than comparing to today.
  const predictionStartDate = (segments || []).find(
    (s) => s.mode === "prediction"
  )?.start || null;
  const series   = data[activeTab] || [];

  // Derive unit from first data point that has one
  const unit = series.find((d) => d.unit)?.unit || "";

  // Build chart data: add confidence band bounds and flag prediction days
  const chartData = series.map((d) => {
    const v = d.value;
    const c = d.confidence ?? 0.5;
    // Band half-width: widens as confidence drops (max ±15% of value)
    const halfWidth = v != null ? Math.abs(v) * (1 - c) * 0.15 : 0;
    return {
      date:        d.date,
      value:       v,
      upper:       v != null ? +(v + halfWidth).toFixed(4) : null,
      lower:       v != null ? +(v - halfWidth).toFixed(4) : null,
      confidence:  c,
      hasAnomaly:  d.hasAnomaly,
      // Prefer backend-provided prediction start date; fall back to local-date comparison.
      isPrediction: predictionStartDate
        ? d.date >= predictionStartDate
        : d.date > todayStr,
    };
  });

  // Split into historical and prediction segments for different line styles.
  // recharts does not support per-segment dash natively, so we overlay two Lines.
  const histData = chartData.map((d) => ({
    ...d,
    histValue: !d.isPrediction ? d.value : null,
  }));
  const predData = chartData.map((d) => ({
    ...d,
    predValue: d.isPrediction ? d.value : null,
  }));
  // Merge into one array for recharts (both extra keys present)
  const merged = chartData.map((d, i) => ({
    ...d,
    histValue: histData[i].histValue,
    predValue: predData[i].predValue,
    // Anomaly value: only set on anomaly days so Scatter renders a dot
    anomalyValue: d.hasAnomaly ? d.value : null,
  }));

  // Custom dot: invisible by default; Scatter handles anomaly markers separately
  const hiddenDot = () => null;

  return (
    <div className="ts-panel">
      {/* Header */}
      <div className="ts-header">
        <span className="ts-title">COMID {comid} — Time Series</span>

        {/* Parameter tabs */}
        <div className="ts-tabs">
          {paramNames.map((name) => (
            <button
              key={name}
              className={`ts-tab ${activeTab === name ? "ts-tab-active" : ""}`}
              onClick={() => setActiveTab(name)}
            >
              {name}
            </button>
          ))}
        </div>

        <button className="ts-close" onClick={onClose} title="Close chart">✕</button>
      </div>

      {/* Chart */}
      <div className="ts-chart-area">
        {series.length === 0 ? (
          <div className="ts-empty">No data available for {activeTab}</div>
        ) : (
          <ResponsiveContainer width="100%" height={180}>
            <ComposedChart
              data={merged}
              margin={{ top: 8, right: 24, bottom: 4, left: 8 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#c8ddf5" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: "#7a90b8" }}
                tickLine={false}
              />
              <YAxis
                tick={{ fontSize: 10, fill: "#7a90b8" }}
                tickLine={false}
                unit={unit ? ` ${unit}` : ""}
                width={60}
              />
              <Tooltip
                contentStyle={{ fontSize: 12, borderColor: "#c8ddf5" }}
                formatter={(val, name) => {
                  if (name === "Confidence band") return null; // hide band in tooltip
                  return [val != null ? `${val} ${unit}` : "N/A", name];
                }}
              />

              {/* Confidence band */}
              <Area
                type="monotone"
                dataKey="upper"
                stroke="none"
                fill="#bfdbfe"
                fillOpacity={0.25}
                dot={false}
                activeDot={false}
                legendType="none"
                connectNulls
              />
              <Area
                type="monotone"
                dataKey="lower"
                stroke="none"
                fill="#ffffff"
                fillOpacity={1}
                dot={false}
                activeDot={false}
                legendType="none"
                connectNulls
              />

              {/* Historical segment — solid blue line */}
              <Line
                type="monotone"
                dataKey="histValue"
                name="Historical"
                stroke="#1234e1"
                strokeWidth={2}
                dot={hiddenDot}
                connectNulls
              />

              {/* Prediction segment — dashed purple line */}
              <Line
                type="monotone"
                dataKey="predValue"
                name="Prediction"
                stroke="#6d28d9"
                strokeWidth={3}
                strokeDasharray="8 4"
                dot={hiddenDot}
                connectNulls
              />

              {/* Anomaly markers — red filled dots */}
              <Scatter
                dataKey="anomalyValue"
                name="Anomaly"
                fill="#ef4444"
                r={5}
                shape={(props) => {
                  // Only render when value is non-null
                  if (props.anomalyValue == null) return null;
                  return (
                    <circle
                      cx={props.cx}
                      cy={props.cy}
                      r={5}
                      fill="#ef4444"
                      stroke="#fff"
                      strokeWidth={1.5}
                    />
                  );
                }}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Legend */}
      <div className="ts-legend">
        <span className="ts-leg-item ts-leg-hist">Historical</span>
        <span className="ts-leg-item ts-leg-pred">Prediction</span>
        <span className="ts-leg-item ts-leg-anom">Anomaly</span>
        <span className="ts-leg-item ts-leg-band">Confidence band</span>
      </div>
    </div>
  );
}
