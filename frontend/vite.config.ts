import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const apiTarget = process.env.WATERAGENTS_DEV_API_TARGET ?? 'http://127.0.0.1:8000'
const proxy = {
  target: apiTarget,
  changeOrigin: true,
  configure(server: { on: (name: string, listener: (request: { setHeader: (name: string, value: string) => void }, source: { headers: { origin?: string; host?: string } }) => void) => void }) {
    server.on('proxyReq', (request, source) => {
      if (source.headers.host && source.headers.origin === `http://${source.headers.host}`) {
        request.setHeader('Origin', apiTarget)
      }
    })
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: {
      '/api': proxy,
      '/openapi.json': proxy,
    },
  },
  test: {
    include: ['tests/**/*.{test,spec}.{ts,tsx}'],
    environment: 'jsdom', setupFiles: ['./tests/setup.ts'],
  },
})
