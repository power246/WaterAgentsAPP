import { useEffect, useRef } from "react";
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  Polyline,
  CircleMarker,
  useMapEvents,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./MapView.css";

// ── 修复 Leaflet 默认图标路径问题（Vite 构建时常见）────────────────────────────
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl:       "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl:     "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

// ── 颜色常量（与图例同步）────────────────────────────────────────────────────────
const COLOR = {
  inlet:    "#60a5fa",  // has USGS data  → blue
  active:   "#f59e0b",  // inferring      → orange
  done:     "#10b981",  // done           → green
  target:   "#f472b6",  // target segment → pink
  pending:  "#6b7280",  // waiting        → gray
  excluded: "#374151",  // not in chain   → dark gray (dimmed)
  edge:     "#4f46e5",  // active edge    → indigo
  edgeExcl: "#1f2937",  // excluded edge  → very dark
};

/**
 * Apply a tiny coordinate offset to nodes that share the same lat/lon,
 * so they don't completely overlap on the map.
 * Returns a new array with adjusted lat/lon values.
 */
function deduplicateCoords(nodes) {
  const seen = {};   // "lat,lon" -> count
  return nodes.map((node) => {
    if (!node.lat || !node.lon) return node;
    const key = `${node.lat.toFixed(5)},${node.lon.toFixed(5)}`;
    const idx = seen[key] ?? 0;
    seen[key] = idx + 1;
    if (idx === 0) return node;                 // First node at this position – no change
    // Each subsequent node gets a small spiral offset (~30-60m at this latitude)
    const angle = (idx * 137.5 * Math.PI) / 180;  // Golden-angle spiral
    const r = 0.0003 * idx;
    return {
      ...node,
      lat: node.lat + r * Math.cos(angle),
      lon: node.lon + r * Math.sin(angle),
    };
  });
}

/**
 * 根据节点状态返回颜色
 */
function getNodeColor(node, activeComid) {
  if (node.is_excluded) return COLOR.excluded;  // not in inference chain
  if (node.comid === activeComid) return COLOR.active;
  if (node.done)      return COLOR.done;
  if (node.is_inlet)  return COLOR.inlet;
  if (node.is_target) return COLOR.target;
  return COLOR.pending;
}

/**
 * 子组件：监听地图点击
 */
function ClickHandler({ onMapClick }) {
  useMapEvents({ click: (e) => onMapClick(e.latlng) });
  return null;
}

/**
 * Auto-zoom the map once coordinates become available.
 * Includes ALL nodes (active and excluded) so the full topology is visible.
 * hasFit prevents repeated re-centering after each agent_done event.
 */
function FitBoundsController({ nodes }) {
  const map = useMap();
  const hasFit = useRef(false);
  const validCount = nodes.filter((n) => n.lat && n.lon).length;

  useEffect(() => {
    if (hasFit.current) return;
    const valid = nodes.filter((n) => n.lat && n.lon);
    if (valid.length === 0) return;

    map.fitBounds(valid.map((n) => [n.lat, n.lon]), { padding: [60, 60], maxZoom: 13 });
    hasFit.current = true;
  }, [validCount]);

  return null;
}

/**
 * 子组件：在两个节点坐标之间画一条带方向箭头的折线。
 * 箭头通过在终点前一点放置一个细三角形 Marker 来实现（无需额外库）。
 */
function DirectedEdge({ fromNode, toNode, isExcluded }) {
  if (!fromNode?.lat || !toNode?.lat) return null;

  const from = [fromNode.lat, fromNode.lon];
  const to   = [toNode.lat,   toNode.lon];
  const arrowLat = fromNode.lat + (toNode.lat - fromNode.lat) * 0.75;
  const arrowLon = fromNode.lon + (toNode.lon - fromNode.lon) * 0.75;
  const edgeColor = isExcluded ? COLOR.edgeExcl : COLOR.edge;
  const opacity   = isExcluded ? 0.25 : 0.75;

  return (
    <>
      <Polyline
        positions={[from, to]}
        pathOptions={{
          color: edgeColor,
          weight: isExcluded ? 1.5 : 2.5,
          dashArray: isExcluded ? "3 6" : "7 5",
          opacity,
        }}
      />
      {/* Flow direction dot (only on active edges) */}
      {!isExcluded && (
        <CircleMarker
          center={[arrowLat, arrowLon]}
          radius={3}
          pathOptions={{
            color: edgeColor,
            fillColor: edgeColor,
            fillOpacity: 1,
            weight: 0,
          }}
        />
      )}
    </>
  );
}

/**
 * 地图主组件
 *
 * Props:
 *   onMapClick(latlng)        - 用户点击地图空白处的回调
 *   onNodeClick(nodeData)     - 用户点击某个 Agent 节点的回调（传给右侧 Inspector）
 *   selectedPoint             - 当前选中的坐标 {lat, lng}
 *   topology                  - { nodes: [], edges: [] }
 *   activeComid               - 当前正在推理的节点 comid
 */
