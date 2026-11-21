import { bodyLimit } from 'hono/body-limit'
import type { Hono } from 'hono'
import type { AppContext } from '../../worker'
import { resolveAuth } from '../server/http-routes'
import { startCheckout, stripeOutcome, stripeSettings, verifyStripeSignature, type CreditOrder } from './payments'

const GROUPS: Record<string, Record<string, string>> = {
  public: { network: 'GET', ledger: 'GET' },
  user: { account: 'GET', request: 'POST', enroll: 'POST', revoke: 'POST', checkout: 'POST' },
  node: { profile: 'GET', claim: 'POST', result: 'POST', pause: 'POST' },
  reference: { claim: 'POST', result: 'POST' },
}

export function registerNeuroGrid(app: Hono<AppContext>) {
  app.use('/api/grid/*', bodyLimit({
    maxSize: 410_000,
    onError: (c) => c.json({ error: 'body_too_large' }, 413),
  }))
  app.post('/api/grid/stripe/webhook', async (c) => {
    const settings = stripeSettings(c.env)
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
    if (role === 'user') {
      const auth = await resolveAuth(c.req.raw, c.env)
      if (!auth) return c.json({ error: 'unauthorized' }, 401)
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
    if (role === 'user' && action === 'checkout' && response.ok) {
      const settings = stripeSettings(c.env)
      if (!settings) return c.json({ error: 'checkout_disabled' }, 503)
      try {
        const url = await startCheckout(settings, await response.json<CreditOrder>())
        if (url) return c.json({ url }, 200, { 'Cache-Control': 'no-store' })
      } catch {
        return c.json({ error: 'checkout_pending' }, 503)
      }
      return c.json({ error: 'checkout_pending' }, 503)
    }
    const output = new Response(response.body, response)
    output.headers.set('Cache-Control', 'no-store')
    return output
  })
}
