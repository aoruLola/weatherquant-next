import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import { fetchEnvelope, usePoll } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('fetchEnvelope', () => {
  it('returns the parsed envelope on 200', async () => {
    mockFetch({ '/api/v1/overview': envelopeOf({ ok: true }) })
    const result = await fetchEnvelope<{ ok: boolean }>('/api/v1/overview')
    expect(result.data.ok).toBe(true)
    expect(result.schema_version).toBe(1)
  })

  it('classifies 429 separately so the UI can back off', async () => {
    const fn = vi.fn(async () => ({ ok: false, status: 429, json: async () => ({}) }))
    vi.stubGlobal('fetch', fn)
    await expect(fetchEnvelope('/api/v1/overview')).rejects.toMatchObject({
      kind: 'rate-limited',
    })
  })

  it('surfaces plain HTTP failures with their status', async () => {
    const fn = vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) }))
    vi.stubGlobal('fetch', fn)
    await expect(fetchEnvelope('/api/v1/overview')).rejects.toMatchObject({
      kind: 'http',
      status: 503,
    })
  })
})

describe('usePoll', () => {
  it('fetches immediately and exposes the envelope', async () => {
    const fetchMock = mockFetch({ '/api/v1/x': envelopeOf({ n: 1 }) })
    const { result } = renderHook(() => usePoll<{ n: number }>('/api/v1/x', 60000))
    expect(result.current.loading).toBe(true)
    await waitFor(() => expect(result.current.envelope).not.toBeNull())
    expect(result.current.envelope?.data.n).toBe(1)
    expect(result.current.error).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('records the error but keeps the last good payload', async () => {
    mockFetch({ '/api/v1/x': envelopeOf({ n: 1 }) })
    const { result } = renderHook(() => usePoll<{ n: number }>('/api/v1/x', 60000))
    await waitFor(() => expect(result.current.envelope?.data.n).toBe(1))

    // the next refresh fails: stale data stays, error is surfaced
    rejectFetch()
    result.current.refresh()
    await waitFor(() => expect(result.current.error).toBe('network'))
    expect(result.current.envelope?.data.n).toBe(1)
  })

  it('maps rate limits to their own error kind', async () => {
    const fn = vi.fn(async () => {
      throw Object.assign(new Error('429'), { kind: 'rate-limited', status: 429 })
    })
    vi.stubGlobal('fetch', fn)
    const { result } = renderHook(() => usePoll('/api/v1/x', 60000))
    await waitFor(() => expect(result.current.error).toBe('rate-limited'))
  })

  it('aborts the in-flight request on unmount', async () => {
    let capturedSignal: AbortSignal | undefined
    const fn = vi.fn((_url: string, init: any) => {
      capturedSignal = init?.signal
      return new Promise(() => {}) // never resolves
    })
    vi.stubGlobal('fetch', fn)
    const { unmount } = renderHook(() => usePoll('/api/v1/slow', 60000))
    await waitFor(() => expect(capturedSignal).toBeDefined())
    unmount()
    expect(capturedSignal!.aborted).toBe(true)
  })
})
