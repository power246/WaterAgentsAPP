import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import openapiTS, { astToString } from 'openapi-typescript'

const root = resolve(import.meta.dirname, '..', '..')
const python = process.env.WATERAGENTS_PYTHON ?? [
  resolve(root, 'backend', '.venv', 'Scripts', 'python.exe'),
  resolve(root, 'backend', '.venv', 'bin', 'python'),
].find(existsSync) ?? 'python'
const source = [
  'import json',
  'from app.main import create_app',
  'from app.infrastructure.config import AppSettings',
  'print(json.dumps(create_app(AppSettings(_env_file=None)).openapi(), sort_keys=True))',
].join('; ')
const result = spawnSync(python, ['-B', '-c', source], {
  cwd: root,
  encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: resolve(root, 'backend') },
  maxBuffer: 16 * 1024 * 1024,
})
if (result.status !== 0) {
  process.stderr.write(result.stderr || result.error?.message || 'OpenAPI export failed\n')
  process.exit(result.status || 1)
}
const ast = await openapiTS(JSON.parse(result.stdout), { silent: true })
const content = '// Generated from the current backend OpenAPI. Run npm run generate:api.\n' + astToString(ast).replaceAll('\r\n', '\n')
const destination = resolve(root, 'frontend', 'src', 'api', 'generated.ts')
if (process.argv.includes('--check')) {
  if (!existsSync(destination) || readFileSync(destination, 'utf8') !== content) {
    process.stderr.write('Generated API types differ from current backend OpenAPI. Run npm run generate:api.\n')
    process.exit(1)
  }
} else {
  writeFileSync(destination, content)
}
