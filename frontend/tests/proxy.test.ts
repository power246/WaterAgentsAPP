import { afterEach, expect, it } from 'vitest'
import { createServer as createHttpServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { resolve } from 'node:path'
import { createServer as createViteServer } from 'vite'

const previousTarget = process.env.WATERAGENTS_DEV_API_TARGET
afterEach(() => {
  if (previousTarget === undefined) delete process.env.WATERAGENTS_DEV_API_TARGET
  else process.env.WATERAGENTS_DEV_API_TARGET = previousTarget
})

it('forwards SSE chunks immediately and rewrites only same-origin dev mutations', async () => {
  const received: { host?: string; origin?: string }[] = []
  const backend = createHttpServer((request, response) => {
    if (request.url === '/api/probe/sse') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
      response.write('event: probe\ndata: first')
      setTimeout(() => response.write(' second'), 80)
      setTimeout(() => response.end('\n\n'), 160)
      return
    }
    received.push({ host: request.headers.host, origin: request.headers.origin })
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ ok: true }))
  })
  await new Promise<void>((resolve) => backend.listen(0, '127.0.0.1', resolve))
  const backendAddress = backend.address() as AddressInfo
  const backendOrigin = `http://127.0.0.1:${backendAddress.port}`
  process.env.WATERAGENTS_DEV_API_TARGET = backendOrigin
  let vite: Awaited<ReturnType<typeof createViteServer>> | null = null
  try {
    vite = await createViteServer({ configFile: resolve('vite.config.ts'),
      server: { host: '127.0.0.1', port: 0, strictPort: false } })
    await vite.listen()
    const address = vite.httpServer?.address() as AddressInfo
    const frontendOrigin = `http://127.0.0.1:${address.port}`
    const stream = await fetch(`${frontendOrigin}/api/probe/sse`)
    expect(stream.status).toBe(200)
    expect(stream.headers.get('Content-Type')).toContain('text/event-stream')
    const reader = stream.body!.getReader()
    const first = await reader.read()
    expect(new TextDecoder().decode(first.value)).toContain('event: probe\ndata: first')
    expect(first.done).toBe(false)
    let rest = ''
    for (;;) {
      const next = await reader.read()
      if (next.done) break
      rest += new TextDecoder().decode(next.value)
    }
    expect(rest).toBe(' second\n\n')
    await fetch(`${frontendOrigin}/api/probe/mutate`, { method: 'POST',
      headers: { Origin: frontendOrigin, 'Content-Type': 'application/json' }, body: '{}' })
    await fetch(`${frontendOrigin}/api/probe/mutate`, { method: 'POST',
      headers: { Origin: 'https://elsewhere.invalid', 'Content-Type': 'application/json' }, body: '{}' })
    expect(received).toEqual([
      { host: `127.0.0.1:${backendAddress.port}`, origin: backendOrigin },
      { host: `127.0.0.1:${backendAddress.port}`, origin: 'https://elsewhere.invalid' },
    ])
  } finally {
    await vite?.close()
    await new Promise<void>((resolve) => backend.close(() => resolve()))
  }
})
