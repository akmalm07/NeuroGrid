import { createHash, timingSafeEqual } from 'node:crypto'
import { DurableObject } from 'cloudflare:workers'
import type { Env } from '../../worker'
import { EMPTY_HASH, TREASURY } from './ledger'
import { issueProbe, latencyMs, recordExecution, recordProbe, routeWorker, selectRoute, type WorkerLatency } from './latency'
import type { CreditOrder } from './payments'
import {
  CONTEXT, GRANT, LAST_SHARD, MAX_PROMPT_BYTES, MAX_TOKENS, MODEL, QUICK_INPUT, QUICK_OUTPUT, RATE, REVISION,
  RUNTIME, SHARD_COUNT, SUPPLY, UNIT, VOCAB, WIDTH, WINDOW, blockBytes, fee, purchaseCents,
  type Block, type BlockData, type BlockKind, type InferenceProfile, type JobSummary, type NetworkSnapshot,
  type QuoteSummary, type Transfer,
} from './protocol'

const SEEN_MS = 90_000
const WORK_MS = 55_000
const JOB_MS = 25 * 60_000
const QUOTE_MS = 120_000
const MAX_ACTIVE = 4
const KEY_MS = 30 * 24 * 60 * 60 * 1_000
const hash = (value: string) => createHash('sha256').update(value).digest('hex')
const isInteger = (value: unknown, min: number, max: number): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max
const validToken = (value: unknown): value is number => isInteger(value, 0, VOCAB - 1)

interface Node extends WorkerLatency {
  id: string
  owner: string
  shard: number
  seen: number
  assigned: number
  paused: boolean
  expires: number
  runtime?: string
}
interface Candidate { token?: number }
interface Job extends JobSummary {
  owner: string
  key: string
  expires: number
  reserved: number
  stepBy: number
  task: string
  tokens: number[]
  generated: number[]
  candidate?: Candidate
  lease?: { id: string; node: string; owner: string; expires: number; started?: number; finished?: number }
  attempted: string[]
  rewards: Record<string, number>
  proof: string
  route?: string[]
  position?: number
  epoch?: number
  retries?: number
  ingress?: string
  rate?: string
  profile?: InferenceProfile
  stepOwners?: string[]
  paidThrough?: number
}

interface Message { role: 'user' | 'assistant'; content: string }
interface Quote extends QuoteSummary {
  owner: string
  key: string
  prompt: string
  messages: Message[]
  tokens?: number[]
  standard: boolean
  rate: string
}

class Rejection extends Error {
  constructor(public code: string, public status = 409) { super(code) }
}

export class NeuroGridCoordinator extends DurableObject<Env> {
  private sql = this.ctx.storage.sql

