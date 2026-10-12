import { defineConfig, devices } from '@playwright/test'

/**
 * Browser acceptance for the dashboard UI.
 *
 * Starts the real FastAPI server (fixture dataset + built SPA) through
 * Playwright's webServer, then checks the five pages at the three viewports
 * the spec requires: 1440 desktop, 768 tablet, 375 mobile. Screenshots land in
 * dashboard/screenshots/ for review.
 *
 * The webServer reuses an already-running server on 8787 (local dev), and
 * spawns `uv run weatherquant-dashboard` otherwise (CI).
 */
const ROOT = '../..'

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  snapshotPathTemplate: '{testDir}/screenshots/{projectName}-{arg}{ext}',
  use: {
    baseURL: 'http://127.0.0.1:8787',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'uv run weatherquant-dashboard',
    cwd: ROOT,
    url: 'http://127.0.0.1:8787/healthz',
    reuseExistingServer: true,
    timeout: 60_000,
    env: {
      WQ_DATASET: 'fixture',
      WQ_COLLECTOR_DATA_DIR: 'dashboard/fixtures/data/collector',
      WQ_SHADOW_DATA_DIR: 'dashboard/fixtures/data/shadow',
      WQ_STATIC_DIR: 'dashboard/frontend/dist',
      WQ_RATE_LIMIT_PER_MINUTE: '0',
    },
  },
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'tablet',
      use: { ...devices['Desktop Chrome'], viewport: { width: 768, height: 1024 } },
    },
    {
      name: 'mobile',
      use: { ...devices['Desktop Chrome'], viewport: { width: 375, height: 812 } },
    },
  ],
})
