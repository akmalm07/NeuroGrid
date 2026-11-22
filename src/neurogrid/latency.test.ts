import { describe, expect, it } from 'vitest'
import { firstWorker, issueProbe, recordProbe, type WorkerLatency } from './latency'

describe('worker routing by latency', () => {
  const now = 100_000
  const node = (id: string, assigned: number, ms?: number, at = now) => ({
    id, assigned, seen: now, latency: ms === undefined ? undefined : { ms, at },
  })

  it('prefers a materially faster path even when it was assigned more recently', () => {
    const fast = node('fast', 98_000, 35)
    const slow = node('slow', 70_000, 200)
    expect(firstWorker([slow, fast], now)).toBe(fast)
    expect(firstWorker([slow], now)).toBe(slow)
    expect(firstWorker([], now)).toBeUndefined()
  })

  it('rotates among peers within 25 ms of the fastest sample', () => {
    const fast = node('fast', 98_000, 20)
    const near = node('near', 80_000, 40)
    const slow = node('slow', 70_000, 50)
    expect(firstWorker([slow, fast, near], now)).toBe(near)
  })

  it('ignores stale samples and falls back to assignment order without recent samples', () => {
    const stale = node('stale', 20_000, 1, now - 60_000)
    const legacy = node('legacy', 25_000)
    const known = node('known', 98_000, 90)
    expect(firstWorker([stale, known, legacy], now)).toBe(known)
    expect(firstWorker([legacy, stale], now)).toBe(stale)
  })

  it('lets a slower worker claim a stage when the fastest worker has stopped polling', () => {
    const silent = { ...node('silent', 75_000, 5), seen: now - 8_000 }
    const polling = node('polling', 98_000, 90)
    expect(firstWorker([silent, polling], now)).toBe(polling)
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