  constructor(state: DurableObjectState, env: Env) {
    super(state, env)
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS balances (id TEXT PRIMARY KEY, credits INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS blocks (height INTEGER PRIMARY KEY, kind TEXT NOT NULL, hash TEXT NOT NULL, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL, active INTEGER NOT NULL, created INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(owner, request_key));
      CREATE INDEX IF NOT EXISTS jobs_active ON jobs(active);
      CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created);
      CREATE TABLE IF NOT EXISTS activations (
        job TEXT NOT NULL, slot TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job, slot)
      );
      CREATE TABLE IF NOT EXISTS nodes (key TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS nodes_owner ON nodes(owner);
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS credit_orders (id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS credit_order_pending ON credit_orders(owner) WHERE status = 'pending';
      CREATE UNIQUE INDEX IF NOT EXISTS credit_order_session ON credit_orders(json_extract(data, '$.sessionId'));
      CREATE UNIQUE INDEX IF NOT EXISTS credit_order_intent ON credit_orders(json_extract(data, '$.intentId'));
      CREATE TABLE IF NOT EXISTS payment_adjustments (
        event_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, created INTEGER NOT NULL, data TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS payment_adjustments_intent ON payment_adjustments(intent_id);
      CREATE TABLE IF NOT EXISTS quotes (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL, expires INTEGER NOT NULL,
        status TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(owner, request_key)
      );
    `)
    this.ctx.storage.transactionSync(() => {
      if (!this.one('SELECT height FROM blocks WHERE height = 0')) {
        this.append('genesis', 'neurogrid-v1', [[TREASURY, SUPPLY]], Date.now())
      }
      if (!this.setting('ledger-units')) {
        const now = Date.now()
        for (const job of this.activeJobs()) this.refund(job, 'model_upgraded', now)
        this.sql.exec('UPDATE balances SET credits = credits * ?', UNIT)
        this.set('ledger-units', UNIT)
        this.append('units', 'granite-credit-subunits-v2', [], now)
      }
    })
  }

  private one(query: string, ...values: SqlStorageValue[]) {
    return this.sql.exec(query, ...values).toArray()[0]
  }

  private read<T>(query: string, ...values: SqlStorageValue[]): T | undefined {
    const row = this.one(query, ...values)
    return row ? JSON.parse(String(row.data)) as T : undefined
  }

  private readAll<T>(query: string, ...values: SqlStorageValue[]): T[] {
    return this.sql.exec(query, ...values).toArray().map((row) => JSON.parse(String(row.data)) as T)
  }

  private balance(id: string) {
    return Number(this.one('SELECT credits FROM balances WHERE id = ?', id)?.credits ?? 0)
  }

  private append(kind: BlockKind, reference: string, transfers: Transfer[], createdAt: number, proof = '') {
    const head = this.one('SELECT height, hash FROM blocks ORDER BY height DESC LIMIT 1')
    const data: BlockData = {
      version: this.setting('ledger-units') ? 2 : 1, height: head ? Number(head.height) + 1 : 0,
      previousHash: head ? String(head.hash) : EMPTY_HASH, createdAt, kind, reference, transfers, proof,
    }
    for (const [id, delta] of transfers) {
      const previous = this.balance(id)
      const next = previous + delta
      const minimum = kind === 'purchase_adjustment' && id.startsWith('acct:') ? Number.MIN_SAFE_INTEGER : Math.min(previous, 0)
      if (!isInteger(next, minimum, Number.MAX_SAFE_INTEGER)) throw new Error('Invalid balance transition')
      this.sql.exec('INSERT INTO balances VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET credits = excluded.credits', id, next)
    }
    const digest = hash(blockBytes(data))
    this.sql.exec('INSERT INTO blocks VALUES (?, ?, ?, ?)', data.height, kind, digest, JSON.stringify({ ...data, hash: digest }))
  }

  private grant(account: string, now: number) {
    if (this.one('SELECT id FROM balances WHERE id = ?', account)) return
    if (this.balance(TREASURY) < GRANT * UNIT) return
    this.append('grant', account, [[TREASURY, -GRANT * UNIT], [account, GRANT * UNIT]], now)
  }

  private checkout(account: string, credits: unknown, now: number, live: boolean, origin: string) {
    const existing = this.read<CreditOrder>("SELECT data FROM credit_orders WHERE owner = ? AND status = 'pending'", account)
    if (existing) {
      // Stripe may prune idempotency keys after 24 hours. Never open a second
      // session for an order beyond that window.
      if (existing.live !== live || now - existing.created >= 23 * 3_600_000) {
        throw new Rejection('checkout_pending')
      }
      if (credits !== existing.credits) throw new Rejection('checkout_amount_locked')
      return existing
    }
    const cents = purchaseCents(credits)
    if (cents === undefined) throw new Rejection('invalid_credit_amount', 400)
    this.grant(account, now)
    const order: CreditOrder = { version: 2, id: crypto.randomUUID(), owner: account, credits: cents,
      cents, created: now, live, origin, status: 'pending' }
    this.append('checkout', order.id, [], now)
    this.sql.exec('INSERT INTO credit_orders VALUES (?, ?, ?, ?)', order.id, account, order.status, JSON.stringify(order))
    return order
  }

  private applyCheckout(data: Record<string, unknown>, now: number) {
    if (data.adjustment) return this.receiveAdjustment(data, now)
    if (typeof data.id !== 'string' || typeof data.sessionId !== 'string'
        || (data.status !== 'paid' && data.status !== 'released')) {
      throw new Rejection('invalid_payment', 400)
    }
    const order = this.read<CreditOrder>('SELECT data FROM credit_orders WHERE id = ?', data.id)
    if (!order) throw new Rejection('order_not_found', 404)
    if (order.cents !== data.cents || order.live !== data.live) throw new Rejection('payment_mismatch', 400)
    const intentId = typeof data.intentId === 'string' && /^pi_[A-Za-z0-9]+$/.test(data.intentId) ? data.intentId : undefined
    if (order.intentId && intentId && order.intentId !== intentId) throw new Rejection('payment_mismatch', 400)
    if (order.status === data.status && order.sessionId === data.sessionId) {
      if (order.status === 'paid' && !order.intentId && intentId) {
        order.intentId = intentId
        this.updateOrder(order)
        this.applyQueuedAdjustments(order, now)
      }
      return { ok: true }
    }
    if (order.status !== 'pending') throw new Rejection('payment_state_conflict')
    order.status = data.status
    order.sessionId = data.sessionId
    const paid = order.status === 'paid'
    if (paid && intentId) order.intentId = intentId
    const amount = order.credits * UNIT
    const transfers: Transfer[] = order.version === 2 ? (paid ? [[order.owner, amount]] : []) : [
      [`checkout:${order.id}`, -amount], [paid ? order.owner : TREASURY, amount],
    ]
    this.append(paid ? 'purchase' : 'checkout_release', order.id, transfers, now,
      paid ? hash(order.sessionId) : '')
    this.updateOrder(order)
    if (paid) this.applyQueuedAdjustments(order, now)
    return { ok: true }
  }

  private updateOrder(order: CreditOrder) {
    this.sql.exec('UPDATE credit_orders SET status = ?, data = ? WHERE id = ?', order.status, JSON.stringify(order), order.id)
  }

  private receiveAdjustment(data: Record<string, unknown>, now: number) {
    const { adjustment, eventId, intentId, amount, live } = data
    if ((adjustment !== 'refund' && adjustment !== 'dispute') || typeof eventId !== 'string' || !/^evt_[A-Za-z0-9]+$/.test(eventId)
        || typeof intentId !== 'string' || !/^pi_[A-Za-z0-9]+$/.test(intentId)
        || !isInteger(amount, 1, Number.MAX_SAFE_INTEGER) || typeof live !== 'boolean'
        || (adjustment === 'dispute' && (typeof data.disputeId !== 'string' || !/^dp_[A-Za-z0-9]+$/.test(data.disputeId)
          || typeof data.active !== 'boolean'))) throw new Rejection('invalid_payment', 400)
    const order = this.read<CreditOrder>("SELECT data FROM credit_orders WHERE json_extract(data, '$.intentId') = ?", intentId)
    if (order) {
      this.applyAdjustment(order, data, now)
    } else {
      this.sql.exec('DELETE FROM payment_adjustments WHERE created < ?', now - 30 * 24 * 3600_000)
      const queued = this.one('SELECT COUNT(*) AS n FROM payment_adjustments')
      if (Number(queued?.n) < 1000) this.sql.exec('INSERT OR IGNORE INTO payment_adjustments VALUES (?, ?, ?, ?)',
        eventId, intentId, now, JSON.stringify(data))
    }
    return { ok: true }
  }

  private applyQueuedAdjustments(order: CreditOrder, now: number) {
    if (!order.intentId) return
    const queued = this.readAll<Record<string, unknown>>(
      'SELECT data FROM payment_adjustments WHERE intent_id = ? ORDER BY created, event_id', order.intentId)
    for (const event of queued) this.applyAdjustment(order, event, now)
    this.sql.exec('DELETE FROM payment_adjustments WHERE intent_id = ?', order.intentId)
  }

  private applyAdjustment(order: CreditOrder, data: Record<string, unknown>, now: number) {
    if (order.live !== data.live || order.status !== 'paid' || !isInteger(data.amount, 1, order.cents)) return
    const total = () => Math.min(order.cents, (order.refunded ?? 0) + Object.values(order.disputes ?? {})
      .reduce((sum, dispute) => sum + (dispute.active ? dispute.amount : 0), 0))
    const before = total()
    if (data.adjustment === 'refund') {
      order.refunded = Math.max(order.refunded ?? 0, data.amount)
    } else {
      const disputes = order.disputes ?? (order.disputes = {})
      const id = data.disputeId as string
      // A delayed withdrawal cannot reopen a dispute whose funds were already reinstated.
      if (disputes[id]?.active !== false) disputes[id] = { active: data.active === true, amount: data.amount }
    }
    const difference = (before - total()) * UNIT
    if (difference) {
      const digest = hash(String(data.eventId))
      if (!this.one("SELECT height FROM blocks WHERE kind = 'purchase_adjustment' AND json_extract(data, '$.proof') = ?", digest)) {
        this.append('purchase_adjustment', order.id + ':' + digest, [[order.owner, difference]], now, digest)
      }
    }
    this.updateOrder(order)
  }

  private activeJobs() {
    return this.readAll<Job>('SELECT data FROM jobs WHERE active = 1 ORDER BY created, id')
  }

  private saveJob(job: Job) {
    this.sql.exec(`
      INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET active = excluded.active, data = excluded.data
    `, job.id, job.owner, job.key, Number(!['settled', 'refunded'].includes(job.status)),
    job.createdAt, JSON.stringify(job))
  }

  private setting(key: string) {
    return Number(this.one('SELECT value FROM settings WHERE key = ?', key)?.value ?? 0)
  }

  private set(key: string, value: number) {
    this.sql.exec('INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', key, value)
  }

  private nodes() {
    return this.readAll<Node>('SELECT data FROM nodes ORDER BY id')
  }

  private available(node: Node, now: number) {
    return node.runtime === RUNTIME && !node.paused && node.expires > now && node.seen + SEEN_MS > now
  }

  private verifierOnline(now: number) {
    return this.setting(`reference:${RUNTIME}`) + SEEN_MS > now
  }

  private clearJobInputs(job: Job) {
    job.tokens = []
    job.generated = []
    this.clearActivations(job.id)
    delete job.candidate
    delete job.lease
  }

  private clearActivations(id: string) {
    this.sql.exec('DELETE FROM activations WHERE job = ?', id)
  }

  private activation(id: string, slot: 'input' | 'result'): string {
    const value = this.one('SELECT payload FROM activations WHERE job = ? AND slot = ?', id, slot)?.payload
    if (typeof value !== 'string') throw new Error('Missing Granite activation')
    return value
  }

  private setActivation(id: string, slot: 'input' | 'result', payload: string) {
    this.sql.exec('INSERT INTO activations VALUES (?, ?, ?) ON CONFLICT(job, slot) DO UPDATE SET payload = excluded.payload',
      id, slot, payload)
  }

  private refund(job: Job, reason: string, now: number) {
    this.append('refund', job.id, [[`held:${job.id}`, -job.reserved], [job.owner, job.reserved]], now)
    job.status = 'refunded'
    job.reason = reason
    job.output = ''
    if (job.rate) job.returned = job.reserved / UNIT
    this.clearJobInputs(job)
    this.saveJob(job)
  }

  private settle(job: Job, now: number) {
    if (!job.output.trim()) return this.refund(job, 'empty_reply', now)
    job.charge = fee(job.tokens.length, job.count)
    const paid = job.charge * UNIT
    if (job.rate !== RATE || paid > job.reserved) throw new Error('Invalid locked inference charge')
    job.returned = (job.reserved - paid) / UNIT
    const credits = new Map<string, number>([[job.owner, job.reserved - paid]])
    const workers = Object.entries(job.rewards)
    const work = workers.reduce((total, [, units]) => total + units, 0)
    if (!work) throw new Error('Missing verified work')
    const rewardPool = Math.floor(paid * 7 / 10)
    for (const [owner, units] of workers) {
      const reward = Math.floor(rewardPool * units / work)
      credits.set(owner, (credits.get(owner) ?? 0) + reward)
    }
    const transfers: Transfer[] = [[`held:${job.id}`, -job.reserved], ...credits]
    this.append('settle', job.id, transfers.filter(([, amount]) => amount !== 0), now, job.proof)
    this.set('settled', this.setting('settled') + 1)
    job.status = 'settled'
    this.clearJobInputs(job)
    this.saveJob(job)
  }

  private retry(job: Job, now: number, preserveWorker = false) {
    const failed = job.lease?.node
    const candidates = this.nodes()
    if (failed && !preserveWorker) {
      const node = candidates.find(({ id }) => id === failed)
      if (node) {
        node.failures = Math.min((node.failures ?? 0) + 1, 10)
        this.sql.exec('UPDATE nodes SET data = ? WHERE id = ?', JSON.stringify(node), node.id)
      }
    }
    job.retries = (job.retries ?? 0) + 1
    if (job.retries > 8) return this.refund(job, 'partition_unavailable', now)
    if (job.route) {
      if (!preserveWorker) {
        const other = this.pickWorker(job, candidates, now, failed)
        job.route[job.stage] = other?.id ?? ''
        job.attempted = failed ? [failed] : []
      }
      this.rewind(job)
    }
    delete job.lease
    delete job.candidate
    job.status = 'waiting'
    job.stepBy = now + WORK_MS
    this.saveJob(job)
  }

  private tick(now: number) {
    for (const job of this.activeJobs()) {
      if (now >= job.expires || job.status === 'waiting' && now >= job.stepBy) {
        this.refund(job, job.status === 'waiting' ? 'partition_unavailable' : 'inference_timeout', now)
      } else if (job.status === 'working' && job.lease && now >= job.lease.expires
        || job.status === 'verifying' && now >= job.stepBy) {
        this.retry(job, now)
      }
    }
  }

  private summary(job: Job): JobSummary {
    const { id, prompt, status, stage, output, reason, count, maxTokens, charge, createdAt } = job
    return { id, prompt, status, stage, output: status === 'settled' ? output : '', reason, count, maxTokens, charge,
      createdAt, model: job.model, route: job.route, trimmed: job.trimmed, returned: job.returned }
  }

  private network(now: number): NetworkSnapshot {
    const jobs = this.activeJobs()
    const active = jobs[0]
    return {
      nodes: this.nodes().filter((node) => node.expires > now).map((node) => ({
        id: node.id, shard: node.shard, pingMs: latencyMs(node, now),
        status: node.paused ? 'paused' : !this.available(node, now) ? 'offline'
          : jobs.some((job) => job.lease?.node === node.id) ? 'working' : 'online',
      })),
      verifier: this.verifierOnline(now),
      settled: this.setting('settled'),
      height: Number(this.one('SELECT MAX(height) AS height FROM blocks')?.height),
      active: active ? { stage: active.stage, status: active.status, count: active.count } : null,
    }
  }

  private quoteSummary(quote: Quote): QuoteSummary {
    const { id, status, expires, reason, credits, profile, maxTokens, trimmed } = quote
    return { id, status, expires, reason, credits, profile, maxTokens, trimmed }
  }

  private saveQuote(quote: Quote) {
    this.sql.exec(`INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data`,
    quote.id, quote.owner, quote.key, quote.expires, quote.status, JSON.stringify(quote))
  }

  private draft(account: string, data: Record<string, unknown>, now: number) {
    const { prompt, key } = data
    if (typeof prompt !== 'string' || !prompt.trim() || Buffer.byteLength(prompt) > MAX_PROMPT_BYTES
        || !validKey(key) || Object.hasOwn(data, 'maxTokens')) {
      throw new Rejection('invalid_request', 400)
    }
    this.sql.exec('DELETE FROM quotes WHERE expires <= ?', now)
    const existing = this.read<Quote>('SELECT data FROM quotes WHERE owner = ? AND request_key = ?', account, key)
    if (existing) {
      if (existing.prompt !== prompt.trim()) throw new Rejection('quote_mismatch')
      return this.quoteSummary(existing)
    }
    if (!this.verifierOnline(now)) throw new Rejection('network_unavailable')
    const limit = this.one("SELECT COUNT(*) AS n FROM quotes WHERE status = 'preparing'")
    const ownerLimit = this.one('SELECT COUNT(*) AS n FROM quotes WHERE owner = ?', account)
    if (Number(limit?.n) >= 16 || Number(ownerLimit?.n) >= 10) throw new Rejection('rate_limit', 429)
    const history = this.readAll<Job>(`SELECT data FROM jobs WHERE owner = ?
      AND active = 0 AND json_extract(data, '$.status') = 'settled'
      ORDER BY created DESC, id DESC LIMIT 10`, account)
    const pairs: Message[][] = []
    let historyBytes = 0
    for (const job of history) {
      historyBytes += Buffer.byteLength(job.prompt) + Buffer.byteLength(job.output)
      if (historyBytes > 32_768) break
      if (job.output) pairs.unshift([{ role: 'user', content: job.prompt }, { role: 'assistant', content: job.output }])
    }
    this.grant(account, now)
    const quote: Quote = {
      id: crypto.randomUUID(), expires: now + QUOTE_MS, status: 'preparing', rate: RATE,
      owner: account, key, prompt: prompt.trim(), messages: [...pairs.flat(), { role: 'user', content: prompt.trim() }],
      standard: /\b(summarize|summarise|essay|in detail|step.by.step|write a)\b/i.test(prompt),
    }
    this.saveQuote(quote)
    return this.quoteSummary(quote)
  }

  private queuedWorkers(nodes: Node[], now: number) {
    const jobs = this.activeJobs()
    return nodes.filter((node) => this.available(node, now)).map((node) => ({
      ...node, queued: jobs.filter((job) => job.route?.[node.shard] === node.id).length,
    }))
  }

  private pickWorker(job: Job, nodes: Node[], now: number, exclude?: string) {
    return routeWorker(this.queuedWorkers(nodes, now).filter((node) =>
      node.shard === job.stage && node.id !== exclude && !job.attempted.includes(node.id)), now, job.ingress)
  }

  private user(action: string, account: string, data: Record<string, unknown>, now: number, ingress?: string) {
    switch (action) {
      case 'account': {
        this.grant(account, now)
        const order = this.read<CreditOrder>(
          "SELECT data FROM credit_orders WHERE owner = ? AND status = 'pending'", account)
        return {
          credits: this.balance(account) / UNIT,
          pendingCheckout: order && { credits: order.credits, cents: order.cents },
          jobs: this.readAll<Job>('SELECT data FROM jobs WHERE owner = ? ORDER BY created DESC LIMIT 20', account)
            .map((job) => this.summary(job)),
          nodes: this.readAll<Node>('SELECT data FROM nodes WHERE owner = ? ORDER BY id', account)
            .filter((node) => node.expires > now)
            .map(({ id, shard, paused, expires }) => ({ id, shard, paused, expires })),
        }
      }
      case 'enroll': {
        this.sql.exec('DELETE FROM nodes WHERE json_extract(data, \'$.expires\') <= ?', now)
        const nodes = this.nodes()
        if (nodes.length >= 100 || nodes.filter((node) => node.owner === account).length >= 6) {
          throw new Rejection('node_limit')
        }
        const counts = Array.from({ length: SHARD_COUNT }, (_, shard) => nodes.filter((node) => node.shard === shard).length)
        if (data.shard !== undefined && !isInteger(data.shard, 0, LAST_SHARD)) throw new Rejection('bad_shard', 400)
        const shard = typeof data.shard === 'number' ? data.shard : counts.indexOf(Math.min(...counts))
        const id = crypto.randomUUID()
        const secret = `ng_${crypto.randomUUID()}${crypto.randomUUID()}`
        const node: Node = { id, owner: account, shard, seen: 0, assigned: 0, paused: false, expires: now + KEY_MS }
        this.sql.exec('INSERT INTO nodes VALUES (?, ?, ?, ?)', hash(secret), id, account, JSON.stringify(node))
        return { id, shard, secret, expires: node.expires }
      }
      case 'revoke': {
        if (typeof data.id !== 'string' || !this.one('SELECT id FROM nodes WHERE id = ? AND owner = ?', data.id, account)) {
          throw new Rejection('node_not_found', 404)
        }
        this.sql.exec('DELETE FROM nodes WHERE id = ? AND owner = ?', data.id, account)
        this.release(data.id, now)
        return { ok: true }
      }
      case 'quote': return this.draft(account, data, now)
      case 'request': {
        const { quoteId, key } = data
        if (!validKey(quoteId) || !validKey(key) || Object.hasOwn(data, 'maxTokens')) {
          throw new Rejection('invalid_request', 400)
        }
        const duplicate = this.read<Job>('SELECT data FROM jobs WHERE owner = ? AND request_key = ?', account, key)
        if (duplicate) return this.summary(duplicate)
        const quote = this.read<Quote>('SELECT data FROM quotes WHERE id = ? AND owner = ? AND request_key = ?',
          quoteId, account, key)
        if (!quote || quote.expires <= now || quote.status !== 'ready' || quote.rate !== RATE
            || !quote.tokens || !quote.credits || !quote.maxTokens) throw new Rejection('quote_expired')
        if (this.activeJobs().length >= MAX_ACTIVE) throw new Rejection('network_busy')
        const route = selectRoute(this.queuedWorkers(this.nodes(), now), SHARD_COUNT, now, ingress)
        if (!this.verifierOnline(now) || !route) throw new Rejection('network_unavailable')
        const requests = this.one('SELECT COUNT(*) AS n FROM jobs WHERE owner = ? AND created > ?', account, now - 60_000)
        if (Number(requests?.n) >= 6) throw new Rejection('rate_limit', 429)
        this.grant(account, now)
        const reserved = quote.credits * UNIT
        if (this.balance(account) < reserved) throw new Rejection('insufficient_credits')
        const job: Job = {
          id: crypto.randomUUID(), owner: account, key, prompt: quote.prompt, maxTokens: quote.maxTokens, reserved,
          status: 'waiting', expires: now + JOB_MS, stepBy: now + WORK_MS, task: crypto.randomUUID(),
          output: '', reason: '', stage: 0, count: 0, charge: 0, createdAt: now,
          tokens: quote.tokens, generated: [], attempted: [], rewards: {}, proof: EMPTY_HASH,
          route, position: 0, epoch: 0, ingress, rate: quote.rate, model: MODEL,
          profile: quote.profile, trimmed: quote.trimmed, stepOwners: [],
        }
        this.append('reserve', job.id, [[account, -reserved], [`held:${job.id}`, reserved]], now)
        this.saveJob(job)
        this.sql.exec('DELETE FROM quotes WHERE id = ?', quote.id)
        return this.summary(job)
      }
      default: throw new Rejection('not_found', 404)
    }
  }

  private release(nodeId: string, now: number) {
    for (const job of this.activeJobs()) {
      if (job.lease?.node === nodeId && job.status === 'working') this.retry(job, now)
    }
  }

  private window(job: Job) {
    const tokens = job.tokens.concat(job.generated)
    const position = job.position ?? 0
    const length = Math.min(WINDOW, tokens.length - position)
    return {
      job: job.id, epoch: job.epoch ?? 0, position, length, emit: position + length === tokens.length,
    }
  }

  private input(job: Job, { position, length }: ReturnType<NeuroGridCoordinator['window']>) {
    return job.stage === 0 ? { tokens: job.tokens.concat(job.generated).slice(position, position + length) }
      : { tensor: this.activation(job.id, 'input') }
  }

  private worker(action: string, secret: string, data: Record<string, unknown>, now: number, colo?: string) {
    const key = hash(secret)
    const node = this.read<Node>('SELECT data FROM nodes WHERE key = ?', key)
    if (!node || node.expires <= now) throw new Rejection('invalid_node_key', 401)
    if (action === 'profile') return { id: node.id, shard: node.shard, revision: REVISION, runtime: RUNTIME }
    if (data.revision !== REVISION || data.runtime !== RUNTIME) throw new Rejection('model_mismatch', 400)
    const write = () => this.sql.exec('UPDATE nodes SET data = ? WHERE key = ?', JSON.stringify(node), key)
    if (action === 'pause') {
      node.paused = true
      delete node.probe
      delete node.latency
      write()
      this.release(node.id, now)
      return { ok: true }
    }
    if (action === 'ping') {
      const pingMs = this.available(node, now) ? recordProbe(node, data.probe, now) : undefined
      if (pingMs === undefined) throw new Rejection('stale_probe')
      write()
      return { pingMs }
    }
    const jobs = this.activeJobs()
    if (action === 'claim') {
      if (!this.available(node, now)) {
        delete node.probe
        delete node.latency
      }
      node.seen = now
      node.runtime = RUNTIME
      node.colo = colo
      node.paused = false
      write()
      const nodes = this.nodes()
      let work = null
      if (!jobs.some((job) => job.lease?.node === node.id && job.status === 'working')) {
        for (const job of jobs) {
          if (!job.route || job.status !== 'waiting') continue
          const pinned = nodes.find(({ id }) => id === job.route?.[job.stage])
          if (!pinned || !this.available(pinned, now)) {
            const alternate = this.pickWorker(job, nodes, now)
            if (!alternate) continue
            job.route[job.stage] = alternate.id
            if ((job.position ?? 0) > 0) this.rewind(job)
            this.saveJob(job)
          }
          if (job.route[job.stage] !== node.id || job.stage !== node.shard) continue
          node.assigned = now
          job.lease = { id: crypto.randomUUID(), node: node.id, owner: node.owner, expires: now + WORK_MS, started: now }
          job.status = 'working'
          this.saveJob(job)
          const window = this.window(job)
          work = { lease: job.lease.id, ...window, input: this.input(job, window) }
          break
        }
      }
      const probe = !work && data.probe === true ? issueProbe(node, now) : undefined
      write()
      return { work, active: jobs.length > 0, probe }
    }
    if (action !== 'result') throw new Rejection('not_found', 404)
    const job = jobs.find((item) => item.status === 'working' && item.lease?.id === data.lease && item.lease?.node === node.id)
    if (!job?.lease) throw new Rejection('stale_lease')
    const { length, emit } = this.window(job)
    if (!isTensor(data.tensor, length) || (job.stage === LAST_SHARD && emit && !validToken(data.token))) {
      this.retry(job, now, data.error === 'cache_miss')
      return { accepted: false, reason: data.error === 'cache_miss' ? 'cache_miss' : 'malformed_result' }
    }
    node.seen = now
    job.lease.finished = now
    write()
    this.setActivation(job.id, 'result', data.tensor)
    job.candidate = job.stage === LAST_SHARD && emit ? { token: data.token as number } : {}
    job.status = 'verifying'
    job.stepBy = now + WORK_MS
    job.task = crypto.randomUUID()
    this.saveJob(job)
    return { accepted: true }
  }

  private rewind(job: Job) {
    job.epoch = (job.epoch ?? 0) + 1
    job.position = 0
    job.stage = 0
    this.clearActivations(job.id)
    job.stepOwners = []
  }

  private prepareQuote(quote: Quote, data: Record<string, unknown>) {
    quote.messages = []
    if (data.error === 'context_limit') {
      quote.status = 'rejected'
      quote.reason = 'context_limit'
    } else {
      const { tokens, profile, maxTokens, trimmed } = data
      if ((profile !== 'quick' && profile !== 'standard') || !Array.isArray(tokens) || !tokens.length
          || !isInteger(maxTokens, 1, profile === 'quick' ? QUICK_OUTPUT : MAX_TOKENS)
          || (profile === 'quick' && tokens.length > QUICK_INPUT) || tokens.length + maxTokens > CONTEXT
          || !tokens.every(validToken) || !isInteger(trimmed, 0, 10)) throw new Rejection('invalid_reference', 400)
      let maximum = maxTokens
      const balance = this.balance(quote.owner)
      while (maximum && fee(tokens.length, maximum) * UNIT > balance) maximum -= 1
      if (!maximum) {
        quote.status = 'rejected'
        quote.reason = 'insufficient_credits'
      } else {
        quote.status = 'ready'
        quote.profile = profile
        quote.tokens = tokens
        quote.maxTokens = maximum
        quote.credits = fee(tokens.length, maximum)
        quote.trimmed = trimmed
      }
    }
    this.saveQuote(quote)
    return { ok: true }
  }

  private verifier(action: string, data: Record<string, unknown>, now: number) {
    if (data.revision !== REVISION || data.runtime !== RUNTIME) throw new Rejection('model_mismatch', 400)
    this.set(`reference:${RUNTIME}`, now)
    const jobs = this.activeJobs()
    if (action === 'claim') {
      const job = jobs.find((item) => item.status === 'verifying')
      const quote = this.read<Quote>("SELECT data FROM quotes WHERE status = 'preparing' AND expires > ? ORDER BY expires LIMIT 1", now)
      if (quote && (!job || (quote.expires - QUOTE_MS + 1_500 <= now && job.stepBy - now > 10_000))) {
        return { work: { id: quote.id, kind: 'prepare', messages: quote.messages, standard: quote.standard } }
      }
      if (job) {
        const window = this.window(job)
        return { work: { id: job.task, kind: 'verify', stage: job.stage, ...window, input: this.input(job, window),
          candidate: { ...job.candidate, tensor: this.activation(job.id, 'result') }, generated: job.generated } }
      }
      return { work: null, active: jobs.length > 0 }
    }
    if (action !== 'result') throw new Rejection('not_found', 404)
    if (!validKey(data.task)) throw new Rejection('stale_verification')
    const quote = this.read<Quote>("SELECT data FROM quotes WHERE id = ? AND status = 'preparing' AND expires > ?", data.task, now)
    if (quote) return this.prepareQuote(quote, data)
    const job = jobs.find((item) => item.status === 'verifying' && item.task === data.task)
    if (!job) throw new Rejection('stale_verification')
    if (data.valid === false) {
      this.retry(job, now, data.cacheMiss === true)
      return { ok: true }
    }
    const window = this.window(job)
    const { length, emit } = window
    if (data.valid !== true || !job.lease || !job.candidate
        || (job.stage < LAST_SHARD && !isTensor(data.tensor, length))
        || (job.stage === LAST_SHARD && emit && (!validToken(data.token) || data.token !== job.candidate.token
          || typeof data.text !== 'string' || Buffer.byteLength(data.text) > 16_384 || typeof data.eos !== 'boolean'))) {
      throw new Rejection('invalid_reference', 400)
    }
    const { owner, node, id } = job.lease
    if (length === 1 && job.lease.started !== undefined && job.lease.finished !== undefined) {
      const worker = this.read<Node>('SELECT data FROM nodes WHERE id = ?', node)
      if (worker) {
        recordExecution(worker, job.lease.finished - job.lease.started, now)
        this.sql.exec('UPDATE nodes SET data = ? WHERE id = ?', JSON.stringify(worker), node)
      }
    }
    const stepOwners = job.stepOwners ?? (job.stepOwners = [])
    stepOwners.push(owner)
    job.proof = hash(JSON.stringify([job.proof, REVISION, job.epoch, window.position, job.stage, node, id,
      hash(JSON.stringify(this.input(job, window))), hash(this.activation(job.id, 'result')), job.candidate.token]))
    delete job.lease
    delete job.candidate
    if (job.stage === LAST_SHARD) {
      const newPosition = window.position + length
      const metered = newPosition - Math.max(window.position, job.paidThrough ?? 0)
      if (metered > 0) {
        for (const worker of stepOwners) job.rewards[worker] = (job.rewards[worker] ?? 0) + metered
        job.paidThrough = newPosition
      }
      job.position = newPosition
      job.stepOwners = []
      job.stage = 0
      this.clearActivations(job.id)
      if (emit) {
        job.generated.push(data.token as number)
        job.output = data.text as string
        job.count += 1
        job.attempted = []
        if (data.eos || job.count === job.maxTokens) {
          if (!data.eos) job.reason = 'output_limit'
          this.settle(job, now)
          return { ok: true }
        }
      }
    } else {
      job.stage += 1
      this.setActivation(job.id, 'input', data.tensor as string)
      this.sql.exec("DELETE FROM activations WHERE job = ? AND slot = 'result'", job.id)
    }
    job.status = 'waiting'
    job.stepBy = now + WORK_MS
    this.saveJob(job)
    return { ok: true }
  }

  private async refresh(now: number) {
    const deadlines = this.activeJobs().map((job) =>
      Math.min(job.expires, job.status === 'working' ? (job.lease?.expires ?? now) : job.stepBy))
    const deadline = Math.min(...deadlines)
    const heartbeat = this.nodes().flatMap((node) => this.available(node, now)
      ? [Math.min(node.seen + SEEN_MS, node.expires)] : [])
    if (this.verifierOnline(now)) heartbeat.push(this.setting(`reference:${RUNTIME}`) + SEEN_MS)
    const nextAlarm = Math.min(deadline, ...heartbeat)
    const current = await this.ctx.storage.getAlarm()
    if (Number.isFinite(nextAlarm) && (current === null || nextAlarm < current || nextAlarm - current > 2_000)) {
      await this.ctx.storage.setAlarm(Math.max(nextAlarm, Date.now() + 25))
    }
    const revision = hash(JSON.stringify(this.network(now)))
    if (await this.ctx.storage.get<string>('projection') === revision) return
    const room = this.env.RECORD_ROOMS.getByName(`app:${this.env.DEEPSPACE_APP_ID}`)
    try {
      const response = await room.fetch('https://internal/api/tools/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-User-Id': this.env.OWNER_USER_ID || 'neurogrid', 'X-App-Action': 'true' },
        body: JSON.stringify({ tool: 'records.create', params: { collection: 'grid_status', recordId: 'network', data: { revision } } }),
      })
      const result = await response.json<{ success: boolean }>()
      if (result.success) await this.ctx.storage.put('projection', revision)
    } catch {
      // Authoritative state remains available through HTTP if a projection write fails.
    }
  }

  async alarm() {
    const now = Date.now()
    this.ctx.storage.transactionSync(() => this.tick(now))
    await this.refresh(now)
  }

  async fetch(request: Request) {
    const [, role, action] = new URL(request.url).pathname.split('/')
    let data: Record<string, unknown> = {}
    try {
      if (request.method === 'POST') data = await request.json()
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Rejection('invalid_body', 400)
    } catch {
      return Response.json({ error: 'invalid_body' }, { status: 400 })
    }
    const now = Date.now()
    const colo = request.headers.get('X-Grid-Colo') ?? undefined
    this.ctx.storage.transactionSync(() => this.tick(now))
    try {
      const result = this.ctx.storage.transactionSync(() => {
        switch (role) {
          case 'public': {
            if (action === 'network') return this.network(now)
            if (action !== 'ledger') throw new Rejection('not_found', 404)
            const raw = new URL(request.url).searchParams.get('after')
            const after = raw === null ? -1 : Number(raw)
            if (!isInteger(after, -1, 10_000_000)) throw new Rejection('invalid_cursor', 400)
            return { blocks: this.readAll<Block>('SELECT data FROM blocks WHERE height > ? ORDER BY height LIMIT 100', after),
              height: this.network(now).height }
          }
          case 'user': {
            const id = request.headers.get('X-Grid-User')
            if (!id) throw new Rejection('unauthorized', 401)
            return this.user(action, `acct:${hash(id).slice(0, 24)}`, data, now, colo)
          }
          case 'node':
            return this.worker(action, request.headers.get('X-Grid-Credential') ?? '', data, now, colo)
          case 'payments': {
            if (action === 'outcome') return this.applyCheckout(data, now)
            if (action !== 'reserve') throw new Rejection('not_found', 404)
            const id = request.headers.get('X-Grid-User')
            if (!id) throw new Rejection('unauthorized', 401)
            if (typeof data.live !== 'boolean' || typeof data.origin !== 'string') {
              throw new Rejection('invalid_checkout_context', 500)
            }
            return this.checkout(`acct:${hash(id).slice(0, 24)}`, data.credits, now, data.live, data.origin)
          }
          case 'reference': {
            const secret = this.env.NEUROGRID_VERIFIER_SECRET
            if (!secret || !timingSafeEqual(Buffer.from(hash(secret)),
              Buffer.from(hash(request.headers.get('X-Grid-Credential') ?? '')))) {
              throw new Rejection('unauthorized', 401)
            }
            return this.verifier(action, data, now)
          }
          default: throw new Rejection('not_found', 404)
        }
      })
      return Response.json(result)
    } catch (error) {
      if (error instanceof Rejection) return Response.json({ error: error.code }, { status: error.status })
      throw error
    } finally {
      this.ctx.waitUntil(this.refresh(now))
    }
  }
}

function isTensor(value: unknown, length: number): value is string {
  return typeof value === 'string' && length > 0 && length <= WINDOW
    && value.length === 4 * Math.ceil(length * WIDTH * 2 / 3) && /^[A-Za-z0-9+/]+={0,2}$/.test(value)
}

function validKey(value: unknown): value is string {
  return typeof value === 'string' && /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/.test(value)
}
