const PROBE_INTERVAL_MS = 15_000
const MAX_PROBE_MS = 2_000
const SAMPLE_TTL_MS = 60_000
const ASSIGNMENT_FRESH_MS = 8_000

interface PingProbe { id?: string; sentAt: number }
interface LatencySample { ms: number; at: number }

export interface WorkerLatency {
  probe?: PingProbe
  latency?: LatencySample
  execution?: LatencySample
  colo?: string
  failures?: number
}

export interface RouteWorker extends WorkerLatency {
  id: string
  shard: number
  seen: number
  assigned: number
  queued?: number
}

export function issueProbe(node: WorkerLatency, now: number): string | undefined {
  if (node.probe && now - node.probe.sentAt < PROBE_INTERVAL_MS) return
  const id = crypto.randomUUID()
  node.probe = { id, sentAt: now }
  return id
}

function freshLatency(node: WorkerLatency, now: number) {
  return node.latency && now - node.latency.at < SAMPLE_TTL_MS ? node.latency.ms : Infinity
}

export function recordProbe(node: WorkerLatency, id: unknown, now: number): number | undefined {
  const probe = node.probe
  if (typeof id !== 'string' || !probe || probe.id !== id) return
  const elapsed = now - probe.sentAt
  if (elapsed < 0 || elapsed >= MAX_PROBE_MS) return
  delete probe.id
  const previous = freshLatency(node, now)
  node.latency = { ms: Number.isFinite(previous) ? (3 * previous + elapsed) / 4 : elapsed, at: now }
  return Math.round(node.latency.ms)
}

export function latencyMs(node: WorkerLatency, now: number): number | undefined {
  const value = freshLatency(node, now)
  return Number.isFinite(value) ? Math.round(value) : undefined
}

export function recordExecution(node: WorkerLatency, elapsed: number, now: number) {
  if (!Number.isFinite(elapsed) || elapsed < 0) return
  const previous = node.execution
  node.execution = { ms: previous && now - previous.at < SAMPLE_TTL_MS
    ? (3 * previous.ms + elapsed) / 4 : elapsed, at: now }
  node.failures = Math.max(0, (node.failures ?? 0) - 1)
}

export function routeWorker<T extends RouteWorker>(eligible: T[], now: number, ingress?: string): T | undefined {
  const cost = (node: T) => {
    const link = freshLatency(node, now)
    const transit = Number.isFinite(link) ? link : 500
    const execution = node.execution && now - node.execution.at < SAMPLE_TTL_MS ? node.execution.ms : 300
    const region = ingress && node.colo === ingress ? 0 : 15
    return transit + execution + region + (node.queued ?? 0) * 400 + (node.failures ?? 0) * 500
      + (now - node.seen >= ASSIGNMENT_FRESH_MS ? 1_000 : 0)
  }
  return eligible.slice().sort((a, b) => cost(a) - cost(b) || a.assigned - b.assigned || a.id.localeCompare(b.id))[0]
}

export function selectRoute(workers: RouteWorker[], shards: number, now: number, ingress?: string): string[] | undefined {
  // All serving links are relayed by the coordinator, so their route costs are additive.
  const route = Array.from({ length: shards }, (_, shard) =>
    routeWorker(workers.filter((node) => node.shard === shard), now, ingress)?.id)
  return route.every((id): id is string => id !== undefined) ? route : undefined
}
