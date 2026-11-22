/**
 * HTTP routes that proxy app requests to DeepSpace platform services.
 *
 * The app worker remains the security boundary: caller identity is derived
 * from a verified JWT, caller-supplied identity headers are removed, and app
 * identity is re-asserted only from worker bindings.
 */

import type { Hono } from 'hono'
import {
  apiWorkerFetch,
  authWorkerFetch,
  BROWSER_PROXY_ROUTES,
  isPlatformReservedPath,
  verifyJwt,
} from 'deepspace/worker'
import type { JwtVerifierConfig, VerifyResult } from 'deepspace/worker'
import type { AppContext, Env } from '../../worker.js'

export async function verifyAppToken(token: string, env: Env): Promise<VerifyResult | null> {
  const config: JwtVerifierConfig = { publicKey: env.AUTH_JWT_PUBLIC_KEY, issuer: env.AUTH_JWT_ISSUER }
  return (await verifyJwt(config, token)).result
}

export function resolveAuth(req: Request, env: Env) {
  const header = req.headers.get('Authorization')
  return header?.startsWith('Bearer ') ? verifyAppToken(header.slice(7), env) : null
}

/**
 * Re-assert this app's identity headers on a proxied request, fail-closed.
 * Never forwards caller-supplied identity; attaches ours only when the app
 * has a token (absent pre-first-deploy — see Env.APP_IDENTITY_TOKEN).
 */
function reassertAppIdentity(headers: Headers, env: Env): void {
  headers.delete('x-app-identity-token')
  headers.delete('x-app-id')
  if (!env.APP_IDENTITY_TOKEN) return
  headers.set('x-app-identity-token', env.APP_IDENTITY_TOKEN)
  headers.set('x-app-id', env.DEEPSPACE_APP_ID)
}

/** Register the app-scoped DeepSpace auth proxy. */
export function registerAuthRoutes(app: Hono<AppContext>): void {
  // Social OAuth redirect + code exchange.
  app.get('/api/auth/social-redirect', (c) => {
    const provider = c.req.query('provider')
    if (!provider) return c.json({ error: 'Missing provider' }, 400)

    const appOrigin = new URL(c.req.url).origin
    const authOrigin = new URL(c.env.AUTH_WORKER_URL).origin

    return c.redirect(
      `${authOrigin}/login/social?provider=${encodeURIComponent(provider)}&returnTo=${encodeURIComponent(appOrigin)}`,
    )
  })

  app.get('/api/auth/oauth-complete', async (c) => {
    const code = c.req.query('code')
    const appOrigin = new URL(c.req.url).origin
    // Land the signed-in user in the app, not on the static landing. `/` is a
    // static page (no auth/realtime providers), so redirecting there after auth
    // would strand the user; `/home` is the dynamic app boundary.
    const appHome = `${appOrigin}/home`

    if (!code) return c.redirect(appHome)

    const res = await authWorkerFetch(c.env, '/api/auth/exchange-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })

    if (!res.ok) return c.redirect(appHome)
    const data = (await res.json()) as { sessionToken?: string }
    if (!data.sessionToken) return c.redirect(appHome)
    const sessionToken = data.sessionToken

    return new Response(null, {
      status: 302,
      headers: {
        Location: appHome,
        'Set-Cookie': `__Secure-better-auth.session_token=${encodeURIComponent(sessionToken)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`,
      },
    })
  })

  app.all('/api/auth/sign-out', async (c) => {
    try {
      await authWorkerFetch(c.env, '/api/auth/sign-out', {
        method: c.req.method,
        headers: c.req.raw.headers,
        body: c.req.method !== 'GET' && c.req.method !== 'HEAD' ? c.req.raw.body : undefined,
      })
    } catch {
      // Still expire the app-scoped cookie below. A network/auth-worker
      // failure must not leave the browser immediately signed back in.
    }

    return new Response(JSON.stringify({ success: true }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Set-Cookie':
          '__Secure-better-auth.session_token=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0',
      },
    })
  })

  // Auth proxy → auth-worker (same-origin cookies).
  app.all('/api/auth/*', async (c) => {
    const url = new URL(c.req.url)
    const res = await authWorkerFetch(c.env, url.pathname + url.search, {
      method: c.req.method,
      headers: c.req.raw.headers,
      body: c.req.method !== 'GET' && c.req.method !== 'HEAD' ? c.req.raw.body : undefined,
    })
    const headers = new Headers(res.headers)
    const setCookie = headers.get('set-cookie')
    if (setCookie) {
      headers.set('set-cookie', setCookie.replace(/;\s*Domain=[^;]*/gi, ''))
    }
    return new Response(res.body, { status: res.status, headers })
  })

  // Debug proxy → app's RecordRoom DO.
  //
  // Both this proxy and the DO gate on ALLOW_DEBUG_ROUTES === "true". The
  // debug handler has no auth of its own once reached, so production and
  // staging environments must enable it deliberately through app secrets.
  app.all('/api/debug/*', async (c) => {
    if (c.env.ALLOW_DEBUG_ROUTES !== 'true') {
      return c.notFound()
    }
    const stub = c.env.RECORD_ROOMS.get(
      c.env.RECORD_ROOMS.idFromName(`app:${c.env.DEEPSPACE_APP_ID}`),
    )
    // Forward verbatim: the DO dispatches on the original pathname.
    return stub.fetch(c.req.raw)
  })

}

