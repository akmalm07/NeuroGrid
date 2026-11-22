import { createHmac } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../../worker'
import { startCheckout, stripeOutcome, stripeSettings, verifyStripeSignature } from './payments'
import { purchaseCents } from './protocol'

const id = '1e27e7d5-659d-4c6b-9f6e-c6c61f657bc0'

it('enables live checkout only on its configured HTTPS origin', () => {
  const origin = 'https://neurogrid.app.space'
  const local = 'http://localhost:5173'
  const settings = {
    NEUROGRID_STRIPE_KEY: 'rk_live_fixture',
    NEUROGRID_STRIPE_WEBHOOK: 'whsec_fixture',
    NEUROGRID_STRIPE_ORIGIN: origin,
  } as Env
  expect(stripeSettings(settings, origin)?.live).toBe(true)
  expect(stripeSettings(settings, local)).toBeUndefined()
  expect(stripeSettings({ ...settings, NEUROGRID_STRIPE_ORIGIN: local }, local)).toBeUndefined()
  expect(stripeSettings({
    ...settings, NEUROGRID_STRIPE_KEY: 'rk_test_fixture', NEUROGRID_STRIPE_ORIGIN: local,
  }, local)?.live).toBe(false)
})

it('prices whole-credit orders at one cent per credit, with a one-dollar minimum', () => {
  expect(purchaseCents(100)).toBe(100)
  expect(purchaseCents(253)).toBe(253)
  expect(purchaseCents(10_000)).toBe(10_000)
  for (const invalid of [99, 100.1, 10_001, '100', null, undefined, Infinity]) {
    expect(purchaseCents(invalid)).toBeUndefined()
  }
})
const event = (type: string, status: string, paymentStatus: string) => JSON.stringify({
  type, livemode: false, data: { object: {
    object: 'checkout.session', id: 'cs_test_example', mode: 'payment',
    status, payment_status: paymentStatus, livemode: false, currency: 'usd',
    client_reference_id: id, metadata: { neurogrid_order: id }, amount_total: 500,
  } },
})

describe('Stripe webhook verification', () => {
  it('accepts the signed body and rejects a changed or expired request', () => {
    const payload = event('checkout.session.completed', 'complete', 'paid')
    const timestamp = 1_765_000_000
    const secret = 'whsec_fixture'
    const hmac = createHmac('sha256', secret).update(`${timestamp}.${payload}`).digest('hex')
    const signature = `t=${timestamp},v1=${hmac}`
    const now = timestamp * 1_000
    expect(verifyStripeSignature(payload, signature, secret, now)).toBe(true)
    expect(verifyStripeSignature(payload + ' ', signature, secret, now)).toBe(false)
    expect(verifyStripeSignature(payload, signature, secret, now + 300_001)).toBe(false)
  })

  it('fulfills only a paid Checkout Session in the configured mode', () => {
    expect(stripeOutcome('{', false)).toBeUndefined()
    const paid = event('checkout.session.completed', 'complete', 'paid')
    expect(stripeOutcome(paid, false)).toEqual({
      id, cents: 500, live: false, sessionId: 'cs_test_example', status: 'paid',
    })
    expect(stripeOutcome(paid, true)).toBeUndefined()
    expect(stripeOutcome(event('checkout.session.completed', 'complete', 'unpaid'), false)).toBeUndefined()
    expect(stripeOutcome(event('checkout.session.async_payment_succeeded', 'complete', 'paid'), false)?.status).toBe('paid')
  })

  it('releases only a confirmed expiration or asynchronous failure', () => {
    expect(stripeOutcome(event('checkout.session.expired', 'expired', 'unpaid'), false)?.status).toBe('released')
    expect(stripeOutcome(event('checkout.session.async_payment_failed', 'complete', 'unpaid'), false)?.status).toBe('released')
    expect(stripeOutcome(event('checkout.session.expired', 'expired', 'paid'), false)).toBeUndefined()
  })
})

it('retries Checkout creation with a stable idempotency key and the reserved price', async () => {
  const fetchStripe = vi.fn<typeof fetch>().mockImplementation(async () =>
    Response.json({ status: 'open', url: 'https://checkout.stripe.com/example' }))
  vi.stubGlobal('fetch', fetchStripe)
  try {
    const settings = { key: 'sk_test_fixture', secret: 'whsec_fixture', origin: 'https://neurogrid.app.space', live: false }
    const order = { id, owner: 'acct:test', created: 0, origin: settings.origin, live: false,
      credits: 253, cents: 253, status: 'pending' as const }
    await expect(startCheckout(settings, order)).resolves.toBe('https://checkout.stripe.com/example')
    await startCheckout(settings, order)
    const first = fetchStripe.mock.calls[0][1]
    const second = fetchStripe.mock.calls[1][1]
    expect(first?.headers).toMatchObject({ 'Idempotency-Key': `neurogrid-${id}` })
    expect(second?.headers).toEqual(first?.headers)
    expect(String(second?.body)).toBe(String(first?.body))
    expect(new URLSearchParams(String(first?.body)).get('line_items[0][price_data][unit_amount]')).toBe('253')
  } finally {
    vi.unstubAllGlobals()
  }
})
