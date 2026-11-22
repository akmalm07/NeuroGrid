const PROBE_INTERVAL_MS = 15_000
const MAX_PROBE_MS = 2_000
const SAMPLE_TTL_MS = 60_000
const TIE_MS = 25
const ASSIGNMENT_FRESH_MS = 8_000

interface PingProbe { id?: string; sentAt: number }
interface LatencySample { ms: number; at: number }

export interface WorkerLatency {
  probe?: PingProbe
  latency?: LatencySample
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

export function firstWorker<T extends WorkerLatency & { id: string; assigned: number; seen: number }>(
  eligible: T[], now: number,
): T | undefined {
  // A silent worker must stop blocking assignments before the stage times out.
  const polling = eligible.filter((node) => node.seen + ASSIGNMENT_FRESH_MS > now)
  const best = Math.min(...polling.map((node) => freshLatency(node, now)))
  return polling.filter((node) => freshLatency(node, now) <= best + TIE_MS)
    .sort((a, b) => a.assigned - b.assigned || a.id.localeCompare(b.id))[0]
}
