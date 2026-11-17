import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { marketFixture, marketsFixture } from '../test/fixtures'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import Markets from './Markets'

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <MemoryRouter>
      <Markets />
    </MemoryRouter>,
  )
}

describe('Markets', () => {
  it('renders one card per market with quotes and freshness', async () => {
    mockFetch({
      '/api/v1/markets': envelopeOf(
        marketsFixture([
          marketFixture(),
          marketFixture({
            market_id: 'm-80',
            bracket_label: '80°F or below',
            question: 'Will the highest temperature in New York City be 80°F or below?',
          }),
        ]),
      ),
    })
    renderPage()

    await waitFor(() => expect(screen.getAllByTestId('market-card')).toHaveLength(2))
    const first = screen.getAllByTestId('market-card')[0]
    expect(within(first).getByText('81°F')).toBeInTheDocument()
    expect(first).toHaveTextContent('0.62')
    expect(first).toHaveTextContent('0.65')
    // model probability honestly absent — N/A, never an empty cell
    expect(first).toHaveTextContent('N/A')
    expect(first).toHaveTextContent('OPEN')
  })

  it('shows UNKNOWN rather than OPEN when the schedule is unavailable', async () => {
    const unknown = marketFixture({
      open_now: null,
      open_now_source: null,
      open_now_reason: 'timezone-aware market start and end times are required',
      start_time_utc: null,
      end_time_utc: null,
      target_date: null,
      rules_text: null,
      resolution_source: null,
      rules_source: null,
      rules_reason: 'Gamma metadata missing: rules_text, resolution_source',
    })
    mockFetch({ '/api/v1/markets': envelopeOf(marketsFixture([unknown])) })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('market-open-state')).toBeInTheDocument())
    const chip = screen.getByTestId('market-open-state')
    expect(chip).toHaveTextContent('UNKNOWN')
    expect(chip).not.toHaveTextContent('OPEN')
    // reason comes straight from the backend (or the Chinese fallback when
    // the backend left it blank) — either is fine; the key contract is
    // UNKNOWN rather than a guessed OPEN.
    const title = chip.getAttribute('title') ?? ''
    expect(title).not.toBe('')
    // missing target date and rules are named, not left blank
    expect(screen.getByTestId('market-card')).toHaveTextContent('目标日 N/A')
    expect(screen.getByTestId('market-card')).toHaveTextContent('规则 N/A')
  })

  it('marks stale books as STALE instead of quoting them as live', async () => {
    const staleMarket = marketFixture({
      book: {
        ...marketFixture().book!,
        stale: true,
        age_seconds: 3600,
        reason: 'book is 3600s old (limit 900s)',
      },
    })
    mockFetch({ '/api/v1/markets': envelopeOf(marketsFixture([staleMarket])) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('market-card')).toBeInTheDocument())
    expect(screen.getByTestId('market-card')).toHaveTextContent('STALE')
  })

  it('shows an explicit empty state when nothing matches', async () => {
    mockFetch({ '/api/v1/markets': envelopeOf(marketsFixture([])) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('markets-empty')).toBeInTheDocument())
    expect(screen.getByTestId('markets-empty')).toHaveTextContent('没有符合条件的市场')
  })

  it('surfaces a missing archive as missing, not as an empty-but-healthy list', async () => {
    const missing = marketsFixture([])
    missing.source_status = 'missing'
    missing.reason = 'collector raw archive not found'
    mockFetch({ '/api/v1/markets': envelopeOf(missing, { state: 'missing', reason: 'no archive' }) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('markets-empty')).toBeInTheDocument())
    expect(screen.getByTestId('markets-empty')).toHaveTextContent('collector raw archive not found')
  })

  it('closes a market whose schedule has ended, even if a New York day label remains', async () => {
    mockFetch({
      '/api/v1/markets': envelopeOf(
        marketsFixture([
          marketFixture({
            target_day_ended: true,
            open_now: false,
            open_now_reason: 'current time is at/after the market end (end is exclusive)',
          }),
        ]),
      ),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('market-card')).toBeInTheDocument())
    expect(screen.getByText('目标日已过')).toBeInTheDocument()
    expect(screen.getByTestId('market-open-state')).toHaveTextContent('CLOSED')
    expect(screen.queryByText('ACTIVE')).not.toBeInTheDocument()
    expect(screen.queryByText('INACTIVE')).not.toBeInTheDocument()
  })

  it('opens on tradeable markets instead of the whole archive tail', async () => {
    const fetchMock = mockFetch({
      '/api/v1/markets': envelopeOf(marketsFixture([marketFixture()])),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('market-card')).toBeInTheDocument())
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('status=tradeable'))).toBe(true)
    })
    expect(screen.getByLabelText('市场状态')).toHaveValue('tradeable')
  })

  it('passes filter changes into the polled URL', async () => {
    const fetchMock = mockFetch({ '/api/v1/markets': envelopeOf(marketsFixture([marketFixture()])) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('market-card')).toBeInTheDocument())

    const { fireEvent } = await import('@testing-library/react')
    fireEvent.change(screen.getByLabelText('市场状态'), { target: { value: 'tradeable' } })

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('status=tradeable'))).toBe(true)
    })

    fireEvent.change(screen.getByTestId('filter-decision'), { target: { value: 'ACCEPT' } })
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('decision=ACCEPT'))).toBe(true)
    })
  })

  it('renders an error card when the API is unreachable', async () => {
    rejectFetch()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('markets-error')).toBeInTheDocument())
  })
})
