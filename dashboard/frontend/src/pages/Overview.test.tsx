import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import { marketFixture, overviewFixture } from '../test/fixtures'
import Overview from './Overview'

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <MemoryRouter>
      <Overview />
    </MemoryRouter>,
  )
}

describe('Overview', () => {
  it('renders the six-second verdict from a real payload', async () => {
    mockFetch({ '/api/v1/overview': envelopeOf(overviewFixture()) })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('stat-tokens')).toBeInTheDocument())
    expect(screen.getByTestId('stat-tokens')).toHaveTextContent('66')
    expect(screen.getByTestId('stat-active-markets')).toHaveTextContent('33')
    expect(screen.getByTestId('stat-accept')).toHaveTextContent('3')
    expect(screen.getByTestId('stat-reject')).toHaveTextContent('30')
    expect(screen.getByTestId('stat-book-age')).toHaveTextContent('4')

    // collector & shadow chips present with their states
    expect(screen.getByTestId('chip-collector')).toHaveAttribute('data-state', 'healthy')
    expect(screen.getByTestId('chip-shadow')).toHaveTextContent('LIVE')

    // WS queue telemetry (Collector >= v0.1.2) reaches the health card
    expect(screen.getByTestId('ws-queue')).toHaveTextContent('高水位')
    expect(screen.getByTestId('ws-queue')).toHaveTextContent('11')

    // the decision card carries the no-profit disclaimer in its hint
    expect(screen.getByTestId('decision-distribution-card')).toHaveTextContent(
      '不代表已验证的盈利机会',
    )
  })

  it('labels a REPLAY shadow as REPLAY in the header chip', async () => {
    const replay = overviewFixture()
    replay.shadow = { ...replay.shadow!, mode: 'REPLAY' }
    mockFetch({ '/api/v1/overview': envelopeOf(replay) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('chip-shadow')).toBeInTheDocument())
    expect(screen.getByTestId('chip-shadow')).toHaveTextContent('Shadow REPLAY')
    expect(screen.getByTestId('chip-shadow')).toHaveAttribute('data-state', 'replay')
  })

  it('marks a stale heartbeat as stale, not as healthy', async () => {
    const stale = envelopeOf(overviewFixture(), {
      state: 'stale',
      age_seconds: 600,
      reason: 'collector: 600s old (limit 120s)',
    })
    mockFetch({ '/api/v1/overview': stale })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('freshness-pill')).toBeInTheDocument())
    expect(screen.getByTestId('freshness-pill')).toHaveAttribute('data-state', 'stale')
    expect(screen.getByTestId('freshness-pill')).toHaveTextContent('STALE')
  })

  it('shows MISSING (grey, not green) when the collector heartbeat is absent', async () => {
    const gone = overviewFixture()
    gone.collector = {
      ...gone.collector!,
      present: false,
      health_status: 'unknown',
      health_reasons: [],
      reason: 'collector heartbeat not found (collector not running, or not mounted)',
    }
    mockFetch({ '/api/v1/overview': envelopeOf(gone) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('chip-collector')).toBeInTheDocument())
    expect(screen.getByTestId('chip-collector')).toHaveAttribute('data-state', 'missing')
    expect(screen.getByTestId('chip-collector')).toHaveTextContent('MISSING')
    expect(screen.getByTestId('stat-collector')).toHaveTextContent(
      'collector heartbeat not found',
    )
  })

  it('shows model-vs-market as explicitly unavailable, with the reason', async () => {
    mockFetch({ '/api/v1/overview': envelopeOf(overviewFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('model-vs-market-empty')).toBeInTheDocument())
    expect(screen.getByTestId('model-vs-market-empty')).toHaveTextContent(
      'detail data awaits the shadow-side snapshot producer',
    )
  })

  it('renders model-vs-market rows when the producer has been integrated', async () => {
    const withRows = overviewFixture({
      model_vs_market: [marketFixture({ model_probability: 0.41, net_edge_usd: 0.03 })],
      model_vs_market_status: 'fresh',
      model_vs_market_reason: null,
    })
    mockFetch({ '/api/v1/overview': envelopeOf(withRows) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('model-vs-market-card')).toBeInTheDocument())
    expect(screen.getByTestId('model-vs-market-card')).toHaveTextContent('41.0%')
    expect(screen.getByTestId('model-vs-market-card')).toHaveTextContent('+0.030')
  })

  it('turns red when the collector goes unhealthy', async () => {
    const unhealthy = overviewFixture()
    unhealthy.collector = {
      ...unhealthy.collector!,
      health_status: 'unhealthy',
      health_reasons: ['upstream silent for 2000s'],
    }
    mockFetch({ '/api/v1/overview': envelopeOf(unhealthy) })
    renderPage()
    await waitFor(() =>
      expect(screen.getByTestId('chip-collector')).toHaveAttribute('data-state', 'unhealthy'),
    )
    expect(screen.getByTestId('unhealthy-banner')).toBeInTheDocument()
    expect(screen.getByTestId('unhealthy-banner')).toHaveTextContent('upstream silent')
  })

  it('marks a stale quote window as a warning, not as fresh', async () => {
    const stale = overviewFixture({ latest_book_age_seconds: 4000 })
    mockFetch({ '/api/v1/overview': envelopeOf(stale) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('stat-book-age')).toBeInTheDocument())
    expect(screen.getByTestId('stat-book-age')).toHaveTextContent('STALE')
    const value = screen.getByTestId('stat-book-age').querySelector('.tnum')!
    expect(value.className).toContain('text-warn')
  })

  it('renders an error card instead of a blank page when the API is down', async () => {
    rejectFetch()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('overview-error')).toBeInTheDocument())
    expect(screen.getByTestId('overview-error')).toHaveTextContent('无法获取 Dashboard 数据')
  })
})
