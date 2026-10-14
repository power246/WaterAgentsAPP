import { Fragment, useEffect, useMemo } from 'react'
import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from 'react-leaflet'
import type { LatLngBoundsExpression } from 'leaflet'
import type { RunDetail, RunSnapshot } from '../../api/http'
import { nodeVisual, visualLabels, type Graph, type NodeVisual } from './models'

const colors: Record<NodeVisual, string> = {
  excluded: '#8d98a3', pending: '#778797', running: '#2176cf',
  completed: '#258056', failed: '#c94540', cancelled: '#d27925',
  'cancelled-before-start': '#78818a',
}

function nodePresentation(node: Graph['nodes'][number], snapshot: RunSnapshot | null,
  sessions: RunDetail['sessions'] | undefined, runStatus: string) {
  const summary = snapshot?.waterbodies.find((item) => item.target_comid === node.comid)
  const session = snapshot?.sessions.find((item) => item.target_comid === node.comid)
    ?? sessions?.find((item) => item.target_comid === node.comid)
  const sessionId = summary?.session_id ?? session?.session_id ?? null
  const status = summary?.session_status ?? session?.status
  return { sessionId, visual: nodeVisual(summary?.participating ?? node.participation === 'active', status, runStatus) }
}

function Viewport({ points }: { points: [number, number][] }) {
  const map = useMap()
  useEffect(() => {
    if (points.length) map.fitBounds(points as LatLngBoundsExpression, { padding: [24, 24], maxZoom: 11 })
  }, [map, points])
  return null
}

function PickPoint({ onPick }: { onPick?: (latitude: number, longitude: number) => void }) {
  useMapEvents({ click: (event) => onPick?.(event.latlng.lat, event.latlng.lng) })
  return null
}

export function TopologyMap({ graph, snapshot, sessions, runStatus, selectedSessionId, onSelect, onPick, topologyMessage }: {
  graph: Graph | null
  snapshot: RunSnapshot | null
  sessions?: RunDetail['sessions']
  runStatus: string
  selectedSessionId: string | null
  onSelect: (comid: string, sessionId: string | null) => void
  onPick?: (latitude: number, longitude: number) => void
  topologyMessage?: string
}) {
  const nodes = graph?.nodes ?? []
  const located = useMemo(() => (graph?.nodes ?? []).map((node) => {
    const geometry = node.geometry_ref ? graph?.geometries?.[node.geometry_ref] : undefined
    const point = node.point ?? geometry?.representative_point
    return point ? { node, geometry, point } : null
  }).filter((value) => value !== null), [graph])
  const points: [number, number][] = useMemo(() => located.map(({ point }) => [point.latitude, point.longitude]), [located])
  return <section className="map-panel" aria-label="Topology map">
    <div className="panel-title"><h2>Topology</h2><span>{nodes.length ? `${nodes.length} waterbodies` : topologyMessage ?? 'No topology yet'}</span></div>
    <div className="map-frame">
      <MapContainer center={[39, -97]} zoom={4} scrollWheelZoom className="map-canvas">
        <TileLayer attribution='&copy; OpenStreetMap contributors' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
        <Viewport points={points} />
        <PickPoint onPick={onPick} />
        {located.map(({ node, point }) => {
          const { sessionId, visual } = nodePresentation(node, snapshot, sessions, runStatus)
          const selected = Boolean(sessionId && sessionId === selectedSessionId)
          return <Fragment key={node.comid}>
            {visual === 'running' && <CircleMarker center={[point.latitude, point.longitude]} radius={15}
              pathOptions={{ color: colors.running, fillOpacity: 0, weight: 2 }} className="running-halo" />}
            <CircleMarker center={[point.latitude, point.longitude]} radius={selected ? 10 : 7}
              pathOptions={{ color: colors[visual], fillColor: colors[visual], fillOpacity: visual === 'excluded' ? 0.35 : 0.9,
                weight: selected ? 3 : 2, dashArray: visual === 'cancelled-before-start' ? '3 3' : undefined }}
              eventHandlers={{ click: () => onSelect(node.comid, sessionId) }}>
              <Tooltip>{node.comid}: {visualLabels[visual]}{selected ? ' · selected' : ''}</Tooltip>
            </CircleMarker>
          </Fragment>
        })}
        {located.map(({ node, geometry }) => geometry && <Polyline key={`line-${node.comid}`}
          positions={geometry.simplified_coordinates.map(([longitude, latitude]) => [latitude, longitude])}
          pathOptions={{ color: '#6c9fc2', weight: 2, opacity: 0.65 }} />)}
      </MapContainer>
    </div>
    <div className="legend" aria-label="Node status legend">
      {(Object.keys(visualLabels) as NodeVisual[]).map((visual) =>
        <span key={visual}><i className={`legend-dot ${visual}`} />{visualLabels[visual]}</span>)}
    </div>
    <div className="node-list" aria-label="Waterbody list">
      {nodes.map((node) => {
        const { sessionId, visual } = nodePresentation(node, snapshot, sessions, runStatus)
        return <button key={node.comid} type="button" onClick={() => onSelect(node.comid, sessionId)}>
          <i className={`legend-dot ${visual}`} />COMID {node.comid} — {visualLabels[visual]}
        </button>
      })}
    </div>
  </section>
}