export default function MapView({
  onMapClick,
  onNodeClick,
  selectedPoint,
  topology,
  activeComid,
  googleMapsKey,    // optional — when set, use Google Maps tiles instead of OSM
}) {
  // 默认中心：北美中部
  const defaultCenter = [39.5, -98.35];
  const defaultZoom   = 4;

  return (
    <div className="map-wrapper">
      {/* ── 图例（右上角悬浮）── */}
      <div className="map-legend">
        {[
          { color: COLOR.inlet,    label: "Has USGS data" },
          { color: COLOR.active,   label: "Inferring" },
          { color: COLOR.done,     label: "Done" },
          { color: COLOR.target,   label: "Target segment" },
          { color: COLOR.pending,  label: "Queued" },
          { color: COLOR.excluded, label: "Excluded (no data path)" },
        ].map(({ color, label }) => (
          <div className="legend-item" key={label}>
            <span style={{ background: color }} />
            {label}
          </div>
        ))}
      </div>

      <MapContainer
        center={defaultCenter}
        zoom={defaultZoom}
        style={{ width: "100%", height: "100%" }}
      >
        {/* Base tile layer — Google Maps when a key is available, OSM otherwise */}
        {googleMapsKey ? (
          <TileLayer
            attribution="&copy; Google Maps"
            url={`https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&key=${googleMapsKey}`}
          />
        ) : (
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
        )}

        {/* 地图点击监听 */}
        <ClickHandler onMapClick={onMapClick} />

        {/* 自动缩放控制器（拓扑出现时触发）*/}
        <FitBoundsController nodes={topology.nodes} />

        {/* 用户手动选择的查询点 */}
        {selectedPoint && (
          <Marker position={[selectedPoint.lat, selectedPoint.lng]}>
            <Popup>
              📍 查询点<br />
              {selectedPoint.lat.toFixed(6)}, {selectedPoint.lng.toFixed(6)}
            </Popup>
          </Marker>
        )}

        {/* ── 推理链路：有方向的边 ── */}
        {topology.edges.map((edge, i) => {
          const fromNode = topology.nodes.find((n) => n.comid === edge.from);
          const toNode   = topology.nodes.find((n) => n.comid === edge.to);
          return <DirectedEdge key={i} fromNode={fromNode} toNode={toNode} isExcluded={edge.is_excluded} />;
        })}

        {/* ── Agent nodes ── */}
        {deduplicateCoords(topology.nodes).map((node) => {
          // Skip rendering if coordinates are not yet available.
          // With the pre-fetch in graph.py this should only happen on the
          // very first render frame before the topology event arrives.
          if (!node.lat || !node.lon) return null;

          const color    = getNodeColor(node, activeComid);
          const isActive = node.comid === activeComid;
          const radius   = node.is_target ? 12 : node.is_inlet ? 10 : 8;

          return (
            <span key={node.comid}>
              {/* 脉冲光晕：仅在该节点正在推理时显示 */}
              {isActive && (
                <CircleMarker
                  center={[node.lat, node.lon]}
                  radius={radius + 8}
                  pathOptions={{
                    color: COLOR.active,
                    fillColor: COLOR.active,
                    fillOpacity: 0.2,
                    weight: 1.5,
                    opacity: 0.5,
                  }}
                />
              )}

              {/* Main node circle – dimmed if excluded from inference chain */}
              <CircleMarker
                center={[node.lat, node.lon]}
                radius={radius}
                pathOptions={{
                  color,
                  fillColor: color,
                  fillOpacity: node.is_excluded ? 0.3 : 0.9,
                  opacity:     node.is_excluded ? 0.4 : 1,
                  weight: isActive ? 3 : 1.5,
                }}
                // Click node → open in right-panel Inspector
                eventHandlers={{
                  click: () => onNodeClick && onNodeClick(node),
                }}
              >
                <Popup maxWidth={300}>
                  <NodePopupContent node={node} />
                </Popup>
              </CircleMarker>
            </span>
          );
        })}
      </MapContainer>
    </div>
  );
}

/**
 * 节点 Popup 内容（点击地图上的圆圈后弹出）
 */
function NodePopupContent({ node }) {
  return (
    <div className="node-popup">
      <div className="popup-header">
        {node.is_target && "🎯 "}
        {node.is_inlet  && "📡 "}
        <strong>COMID: {node.comid}</strong>
      </div>

      {node.has_usgs_site && (
        <div className="popup-row">
          <span>USGS 站点</span>
          <span>{node.site_id}</span>
        </div>
      )}

      <div className="popup-row">
        <span>坐标</span>
        <span>{node.lat?.toFixed(4)}, {node.lon?.toFixed(4)}</span>
      </div>

      {/* 推理完成后显示结果摘要 */}
      {node.result && (
        <div className="popup-result">
          <div className="popup-result-title">推理结果</div>
          {node.result.parameters?.map((p, i) => (
            <div key={i} className="popup-param">
              <span>{p.name}</span>
              <span>
                {p.estimate_value != null
                  ? `${p.estimate_value} ${p.unit || ""}`
                  : "N/A"}
              </span>
            </div>
          ))}
          {node.result.estimated_flow != null && (
            <div className="popup-param">
              <span>流量</span>
              <span>{node.result.estimated_flow} cfs</span>
            </div>
          )}
          <div className="popup-confidence">
            置信度: {((node.result.confidence || 0) * 100).toFixed(0)}%
          </div>
          <div className="popup-hint">点击节点 → 右侧查看完整 LLM 详情</div>
        </div>
      )}

      {!node.result && (
        <div className="popup-hint">推理完成后可在右侧 Inspector 查看详情</div>
      )}
    </div>
  );
}