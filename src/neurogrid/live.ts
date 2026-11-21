import { useEffect, useState } from 'react'
import { useQuery } from 'deepspace'
import { readNetwork } from './api'

export function useNetwork(revision?: string) {
  const [result, setResult] = useState<Awaited<ReturnType<typeof readNetwork>>>()
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let current = true
    let pending = false
    async function load() {
      if (pending) return
      pending = true
      try {
        const state = await readNetwork()
        if (current) { setResult(state); setFailed(false) }
      } catch {
        if (current) setFailed(true)
      } finally {
        pending = false
      }
    }
    void load()
    const timeout = setInterval(load, 8_000)
    return () => { current = false; clearInterval(timeout) }
  }, [revision])
  return { result, failed }
}

export function useGridRevision() {
  const { records } = useQuery<{ revision: string }>('grid_status', { limit: 1 })
  return records[0]?.data.revision
}
