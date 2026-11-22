/**
 * App Worker — explicit assembly for DeepSpace app routes and Durable Objects.
 *
 * Route implementations live under src/server. Keep their registration order
 * here: specific API and WebSocket handlers must precede the SPA fallback.
 */

import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { CanvasRoom, CronRoom, JobRoom, PresenceRoom, RecordRoom, YjsRoom } from 'deepspace/worker'
import type { DOBindings, DOManifest } from 'deepspace/worker'
import { schemas } from './src/schemas.js'
import {
  registerAuthRoutes,
  registerPlatformProxyRoutes,
  registerStaticRoutes,
} from './src/server/http-routes.js'
import { registerRealtimeRoutes } from './src/server/realtime-routes.js'
import { registerNeuroGrid } from './src/neurogrid/routes'
export { NeuroGridCoordinator } from './src/neurogrid/coordinator'

// Dynamic deploy reads this manifest to create the app's DO bindings.
export const __DO_MANIFEST__ = [
  { binding: 'RECORD_ROOMS', className: 'AppRecordRoom', sqlite: true },
  { binding: 'YJS_ROOMS', className: 'AppYjsRoom', sqlite: true },
  { binding: 'CANVAS_ROOMS', className: 'AppCanvasRoom', sqlite: true },
  { binding: 'PRESENCE_ROOMS', className: 'AppPresenceRoom', sqlite: true },
  { binding: 'CRON_ROOMS', className: 'AppCronRoom', sqlite: true },
  { binding: 'JOB_ROOMS', className: 'AppJobRoom', sqlite: true },
  { binding: 'NEUROGRID', className: 'NeuroGridCoordinator', sqlite: true },
] as const satisfies DOManifest

export class AppRecordRoom extends RecordRoom<Env> {
  constructor(state: DurableObjectState, env: Env) {
    super(state, env, schemas, { ownerUserId: env.OWNER_USER_ID })
  }
}

// Keep the original classes registered for the deployed Durable Object migration.
// NeuroGrid exposes only the RecordRoom and coordinator.
export class AppYjsRoom extends YjsRoom<Env> {}
export class AppCanvasRoom extends CanvasRoom<Env> {}
export class AppPresenceRoom extends PresenceRoom<Env> {}
export class AppCronRoom extends CronRoom<Env> {
  constructor(state: DurableObjectState, env: Env) {
    super(state, env, { tasks: [] })
  }
  protected async onTask(): Promise<void> {}
}
export class AppJobRoom extends JobRoom<Env> {
  protected async onJob(): Promise<never> {
    throw new Error('No DeepSpace background jobs are configured')
  }
}

export interface Env extends DOBindings<typeof __DO_MANIFEST__> {
  ASSETS: Fetcher
  /**
   * HMAC app credential minted on first deploy. Proxy routes omit identity
   * headers when it is absent so upstream services fail closed.
   */
  APP_IDENTITY_TOKEN?: string
  /** API service binding in production with a deepspace-dev URL fallback. */
  API_WORKER?: Fetcher
  API_WORKER_URL?: string
  AUTH_JWT_PUBLIC_KEY: string
  AUTH_JWT_ISSUER: string
  AUTH_WORKER_URL: string
  APP_NAME: string
  /** Immutable record-scope and platform identity. */
  DEEPSPACE_APP_ID: string
  OWNER_USER_ID: string
  /**
   * Enables the unauthenticated /api/debug/* proxy only when exactly "true".
   * deepspace dev/test set it locally; deployed environments must opt in.
   */
  ALLOW_DEBUG_ROUTES?: string
  NEUROGRID_VERIFIER_SECRET?: string
  NEUROGRID_STRIPE_KEY?: string
  NEUROGRID_STRIPE_WEBHOOK?: string
  NEUROGRID_STRIPE_ORIGIN?: string
}

export type AppContext = { Bindings: Env }

const app = new Hono<AppContext>()
app.use('/api/*', cors())

registerAuthRoutes(app)
registerRealtimeRoutes(app)
registerNeuroGrid(app)
registerPlatformProxyRoutes(app)
registerStaticRoutes(app)

export default app
