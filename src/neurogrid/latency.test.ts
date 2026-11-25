import { describe, expect, it } from 'vitest'
import { issueProbe, recordProbe, routeWorker, selectRoute, type WorkerLatency } from './latency'

describe('worker routing by latency', () => {
  const now = 100_000
  const node = (id: string, assigned: number, ms?: number, at = now) => ({
    id, assigned, shard: 0, seen: now, latency: ms === undefined ? undefined : { ms, at },
  })

  it('prefers a materially faster path even when it was assigned more recently', () => {
    const fast = node('fast', 98_000, 35)
    const slow = node('slow', 70_000, 200)
    expect(routeWorker([slow, fast], now)).toBe(fast)
    expect(routeWorker([slow], now)).toBe(slow)
    expect(routeWorker([], now)).toBeUndefined()
  })

  it('ranks a farther healthy worker ahead of an overloaded nearby one', () => {
    const nearby = { ...node('nearby', 80_000, 12), colo: 'IAD', queued: 2 }
    const distant = { ...node('distant', 98_000, 140), colo: 'ORD' }
    const output = { ...node('output', 70_000, 50), shard: 1 }
    expect(selectRoute([nearby, distant, output], 2, now, 'IAD')).toEqual(['distant', 'output'])
    expect(selectRoute([nearby, distant], 2, now)).toBeUndefined()
  })

  it('ignores stale samples and falls back to assignment order without recent samples', () => {
    const stale = node('stale', 20_000, 1, now - 60_000)
    const legacy = node('legacy', 25_000)
    const known = node('known', 98_000, 90)
    expect(routeWorker([stale, known, legacy], now)).toBe(known)
    expect(routeWorker([legacy, stale], now)).toBe(stale)
  })

  it('lets a slower worker claim a stage when the fastest worker has stopped polling', () => {
    const silent = { ...node('silent', 75_000, 5), seen: now - 8_000 }
    const polling = node('polling', 98_000, 90)
    expect(routeWorker([silent, polling], now)).toBe(polling)
  })

  it('measures a keyed round trip once, smooths new samples and limits probe frequency', () => {
    const first: WorkerLatency = {}
    const second: WorkerLatency = {}
    const probe = issueProbe(first, now)
    expect(probe).toBeTypeOf('string')
    expect(recordProbe(second, probe, now + 40)).toBeUndefined()
    expect(recordProbe(first, 'wrong', now + 40)).toBeUndefined()
    expect(recordProbe(first, probe, now + 40)).toBe(40)
    expect(recordProbe(first, probe, now + 41)).toBeUndefined()
    expect(issueProbe(first, now + 14_999)).toBeUndefined()
    const newer = issueProbe(first, now + 15_000)
    expect(recordProbe(first, newer, now + 15_080)).toBe(50)
    const late = issueProbe(first, now + 30_000)
    expect(recordProbe(first, late, now + 32_000)).toBeUndefined()
    expect(first.latency?.ms).toBe(50)
  })
})
