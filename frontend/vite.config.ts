import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
      '/openapi.json': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  test: {
    include: ['tests/**/*.{test,spec}.{ts,tsx}'],
    environment: 'jsdom', setupFiles: ['./tests/setup.ts'],
  },
})
