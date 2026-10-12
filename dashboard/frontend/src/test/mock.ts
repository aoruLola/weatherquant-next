import { vi } from 'vitest'
import type { Envelope } from '../types'

/** Install a fetch mock serving `routes` (longest-prefix match on the URL). */
export function mockFetch(routes: Record<string, unknown>) {
  const fn = vi.fn(async (input: any, _init?: any) => {
    const path = String(input)
    const key = Object.keys(routes)
      .filter((k) => path.startsWith(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) {
      return { ok: false, status: 404, json: async () => ({ detail: 'not found' }) }
    }
    const value = routes[key]
    const payload = typeof value === 'function' ? (value as any)(path) : value
    return { ok: true, status: 200, json: async () => payload }
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

export function rejectFetch(error: unknown = new Error('network down')) {
  const fn = vi.fn(async () => {
    throw error
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

export function envelopeOf<T>(data: T, freshness?: Envelope<T>['freshness']): Envelope<T> {
  return {
    schema_version: 1,
    api: 'test',
    generated_at_utc: new Date().toISOString(),
    dataset: 'live',
    freshness: freshness ?? { state: 'fresh', age_seconds: 2 },
    data,
  }
}
