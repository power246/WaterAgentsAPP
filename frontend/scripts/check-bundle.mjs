import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const dist = resolve(import.meta.dirname, '..', 'dist')
function walk(path) {
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    const target = join(path, entry.name)
    if (entry.isDirectory()) { walk(target); continue }
    if (!/\.(js|css|html)$/.test(entry.name)) continue
    const content = readFileSync(target, 'utf8')
    if (/frontend_bk|backend_bk|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}/.test(content)) {
      throw new Error(`Unsafe build output: ${target}`)
    }
  }
}
walk(dist)
