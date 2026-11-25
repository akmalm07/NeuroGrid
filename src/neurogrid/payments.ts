import { createHmac, timingSafeEqual } from 'node:crypto'
import type { Env } from '../../worker'

export interface CreditPack { credits: number; cents: number }
export interface CreditOrder extends CreditPack {
  version?: 2
  id: string
  owner: string
  created: number
  live: boolean
  origin: string
  sessionId?: string
  intentId?: string
  refunded?: number
  disputes?: Record<string, { active: boolean; amount: number }>
  status: 'pending' | 'paid' | 'released'
}

export function stripeSettings(env: Env, requestOrigin: string) {
  const { NEUROGRID_STRIPE_KEY: key, NEUROGRID_STRIPE_WEBHOOK: secret,
    NEUROGRID_STRIPE_ORIGIN: origin } = env
  if (!key || !/^(?:(?:sk|rk)_(?:test|live)_[A-Za-z0-9]+|rkcs_test_[A-Za-z0-9_]+)$/.test(key)
      || !secret?.startsWith('whsec_') || !origin || origin !== requestOrigin) return
  const live = key.includes('_live_')
  const secureOrigin = /^https:\/\/[a-z0-9.-]+$/.test(origin)
  const localTestOrigin = !live && /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)
  if (secureOrigin || localTestOrigin) return { key, secret, origin, live }
}

export async function startCheckout(settings: NonNullable<ReturnType<typeof stripeSettings>>, order: CreditOrder) {
  // Preserve the exact create parameters across retries for Stripe idempotency.
  const body = new URLSearchParams({
    mode: 'payment',
    'adaptive_pricing[enabled]': 'false',
    client_reference_id: order.id,
    'metadata[neurogrid_order]': order.id,
    'line_items[0][quantity]': '1',
    'line_items[0][price_data][currency]': 'usd',
    'line_items[0][price_data][unit_amount]': String(order.cents),
    'line_items[0][price_data][product_data][name]': `NeuroGrid · ${order.credits} inference credits`,
    success_url: `${order.origin}/chat?checkout=complete`,
    cancel_url: `${order.origin}/chat?checkout=canceled`,
  })
  const response = await fetch('https://api.stripe.com/v1/checkout/sessions', {
    method: 'POST',
    headers: { Authorization: `Bearer ${settings.key}`, 'Idempotency-Key': `neurogrid-${order.id}` },
    body,
    signal: AbortSignal.timeout(10_000),
  })
  if (!response.ok) return
  const session = await response.json<{ url?: string; status?: string }>()
  if (session.status === 'open' && session.url && new URL(session.url).origin === 'https://checkout.stripe.com') {
    return session.url
  }
}

export function verifyStripeSignature(payload: string, header: string, secret: string, now: number) {
  const fields = header.split(',')
  const stamp = fields.find((field) => field.startsWith('t='))?.slice(2)
  if (!stamp || !/^\d{1,12}$/.test(stamp) || Math.abs(now - Number(stamp) * 1_000) > 300_000) return false
  const expected = createHmac('sha256', secret).update(`${stamp}.${payload}`).digest()
  return fields.some((field) => /^v1=[a-f\d]{64}$/.test(field)
    && timingSafeEqual(expected, Buffer.from(field.slice(3), 'hex')))
}

export function stripeOutcome(payload: string, live: boolean) {
  let event
  try {
    event = JSON.parse(payload)
  } catch {
    return
  }
  if (event?.livemode !== live) return
  const session = event.data?.object
  const intentId = session?.payment_intent
  if (typeof event.id === 'string' && /^evt_[A-Za-z0-9]+$/.test(event.id)
      && typeof intentId === 'string' && /^pi_[A-Za-z0-9]+$/.test(intentId) && session.currency === 'usd') {
    if (event.type === 'charge.refunded' && session.object === 'charge'
        && Number.isSafeInteger(session.amount_refunded) && session.amount_refunded > 0) {
      return { adjustment: 'refund' as const, intentId, eventId: event.id as string, live, amount: session.amount_refunded as number }
    }
    if ((event.type === 'charge.dispute.funds_withdrawn' || event.type === 'charge.dispute.funds_reinstated')
        && session.object === 'dispute' && typeof session.id === 'string' && /^dp_[A-Za-z0-9]+$/.test(session.id)
        && Number.isSafeInteger(session.amount) && session.amount > 0) {
      return { adjustment: 'dispute' as const, intentId, eventId: event.id as string,
        live, disputeId: session.id as string, amount: session.amount as number,
        active: event.type === 'charge.dispute.funds_withdrawn' }
    }
  }
  const id = session?.metadata?.neurogrid_order
  if (session?.object !== 'checkout.session' || session.mode !== 'payment' || session.livemode !== live
      || typeof id !== 'string' || !/^[\da-f-]{36}$/.test(id) || session.client_reference_id !== id
      || typeof session.id !== 'string' || !/^cs_(test|live)_[A-Za-z0-9]+$/.test(session.id)
      || session.currency !== 'usd' || !Number.isSafeInteger(session.amount_total)) return
  if ((event.type === 'checkout.session.completed' || event.type === 'checkout.session.async_payment_succeeded')
      && session.status === 'complete' && session.payment_status === 'paid') {
    return { id, live, sessionId: session.id as string, cents: session.amount_total as number, status: 'paid' as const,
      ...(typeof intentId === 'string' && /^pi_[A-Za-z0-9]+$/.test(intentId) ? { intentId } : {}) }
  }
  if ((event.type === 'checkout.session.expired' && session.status === 'expired'
      || event.type === 'checkout.session.async_payment_failed' && session.status === 'complete')
      && session.payment_status === 'unpaid') {
    return { id, live, sessionId: session.id as string, cents: session.amount_total as number, status: 'released' as const }
  }
}
