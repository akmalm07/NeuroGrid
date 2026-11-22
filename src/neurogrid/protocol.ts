export const MODEL = 'SmolLM2-135M-Instruct'
export const REVISION = '12fd25f77366fa6b3b4b768ec3050bf629380bac'
export const CONTEXT = 128
export const MAX_TOKENS = 24
export const WIDTH = 576
export const VOCAB = 49152
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
  { title: 'Output', layers: '21–30', detail: 'Final layers, normalization and greedy token selection' },
] as const

export type Transfer = [account: string, delta: number]
export type BlockKind = 'genesis' | 'grant' | 'reserve' | 'settle' | 'refund'
  | 'checkout' | 'purchase' | 'checkout_release'
export interface BlockData {
  version: 1
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
}

export interface NodeSummary {
  id: string
  shard: number
  status: 'online' | 'working' | 'paused' | 'offline'
}

export interface NetworkSnapshot {
  nodes: NodeSummary[]
  verifier: boolean
  settled: number
  height: number
  active: { stage: number; status: JobSummary['status']; count: number } | null
}

export const fee = (tokens: number) => 1 + tokens * 3
