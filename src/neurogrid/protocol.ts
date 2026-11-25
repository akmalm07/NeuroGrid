import granite from '../../compute/neurogrid_node/granite.json'

export const MODEL = 'Granite 4.2 8B'
export const { revision: REVISION, runtime: RUNTIME, width: WIDTH, vocab: VOCAB, context: CONTEXT,
  window: WINDOW, shards: SHARD_COUNT } = granite
export const LAST_SHARD = SHARD_COUNT - 1
export const MAX_PROMPT_BYTES = 16_384
export const QUICK_INPUT = 256
export const QUICK_OUTPUT = 64
export const MAX_TOKENS = 512
export const RATE = 'granite-beta-1'
export const UNIT = 1_000
export const SUPPLY = 100_000
export const GRANT = 100
export const MIN_PURCHASE_CREDITS = 100
export const MAX_PURCHASE_CREDITS = 10_000

// 100 credits per dollar. Keep all prices in integer USD cents.
export function purchaseCents(credits: unknown): number | undefined {
  if (typeof credits === 'number' && Number.isSafeInteger(credits)
      && credits >= MIN_PURCHASE_CREDITS && credits <= MAX_PURCHASE_CREDITS) return credits
}
export const POLL_MS = 1_000
export const SHARDS = [
  { title: 'Input', layers: '01–10', detail: 'Token embeddings and the first ten transformer layers' },
  { title: 'Transform', layers: '11–20', detail: 'Ten middle transformer layers' },
  { title: 'Transform II', layers: '21–30', detail: 'Ten middle transformer layers' },
  { title: 'Output', layers: '31–40', detail: 'Final layers, normalization and greedy token selection' },
] as const

export type Transfer = [account: string, delta: number]
export type BlockKind = 'genesis' | 'grant' | 'reserve' | 'settle' | 'refund'
  | 'checkout' | 'purchase' | 'checkout_release' | 'units' | 'purchase_adjustment'
export interface BlockData {
  version: 1 | 2
  height: number
  previousHash: string
  createdAt: number
  kind: BlockKind
  reference: string
  transfers: Transfer[]
  proof: string
}
export interface Block extends BlockData { hash: string }

export function blockBytes(block: BlockData): string {
  return JSON.stringify([
    block.version, block.height, block.previousHash, block.createdAt, block.kind,
    block.reference, block.transfers, block.proof,
  ])
}

export interface JobSummary {
  id: string
  prompt: string
  status: 'prepare' | 'waiting' | 'working' | 'verifying' | 'settled' | 'refunded'
  stage: number
  output: string
  reason: string
  count: number
  maxTokens: number
  charge: number
  createdAt: number
  returned?: number
  trimmed?: number
  model?: string
  route?: string[]
}

export interface NodeSummary {
  id: string
  shard: number
  status: 'online' | 'working' | 'paused' | 'offline'
  pingMs?: number
}

export interface NetworkSnapshot {
  nodes: NodeSummary[]
  verifier: boolean
  settled: number
  height: number
  active: { stage: number; status: JobSummary['status']; count: number } | null
}

export type InferenceProfile = 'quick' | 'standard'

export interface QuoteSummary {
  id: string
  status: 'preparing' | 'ready' | 'rejected'
  expires: number
  reason?: string
  credits?: number
  profile?: InferenceProfile
  maxTokens?: number
  trimmed?: number
}

export const fee = (input: number, generated: number) =>
  2 + Math.ceil(Math.max(0, input - QUICK_INPUT) / QUICK_INPUT)
    + Math.ceil(Math.max(0, generated - QUICK_OUTPUT) / 32)