export function registerPlatformProxyRoutes(app: Hono<AppContext>): void {
  // Same-origin browser proxy for authenticated SDK hooks. Keep this an exact
  // (method, path) allowlist: prefix matching would expose deploy/CLI routes.
  app.all('/_deepspace/*', async (c) => {
    const url = new URL(c.req.url)
    const method = c.req.method
    const route = BROWSER_PROXY_ROUTES.find(
      (candidate) => candidate.method === method && candidate.path === url.pathname,
    )
    if (!route) {
      return c.json({ error: 'not_found' }, 404)
    }

    const auth = await resolveAuth(c.req.raw, c.env)
    if (!auth?.userId) return c.json({ error: 'unauthorized' }, 401)

    // Always overwrite caller input with this worker's immutable app id.
    const forwardedParams = new URLSearchParams(url.search)
    forwardedParams.set('appId', c.env.DEEPSPACE_APP_ID)
    const queryString = forwardedParams.toString()
    const apiPath =
      url.pathname.replace('/_deepspace/', '/api/') + (queryString ? `?${queryString}` : '')

    const headers = new Headers(c.req.raw.headers)
    headers.delete('x-user-id')
    reassertAppIdentity(headers, c.env)
    headers.set('x-user-id', auth.userId)

    return apiWorkerFetch(c.env, apiPath, {
      method,
      headers,
      body: ['GET', 'HEAD'].includes(method) ? undefined : c.req.raw.body,
    })
  })
}

const matches = (pathname: string, prefixes: readonly string[]): boolean =>
  prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))

/**
 * An unmatched API call must 404 without consulting the asset layer at all —
 * answering it with a static file, or with HTML a client then parses as JSON,
 * is never right.
 */
const API_PREFIXES = ['/api']

/**
 * A FILE, not a client route: last segment carries an extension. Wrong in only
 * the cheap direction — a dotted route 404s visibly, where a missing file
 * getting the shell is HTML parsed as JavaScript and a blank page.
 */
function namesAFile(pathname: string): boolean {
  const last = pathname.slice(pathname.lastIndexOf('/') + 1)
  return last.includes('.')
}

/**
 * Register the client-route fallback last so it cannot shadow worker routes.
 * Deploys set `not_found_handling: "none"`, so a 404 from the binding is a
 * real miss and this worker makes the call the asset layer cannot.
 */
export function registerStaticRoutes(app: Hono<AppContext>): void {
  app.get('*', async (c) => {
    const url = new URL(c.req.url)
    if (matches(url.pathname, API_PREFIXES)) {
      return c.json({ error: 'not_found' }, 404)
    }
    const response = await c.env.ASSETS.fetch(c.req.raw)
    if (response.status !== 404) return response

    if (namesAFile(url.pathname) || isPlatformReservedPath(url.pathname)) {
      return c.json({ error: 'not_found' }, 404)
    }
    // `/`, not `/index.html`: auto-trailing-slash redirects the explicit
    // filename, and the browser would follow it off the URL it asked for.
    url.pathname = '/'
    return c.env.ASSETS.fetch(new Request(url.toString(), c.req.raw))
  })
}
