import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const source = resolve(import.meta.dirname, '..', 'src')
const forbidden = [
  /frontend_bk/i,
  /\bWebSocket\b/,
  /\bEventSource\b/,
  /\blocalStorage\b/,
  /\bsessionStorage\b/,
  /\/api\/[^'"`]*\/agents\/[^'"`]*\/cancel/,
]
function walk(path) {
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    const target = join(path, entry.name)
    if (entry.isDirectory()) { walk(target); continue }
    if (!/\.(ts|tsx)$/.test(entry.name) || entry.name === 'generated.ts') continue
    const content = readFileSync(target, 'utf8')
    for (const pattern of forbidden) {
      if (pattern.test(content)) throw new Error(`Frontend boundary failed: ${target} matches ${pattern}`)
    }
  }
}
walk(source)
