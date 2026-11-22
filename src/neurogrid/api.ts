import { getAuthToken } from 'deepspace'
import type { Block, JobSummary, NetworkSnapshot } from './protocol'

export interface Account {
  credits: number
  pendingCheckout?: { credits: number; cents: number }
  jobs: JobSummary[]
  nodes: { id: string; shard: number; paused: boolean; expires: number }[]
}

export interface Enrollment { id: string; shard: number; secret: string; expires: number }

export const messages: Record<string, string> = {
  unauthorized: 'Sign in again to continue.',
  network_busy: 'The network is finishing another request. Try again in a moment.',
  network_unavailable: 'All three partitions and the verifier must be online.',
  invalid_request: 'Enter a prompt of 500 characters or fewer.',
  insufficient_credits: 'Your balance is too low. Choose a shorter token limit.',
  context_limit: 'This prompt is too long for the 128-token context. Shorten it and try again.',
  inference_timeout: 'The request timed out. Your reserved credits were returned.',
  partition_unavailable: 'A partition lost its worker. Your reserved credits were returned.',
  node_limit: 'You have reached the node limit. Revoke an unused key to make room.',
  rate_limit: 'Too many requests. Try again in a minute.',
  credits_sold_out: 'The treasury has fewer credits available. Try a smaller amount.',
  invalid_credit_amount: 'Enter a whole number from 100 to 10,000 credits.',
  checkout_amount_locked: 'You already have a checkout open. Continue that checkout or wait for it to expire.',
  checkout_pending: 'Could not open checkout. If you already paid, wait for your balance to update before trying again.',
}

export function explain(error: unknown) {
  const code = error instanceof Error ? error.message : ''
  return messages[code] ?? 'Could not reach NeuroGrid. Try again shortly.'
}

async function request<T>(path: string, data?: object): Promise<T> {
  const headers = new Headers()
  if (data) headers.set('Content-Type', 'application/json')
  if (path.startsWith('user/')) {
    const token = await getAuthToken()
    if (!token) throw new Error('unauthorized')
    headers.set('Authorization', 'Bearer ' + token)
  }
  const response = await fetch('/api/grid/' + path, {
    method: data ? 'POST' : 'GET', headers, body: data ? JSON.stringify(data) : undefined,
  })
  const result = await response.json()
  if (!response.ok) throw new Error(result && typeof result === 'object' && 'error' in result
    && typeof result.error === 'string' ? result.error : 'request_failed')
  return result as T
}

export const readNetwork = () => request<NetworkSnapshot>('public/network')
export const readAccount = () => request<Account>('user/account')
export const readCheckoutMode = () => request<{ mode: 'disabled' | 'test' | 'live' }>('public/checkout')
export const checkoutCredits = (credits: number) => request<{ url: string }>('user/checkout', { credits })
export const enroll = (shard: string) => request<Enrollment>('user/enroll', shard === 'auto' ? {} : { shard: Number(shard) })
export const revoke = (id: string) => request<{ ok: boolean }>('user/revoke', { id })
export const submit = (prompt: string, maxTokens: number, key: string) =>
  request<JobSummary>('user/request', { prompt, maxTokens, key })

export async function readLedger(): Promise<Block[]> {
  const blocks: Block[] = []
  let target = Infinity
  while (blocks.length <= target) {
    const page = await request<{ blocks: Block[]; height: number }>('public/ledger?after=' + (blocks.length - 1))
    target = Math.min(target, page.height)
    if (!page.blocks.length) throw new Error('ledger_incomplete')
    blocks.push(...page.blocks)
  }
  return blocks.slice(0, target + 1)
}
