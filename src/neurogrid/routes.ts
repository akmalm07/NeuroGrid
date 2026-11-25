import { bodyLimit } from 'hono/body-limit'
import type { Hono } from 'hono'
import type { AppContext } from '../../worker'
import { resolveAuth } from '../server/http-routes'
import { startCheckout, stripeOutcome, stripeSettings, verifyStripeSignature, type CreditOrder } from './payments'
import { purchaseCents } from './protocol'

const GROUPS: Record<string, Record<string, string>> = {
  public: { network: 'GET', ledger: 'GET' },
  user: { account: 'GET', quote: 'POST', request: 'POST', enroll: 'POST', revoke: 'POST' },
  node: { profile: 'GET', claim: 'POST', ping: 'POST', result: 'POST', pause: 'POST' },
  reference: { claim: 'POST', result: 'POST' },
}

function referenceBeta(url: URL, stagingOrigin?: string) {
  return ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
    || url.protocol === 'https:' && url.origin === stagingOrigin
}

export function registerNeuroGrid(app: Hono<AppContext>) {
  app.use('/api/grid/*', bodyLimit({
    maxSize: 710_000,
    onError: (c) => c.json({ error: 'body_too_large' }, 413),
  }))
  for (const action of ['quote', 'request']) {
    app.use(`/api/grid/user/${action}`, bodyLimit({
      maxSize: 110_000,
      onError: (c) => c.json({ error: 'invalid_request' }, 413),
    }))
  }
  app.get('/api/grid/public/checkout', (c) => {
    const url = new URL(c.req.url)
    const settings = referenceBeta(url, c.env.NEUROGRID_REFERENCE_BETA_ORIGIN) && stripeSettings(c.env, url.origin)
    return c.json({ mode: settings && !settings.live ? 'test' : 'disabled' }, 200,
      { 'Cache-Control': 'no-store' })
  })
  app.post('/api/grid/user/checkout', async (c) => {
    const auth = await resolveAuth(c.req.raw, c.env)
    if (!auth) return c.json({ error: 'unauthorized' }, 401)
    const data = await c.req.json<{ credits?: unknown }>().catch(() => undefined)
    if (purchaseCents(data?.credits) === undefined) return c.json({ error: 'invalid_credit_amount' }, 400)
    const url = new URL(c.req.url)
    const settings = stripeSettings(c.env, url.origin)
    if (!settings || settings.live || !referenceBeta(url, c.env.NEUROGRID_REFERENCE_BETA_ORIGIN)) {
      return c.json({ error: 'checkout_disabled' }, 503)
    }
    const response = await c.env.NEUROGRID.getByName('main').fetch('https://neurogrid.internal/payments/reserve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Grid-User': auth.userId },
      body: JSON.stringify({ credits: data?.credits, live: settings.live, origin: settings.origin }),
    })
    if (!response.ok) return new Response(response.body, {
      status: response.status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    })
    try {
      const url = await startCheckout(settings, await response.json<CreditOrder>())
      if (url) return c.json({ url }, 200, { 'Cache-Control': 'no-store' })
    } catch {
      // If Stripe timed out, retain the reservation for an idempotent retry.
    }
    return c.json({ error: 'checkout_pending' }, 503)
  })
  app.post('/api/grid/stripe/webhook', async (c) => {
    const settings = stripeSettings(c.env, new URL(c.req.url).origin)
    if (!settings) return c.notFound()
    const payload = await c.req.text()
    const signature = c.req.header('Stripe-Signature') ?? ''
    if (!verifyStripeSignature(payload, signature, settings.secret, Date.now())) {
      return c.json({ error: 'invalid_signature' }, 400)
    }
    const outcome = stripeOutcome(payload, settings.live)
    if (!outcome) return c.json({ received: true })
    return c.env.NEUROGRID.getByName('main').fetch('https://neurogrid.internal/payments/outcome', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(outcome),
    })
  })
  app.all('/api/grid/:role/:action', async (c) => {
    const { role, action } = c.req.param()
    if (!Object.hasOwn(GROUPS, role) || !Object.hasOwn(GROUPS[role], action)
        || GROUPS[role][action] !== c.req.method) return c.notFound()
    const headers = new Headers({ 'Content-Type': 'application/json' })
    const colo = c.req.raw.cf?.colo
    if (typeof colo === 'string' && /^[A-Z]{3}$/.test(colo)) headers.set('X-Grid-Colo', colo)
    if (role === 'user') {
      const auth = await resolveAuth(c.req.raw, c.env)
      if (!auth) return c.json({ error: 'unauthorized' }, 401)
      if (['quote', 'request'].includes(action) && !referenceBeta(new URL(c.req.url), c.env.NEUROGRID_REFERENCE_BETA_ORIGIN)) {
        return c.json({ error: 'preview_only' }, 503)
      }
      headers.set('X-Grid-User', auth.userId)
    } else if (role !== 'public') {
      const authorization = c.req.header('Authorization')
      if (!authorization?.startsWith('Bearer ') || authorization.length > 200) {
        return c.json({ error: 'unauthorized' }, 401)
      }
      headers.set('X-Grid-Credential', authorization.slice(7))
    }
    const stub = c.env.NEUROGRID.getByName('main')
    const query = new URL(c.req.url).search
    const response = await stub.fetch(`https://neurogrid.internal/${role}/${action}${query}`, {
      method: c.req.method, headers, body: c.req.method === 'POST' ? await c.req.text() : undefined,
    })
    const output = new Response(response.body, response)
    output.headers.set('Cache-Control', 'no-store')
    return output
  })
}
