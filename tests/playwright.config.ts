import { defineConfig } from '@playwright/test'

/**
 * Port comes from $DEEPSPACE_PORT (set by `deepspace test run [--port N]`),
 * defaulting to 5173. The same port is passed to vite via --strictPort so
 * a busy address fails fast rather than silently rebinding to 5174.
 *
 * To run multiple apps in parallel, give each one a different port:
 *   npx deepspace dev start --port=5180 --host=127.0.0.1
 *   npx deepspace test run --port=5180
 */
const PORT = Number(process.env.DEEPSPACE_PORT ?? 5173)
const existingServer = process.env.NEUROGRID_TEST_URL
const BASE_URL = existingServer || `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.ts',
  globalSetup: existingServer ? undefined : './helpers/global-setup.ts',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: BASE_URL,
    headless: true,
  },
  webServer: existingServer ? undefined : {
    command: `npx vite --port ${PORT} --strictPort --host 127.0.0.1`,
    cwd: '..',
    url: BASE_URL,
    reuseExistingServer: true,
    timeout: 30_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
