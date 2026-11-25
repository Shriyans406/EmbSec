// Hooks and helpers that are not components.
import { useCallback, useEffect, useRef, useState } from 'react'

export function usePoll(fn, ms = 4000, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)
  const load = useCallback(async () => {
    try {
      const d = await fn()
      if (alive.current) {
        setData(d)
        setError(null)
      }
    } catch (e) {
      if (alive.current) setError(e)
    } finally {
      if (alive.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    alive.current = true
    load()
    const t = ms ? setInterval(load, ms) : null
    return () => {
      alive.current = false
      if (t) clearInterval(t)
    }
  }, [load, ms])
  return { data, error, loading, reload: load }
}

export const when = (ts) => (ts ? new Date(ts).toLocaleString() : '-')

export const go = (path) => {
  location.hash = `#/${path}`
}
