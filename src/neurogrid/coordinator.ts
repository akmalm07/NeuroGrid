import { createHash, timingSafeEqual } from 'node:crypto'
import { DurableObject } from 'cloudflare:workers'
import type { Env } from '../../worker'
import { EMPTY_HASH, TREASURY } from './ledger'
import {
  CONTEXT, GRANT, MAX_TOKENS, REVISION, SUPPLY, VOCAB, WIDTH, blockBytes, fee,
  type Block, type BlockData, type BlockKind, type JobSummary, type NetworkSnapshot, type Transfer,
} from './protocol'

const SEEN_MS = 22_000
const WORK_MS = 12_000
const JOB_MS = 180_000
const KEY_MS = 30 * 24 * 60 * 60 * 1_000
const hash = (value: string) => createHash('sha256').update(value).digest('hex')
const isInteger = (value: unknown, min: number, max: number): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max
const validToken = (value: unknown): value is number => isInteger(value, 0, VOCAB - 1)

interface Node {
  id: string
  owner: string
  shard: number
  seen: number
  assigned: number
  paused: boolean
  expires: number
}
interface Candidate { tensor: string; token?: number }
interface Job extends JobSummary {
  owner: string
  key: string
  expires: number
  reserved: number
  stepBy: number
  task: string
  tokens: number[]
  generated: number[]
  activation: string
  candidate?: Candidate
  lease?: { id: string; node: string; owner: string; expires: number }
  attempted: string[]
  rewards: Record<string, number>
  proof: string
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
      CREATE TABLE IF NOT EXISTS nodes (key TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS nodes_owner ON nodes(owner);
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
    `)
    this.ctx.storage.transactionSync(() => {
      if (!this.one('SELECT height FROM blocks WHERE height = 0')) {
        this.append('genesis', 'neurogrid-v1', [[TREASURY, SUPPLY]], Date.now())
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
      version: 1, height: head ? Number(head.height) + 1 : 0,
      previousHash: head ? String(head.hash) : EMPTY_HASH, createdAt, kind, reference, transfers, proof,
    }
    for (const [id, delta] of transfers) {
      const next = this.balance(id) + delta
      if (!isInteger(next, 0, SUPPLY)) throw new Error('Invalid balance transition')
      this.sql.exec('INSERT INTO balances VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET credits = excluded.credits', id, next)
    }
    const digest = hash(blockBytes(data))
    this.sql.exec('INSERT INTO blocks VALUES (?, ?, ?, ?)', data.height, kind, digest, JSON.stringify({ ...data, hash: digest }))
  }

  private grant(account: string, now: number) {
    if (this.one('SELECT id FROM balances WHERE id = ?', account)) return
    if (this.balance(TREASURY) < GRANT) return
    this.append('grant', account, [[TREASURY, -GRANT], [account, GRANT]], now)
  }

  private activeJob() {
    return this.read<Job>('SELECT data FROM jobs WHERE active = 1 LIMIT 1')
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
    return !node.paused && node.expires > now && node.seen + SEEN_MS > now
  }

  private verifierOnline(now: number) {
    return this.setting('verifier') + SEEN_MS > now
  }

  private clearJobInputs(job: Job) {
    job.tokens = []
    job.generated = []
    job.activation = ''
    delete job.candidate
    delete job.lease
  }

  private refund(job: Job, reason: string, now: number) {
    this.append('refund', job.id, [[`held:${job.id}`, -job.reserved], [job.owner, job.reserved]], now)
    job.status = 'refunded'
    job.reason = reason
    job.output = ''
    this.clearJobInputs(job)
    this.saveJob(job)
  }

  private settle(job: Job, now: number) {
    job.charge = fee(job.count)
    const credits = new Map<string, number>([[job.owner, job.reserved - job.charge], [TREASURY, 1]])
    for (const [owner, reward] of Object.entries(job.rewards)) {
      credits.set(owner, (credits.get(owner) ?? 0) + reward)
    }
    const transfers: Transfer[] = [[`held:${job.id}`, -job.reserved], ...credits]
    if (transfers.reduce((sum, [, amount]) => sum + amount, 0) !== 0) throw new Error('Unbalanced settlement')
    this.append('settle', job.id, transfers.filter(([, amount]) => amount !== 0), now, job.proof)
    this.set('settled', this.setting('settled') + 1)
    job.status = 'settled'
    this.clearJobInputs(job)
    this.saveJob(job)
  }

  private retry(job: Job, now: number) {
    delete job.lease
    delete job.candidate
    job.status = 'waiting'
    job.stepBy = now + WORK_MS
    this.saveJob(job)
  }

  private tick(now: number) {
    const job = this.activeJob()
    if (!job) return
    if (now >= job.expires || ['prepare', 'verifying', 'waiting'].includes(job.status) && now >= job.stepBy) {
      this.refund(job, job.status === 'waiting' ? 'partition_unavailable' : 'inference_timeout', now)
    } else if (job.status === 'working' && job.lease && now >= job.lease.expires) {
      this.retry(job, now)
    }
  }

  private summary(job: Job): JobSummary {
    const { id, prompt, status, stage, output, reason, count, maxTokens, charge, createdAt } = job
    return { id, prompt, status, stage, output: status === 'settled' ? output : '', reason, count, maxTokens, charge, createdAt }
  }

  private network(now: number): NetworkSnapshot {
    const active = this.activeJob()
    return {
      nodes: this.nodes().filter((node) => node.expires > now).map((node) => ({
        id: node.id, shard: node.shard,
        status: node.paused ? 'paused' : !this.available(node, now) ? 'offline'
          : active?.lease?.node === node.id ? 'working' : 'online',
      })),
      verifier: this.verifierOnline(now),
      settled: this.setting('settled'),
      height: Number(this.one('SELECT MAX(height) AS height FROM blocks')?.height),
      active: active ? { stage: active.stage, status: active.status, count: active.count } : null,
    }
  }

  private user(action: string, account: string, data: Record<string, unknown>, now: number) {
    switch (action) {
      case 'account':
        this.grant(account, now)
        return {
          credits: this.balance(account),
          jobs: this.readAll<Job>('SELECT data FROM jobs WHERE owner = ? ORDER BY created DESC LIMIT 20', account)
            .map((job) => this.summary(job)),
          nodes: this.readAll<Node>('SELECT data FROM nodes WHERE owner = ? ORDER BY id', account)
            .filter((node) => node.expires > now)
            .map(({ id, shard, paused, expires }) => ({ id, shard, paused, expires })),
        }
      case 'enroll': {
        this.sql.exec('DELETE FROM nodes WHERE json_extract(data, \'$.expires\') <= ?', now)
        const nodes = this.nodes()
        if (nodes.length >= 100 || nodes.filter((node) => node.owner === account).length >= 6) {
          throw new Rejection('node_limit')
        }
        const counts = [0, 1, 2].map((shard) => nodes.filter((node) => node.shard === shard).length)
        if (data.shard !== undefined && !isInteger(data.shard, 0, 2)) throw new Rejection('bad_shard', 400)
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
      case 'request': {
        const { prompt, maxTokens, key } = data
        if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 500
            || !isInteger(maxTokens, 1, MAX_TOKENS) || typeof key !== 'string' || !/^[\da-f-]{36}$/.test(key)) {
          throw new Rejection('invalid_request', 400)
        }
        const duplicate = this.read<Job>('SELECT data FROM jobs WHERE owner = ? AND request_key = ?', account, key)
        if (duplicate) return this.summary(duplicate)
        if (this.activeJob()) throw new Rejection('network_busy')
        const nodes = this.nodes()
        if (!this.verifierOnline(now) || [0, 1, 2].some((shard) =>
          !nodes.some((node) => node.shard === shard && this.available(node, now)))) {
          throw new Rejection('network_unavailable')
        }
        const requests = this.one('SELECT COUNT(*) AS n FROM jobs WHERE owner = ? AND created > ?', account, now - 60_000)
        if (Number(requests?.n) >= 6) throw new Rejection('rate_limit', 429)
        this.grant(account, now)
        const reserved = fee(maxTokens)
        if (this.balance(account) < reserved) throw new Rejection('insufficient_credits')
        const job: Job = {
          id: crypto.randomUUID(), owner: account, key, prompt: prompt.trim(), maxTokens, reserved,
          status: 'prepare', expires: now + JOB_MS, stepBy: now + WORK_MS, task: crypto.randomUUID(),
          output: '', reason: '', stage: 0, count: 0, charge: 0, createdAt: now,
          tokens: [], generated: [], activation: '', attempted: [], rewards: {}, proof: EMPTY_HASH,
        }
        this.append('reserve', job.id, [[account, -reserved], [`held:${job.id}`, reserved]], now)
        this.saveJob(job)
        return this.summary(job)
      }
      default: throw new Rejection('not_found', 404)
    }
  }

  private release(nodeId: string, now: number) {
    const job = this.activeJob()
    if (job?.lease?.node === nodeId && job.status === 'working') this.retry(job, now)
  }

  private input(job: Job) {
    return job.stage === 0 ? { tokens: job.tokens.concat(job.generated) } : { tensor: job.activation }
  }

  private worker(action: string, secret: string, data: Record<string, unknown>, now: number) {
    const key = hash(secret)
    const node = this.read<Node>('SELECT data FROM nodes WHERE key = ?', key)
    if (!node || node.expires <= now) throw new Rejection('invalid_node_key', 401)
    if (action === 'profile') return { id: node.id, shard: node.shard, revision: REVISION }
    if (data.revision !== REVISION) throw new Rejection('model_mismatch', 400)
    if (action === 'pause') {
      node.paused = true
      this.sql.exec('UPDATE nodes SET data = ? WHERE key = ?', JSON.stringify(node), key)
      this.release(node.id, now)
      return { ok: true }
    }
    const job = this.activeJob()
    const length = job ? job.tokens.length + job.generated.length : 0
    if (action === 'claim') {
      node.seen = now
      node.paused = false
      let work = null
      if (job && job.stage === node.shard && job.status === 'waiting' && !job.attempted.includes(node.id)) {
        const eligible = this.nodes().filter((other) => other.shard === node.shard && other.id !== node.id
          && this.available(other, now) && !job.attempted.includes(other.id))
        eligible.push(node)
        eligible.sort((a, b) => a.assigned - b.assigned || a.id.localeCompare(b.id))
        if (eligible[0].id === node.id) {
          node.assigned = now
          job.attempted.push(node.id)
          job.lease = { id: crypto.randomUUID(), node: node.id, owner: node.owner, expires: now + WORK_MS }
          job.status = 'working'
          this.saveJob(job)
          work = { lease: job.lease.id, input: this.input(job), length }
        }
      }
      this.sql.exec('UPDATE nodes SET data = ? WHERE key = ?', JSON.stringify(node), key)
      return { work, active: Boolean(job) }
    }
    if (action !== 'result') throw new Rejection('not_found', 404)
    if (!job || job.status !== 'working' || job.lease?.id !== data.lease || job.lease?.node !== node.id) {
      throw new Rejection('stale_lease')
    }
    if (!isTensor(data.tensor, length) || (job.stage === 2 && !validToken(data.token))) {
      this.retry(job, now)
      return { accepted: false, reason: 'malformed_result' }
    }
    job.candidate = { tensor: data.tensor, ...(job.stage === 2 ? { token: data.token as number } : {}) }
    job.status = 'verifying'
    job.stepBy = now + WORK_MS
    job.task = crypto.randomUUID()
    this.saveJob(job)
    return { accepted: true }
  }

  private verifier(action: string, data: Record<string, unknown>, now: number) {
    if (data.revision !== REVISION) throw new Rejection('model_mismatch', 400)
    this.set('verifier', now)
    const job = this.activeJob()
    if (action === 'claim') {
      if (job?.status === 'prepare') {
        return { work: { id: job.task, kind: 'prepare', prompt: job.prompt, maxTokens: job.maxTokens } }
      }
      if (job?.status === 'verifying') {
        return { work: { id: job.task, kind: 'verify', stage: job.stage, input: this.input(job),
          candidate: job.candidate, generated: job.generated, length: job.tokens.length + job.generated.length } }
      }
      return { work: null, active: Boolean(job) }
    }
    if (action !== 'result') throw new Rejection('not_found', 404)
    if (!job || !['prepare', 'verifying'].includes(job.status) || job.task !== data.task) {
      throw new Rejection('stale_verification')
    }
    if (job.status === 'prepare') {
      if (data.error === 'context_limit') {
        this.refund(job, 'context_limit', now)
      } else {
        if (!Array.isArray(data.tokens) || !data.tokens.length || data.tokens.length + job.maxTokens > CONTEXT
          || !data.tokens.every(validToken)) throw new Rejection('invalid_reference', 400)
        job.tokens = data.tokens
        job.status = 'waiting'
        job.stepBy = now + WORK_MS
        this.saveJob(job)
      }
      return { ok: true }
    }
    if (data.valid === false) {
      this.retry(job, now)
      return { ok: true }
    }
    const length = job.tokens.length + job.generated.length
    if (data.valid !== true || !job.lease || !job.candidate
      || (job.stage < 2 && !isTensor(data.tensor, length))
      || (job.stage === 2 && (!validToken(data.token) || data.token !== job.candidate.token
        || typeof data.text !== 'string' || data.text.length > 4_000 || typeof data.eos !== 'boolean'))) {
      throw new Rejection('invalid_reference', 400)
    }
    const { owner, node, id } = job.lease
    job.rewards[owner] = (job.rewards[owner] ?? 0) + 1
    job.proof = hash(JSON.stringify([job.proof, REVISION, job.count, job.stage, node, id,
      hash(JSON.stringify(this.input(job))), hash(JSON.stringify(job.candidate))]))
    delete job.lease
    delete job.candidate
    job.attempted = []
    if (job.stage === 2) {
      job.generated.push(data.token as number)
      job.output = data.text as string
      job.count += 1
      if (data.eos || job.count === job.maxTokens) {
        this.settle(job, now)
        return { ok: true }
      }
      job.stage = 0
      job.activation = ''
    } else {
      job.stage += 1
      job.activation = data.tensor as string
    }
    job.status = 'waiting'
    job.stepBy = now + WORK_MS
    this.saveJob(job)
    return { ok: true }
  }

  private async refresh(now: number) {
    const job = this.activeJob()
    const deadline = job ? Math.min(job.expires, job.status === 'working' ? job.lease!.expires : job.stepBy) : Infinity
    const heartbeat = this.nodes().flatMap((node) => this.available(node, now)
      ? [Math.min(node.seen + SEEN_MS, node.expires)] : [])
    if (this.verifierOnline(now)) heartbeat.push(this.setting('verifier') + SEEN_MS)
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
            return this.user(action, `acct:${hash(id).slice(0, 24)}`, data, now)
          }
          case 'node':
            return this.worker(action, request.headers.get('X-Grid-Credential') ?? '', data, now)
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
  return typeof value === 'string' && length > 0 && length <= CONTEXT
    && value.length === length * WIDTH * 4 / 3 * 4 && /^[A-Za-z0-9+/]+$/.test(value)
}
