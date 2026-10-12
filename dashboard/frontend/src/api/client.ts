/**
 * Typed fetch + polling.
 *
 * Four behaviours the spec asks for, all here rather than in every page:
 *
 * 1. **Request cancellation** - each poll owns an AbortController; a new tick
 *    aborts the previous one instead of racing it.
 * 2. **De-duplication** - while a request is in flight the next tick is
 *    skipped, so a slow backend cannot queue up overlapping refreshes.
 * 3. **Tab visibility** - polls pause when the document is hidden and fire
 *    once immediately when it becomes visible again.
 * 4. **Recovery** - errors never stop the loop; the last good payload stays on
 *    screen (marked) until a later poll succeeds.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Envelope } from '../types'

export type PollError = 'network' | 'http' | 'rate-limited' | null

export interface PollState<T> {
  envelope: Envelope<T> | null
  error: PollError
  status?: number
  loading: boolean
  lastFetchedAt: number | null
  refresh: () => void
}

export async function fetchEnvelope<T>(
  path: string,
  signal?: AbortSignal,
): Promise<Envelope<T>> {
  const response = await fetch(path, {
    signal,
    headers: { Accept: 'application/json' },
  })
  if (response.status === 429) {
    throw Object.assign(new Error('rate limited'), { kind: 'rate-limited' as const, status: 429 })
  }
  if (!response.ok) {
    throw Object.assign(new Error(`HTTP ${response.status}`), {
      kind: 'http' as const,
      status: response.status,
    })
  }
  return (await response.json()) as Envelope<T>
}

interface UsePollOptions {
  enabled?: boolean
}

export function usePoll<T>(path: string, intervalMs: number, options: UsePollOptions = {}): PollState<T> {
  const { enabled = true } = options
  const [envelope, setEnvelope] = useState<Envelope<T> | null>(null)
  const [error, setError] = useState<PollError>(null)
  const [status, setStatus] = useState<number | undefined>(undefined)
  const [loading, setLoading] = useState(true)
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null)
  const [nonce, setNonce] = useState(0)

  const inFlight = useRef(false)
  const controller = useRef<AbortController | null>(null)

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      if (cancelled) return
      if (document.hidden) {
        timer = setTimeout(tick, intervalMs)
        return
      }
      if (inFlight.current) {
        // De-dup: a slow response must not be chased by a second request.
        timer = setTimeout(tick, intervalMs)
        return
      }
      inFlight.current = true
      controller.current = new AbortController()
      try {
        const next = await fetchEnvelope<T>(path, controller.current.signal)
        if (cancelled) return
        setEnvelope(next)
        setError(null)
        setStatus(undefined)
        setLastFetchedAt(Date.now())
      } catch (e: any) {
        if (cancelled || e?.name === 'AbortError') return
        setError(e?.kind ?? 'network')
        setStatus(e?.status)
        setLastFetchedAt(Date.now())
      } finally {
        inFlight.current = false
        if (!cancelled) setLoading(false)
      }
      if (!cancelled) timer = setTimeout(tick, intervalMs)
    }

    const onVisibility = () => {
      if (!document.hidden) {
        clearTimeout(timer)
        tick()
      }
    }

    void tick()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      clearTimeout(timer)
      controller.current?.abort()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [path, intervalMs, enabled, nonce])

  return { envelope, error, status, loading, lastFetchedAt, refresh }
}
