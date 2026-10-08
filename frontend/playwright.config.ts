import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: {
    baseURL: process.env.WATERAGENTS_E2E_BASE_URL ?? 'http://127.0.0.1:5173', browserName: 'chromium', headless: true,
    channel: process.env.WATERAGENTS_BROWSER_CHANNEL === 'chrome' ? 'chrome' : undefined,
  },
  webServer: process.env.WATERAGENTS_E2E_BASE_URL ? undefined : {
    command: 'npm run dev -- --host 127.0.0.1',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
