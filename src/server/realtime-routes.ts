import type { Hono } from 'hono'
import type { AppContext } from '../../worker'
import { verifyAppToken } from './http-routes'

export function registerRealtimeRoutes(app: Hono<AppContext>) {
  app.get('/ws/:scopeId', async (c) => {
    const scope = `app:${c.env.DEEPSPACE_APP_ID}`
    if (c.req.param('scopeId') !== scope || c.req.header('Upgrade')?.toLowerCase() !== 'websocket') {
      return c.notFound()
    }
    const url = new URL(c.req.url)
    const token = url.searchParams.get('token')
    const auth = token ? await verifyAppToken(token, c.env) : null
    if (token && !auth) return new Response('Unauthorized', { status: 401 })

    // Rebuild identity from the verified token before forwarding the upgrade.
    for (const key of ['token', 'userId', 'userName', 'userEmail', 'userImageUrl', 'role']) {
      url.searchParams.delete(key)
    }
    if (auth) {
      url.searchParams.set('userId', auth.userId)
      if (auth.claims.name) url.searchParams.set('userName', auth.claims.name)
      if (auth.claims.email) url.searchParams.set('userEmail', auth.claims.email)
      if (auth.claims.image) url.searchParams.set('userImageUrl', auth.claims.image)
    }
    return c.env.RECORD_ROOMS.getByName(scope).fetch(new Request(url, c.req.raw))
  })
}
