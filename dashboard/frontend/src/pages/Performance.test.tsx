/**
 * Performance page: what it must never do is as important as what it shows.
 *
 * A missing backfill must not look like a dashboard with no edge, a win
 * rate with too few samples must say so rather than render a ratio, and no
 * filter or empty state may hide the fact that nothing here was traded.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import Performance from './Performance'
import type { PerformancePayload } from '../types'

afterEach(() => vi.unstubAllGlobals())

function payload(overrides: Partial<PerformancePayload> = {}): PerformancePayload {
  return {
    performance_schema_version: 1,
    kind: 'performance-summary',
    generated_at_utc: new Date().toISOString(),
    as_of_utc: new Date().toISOString(),
    source: 'weatherquant-research',
    dataset: 'live',
    sample_size: 42,
    statistical_scope: ['observation_all_accepts', 'opportunity_episodes'],
    warnings: ['Observation Edge Sum counts repeated evaluations; it is not profit.'],
    data: {
      observation_edge_sum: 12.5,
      unique_opportunity_edge_sum: 4.2,
      average_net_edge: 0.05,
      realized_directional_win_rate: null,
      verified_opportunities: 0,
      settled_markets: 0,
      counts: { WIN: 0, LOSS: 0, PENDING: 12 },
      opportunity_episodes: 7,
      accept_observations: 42,
      accept_opportunity_distinct_markets: 4,
      accept_opportunity_distinct_target_dates: 5,
      distinct_markets: 4,
      distinct_target_dates: 5,
      insufficient_reason: 'N/A — insufficient resolved independent samples',
    },
    ...overrides,
  }
}

function emptyPayload(reason: string): PerformancePayload {
  return {
    reason,
    statistical_scope: [],
    warnings: [],
    sample_size: 0,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <Performance />
    </MemoryRouter>,
  )
}

function mockAll(overrides: Record<string, any> = {}) {
  const base = {
    '/api/v1/performance/summary': envelopeOf(payload()),
    '/api/v1/performance/edge': envelopeOf(
      payload({
        kind: 'performance-edge',
        data: {
          overall: { mean: 0.05, median: 0.04, p05: 0.01, p95: 0.09, count: 42 },
          by_model: { calibrated: { mean: 0.06, count: 20 }, gfs_raw: { mean: 0.03, count: 22 } },
          by_edge_band: { '0.05_0.10': [0.06, 0.07] },
        },
      }),
    ),
    '/api/v1/performance/win-rate': envelopeOf(
      payload({
        kind: 'performance-win-rate',
        data: {
          overall: {
            win_rate: null,
            counts: { WIN: 0, LOSS: 0, PENDING: 12 },
            valid_samples: 0,
            distinct_markets: 0,
            distinct_target_dates: 0,
            confidence_interval: null,
            method: 'none',
            reason: 'N/A — insufficient resolved independent samples',
          },
        },
      }),
    ),
    '/api/v1/performance/opportunities?limit=50': envelopeOf(
      payload({
        kind: 'performance-opportunities',
        items: [
          {
            episode_id: 'ep-1',
            first_seen_utc: '2026-10-13T12:00:00+00:00',
            target_date: '2026-10-14',
            bracket_label: '81°F',
            model_name: 'calibrated',
            direction: 'buy_yes',
            first_edge: 0.05,
            first_model_probability: 0.65,
            first_market_price: 0.6,
            duration_seconds: 600,
            observation_count: 4,
            outcome: 'LOSS',
          },
        ],
        total: 1,
        next_cursor: null,
      }),
    ),
    '/api/v1/performance/models': envelopeOf(
      payload({ kind: 'performance-models', data: { by_model_edge: {} } }),
    ),
    '/api/v1/performance/reference-capital?capital_usdc=100&mode=portfolio': envelopeOf(
      payload({
        kind: 'performance-summary',
        statistical_scope: ['reference_capital_hypothetical'],
        data: {
          capital_usdc: 100,
          utilization: 0.4,
          executable_count: 3,
          not_executable_count: 4,
          capacity_limited_count: 1,
          allocated_total_usdc: 40,
          peak_open_allocation_usdc: 40,
          balance_remaining_usdc: 60,
          theoretical_expected_net_edge_usdc: 1.2,
          theoretical_expected_roi: 0.03,
          backtestable: false,
          not_backtestable_reasons: ['no episode could be sized from retained book levels'],
          warnings: ['Reference capital is a research assumption, not a funded account.'],
        },
      }),
    ),
    ...overrides,
  }
  return Object.fromEntries(
    Object.entries(base).map(([path, body]) => [path, body]),
  )
}

describe('Performance page', () => {
  it('keeps the disclaimer on screen no matter what', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() =>
      expect(screen.getByTestId('performance-disclaimer')).toBeInTheDocument(),
    )
    const text = screen.getByTestId('performance-disclaimer').textContent ?? ''
    expect(text).toContain('不是利润')
    expect(text).toContain('实际收益恒为 N/A')
  })

  it('shows the disclaimer even when everything is missing', async () => {
    const missing = Object.fromEntries(
      Object.keys(mockAll()).map((path) => [path, envelopeOf(emptyPayload('backfill has not run'))]),
    )
    mockFetch(missing)
    renderPage()

    await waitFor(() =>
      expect(screen.getByTestId('performance-disclaimer')).toBeInTheDocument(),
    )
    expect(screen.getAllByTestId('performance-missing').length).toBeGreaterThan(0)
  })

  it('reports a missing backfill as unavailable, never as zero', async () => {
    const missing = Object.fromEntries(
      Object.keys(mockAll()).map((path) => [path, envelopeOf(emptyPayload('backfill has not run'))]),
    )
    mockFetch(missing)
    renderPage()

    // Every section reports the same reason, so there is more than one.
    await waitFor(() => expect(screen.getAllByText('backfill has not run').length).toBeGreaterThan(0))
    // The zero-shaped stat cards belong to a payload that exists.
    expect(screen.queryByTestId('stat-observation-edge-sum')).not.toBeInTheDocument()
    expect(screen.queryByTestId('stat-win-rate')).not.toBeInTheDocument()
  })

  it('labels the two edge sums differently', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('stat-observation-edge-sum')).toBeInTheDocument())
    expect(screen.getByTestId('stat-unique-edge-sum')).toBeInTheDocument()
    // The repeated-evaluation total must carry its caveat inline.
    expect(screen.getByTestId('stat-observation-edge-sum')).toHaveTextContent('非利润')
  })

  it('refuses to state a win rate with too few samples', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('stat-wr')).toBeInTheDocument())
    expect(screen.getByTestId('stat-wr')).toHaveTextContent('N/A')
    expect(screen.getByTestId('win-rate-reason')).toHaveTextContent('insufficient')
  })

  it('rejects a reference capital that is not positive', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('capital-input')).toBeInTheDocument())
    const input = screen.getByTestId('capital-input') as HTMLInputElement
    // The invalid value is not sent; the panel explains instead of 422ing.
    const { fireEvent } = await import('@testing-library/react')
    fireEvent.change(input, { target: { value: '0' } })

    await waitFor(() => expect(screen.getByTestId('capital-invalid')).toBeInTheDocument())
  })

  it('says actual profit is N/A because nothing is traded', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('capital-result')).toBeInTheDocument())
    expect(screen.getByText(/实际收益：N\/A/)).toBeInTheDocument()
  })

  it('renders the opportunity table with its outcome column', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities')).toBeInTheDocument())
    const row = screen.getByTestId('opportunity-row')
    expect(row).toHaveTextContent('calibrated')
    expect(row).toHaveTextContent('LOSS')
    expect(row).toHaveTextContent('81°F')
  })

  it('shows an empty state rather than a fabricated table', async () => {
    const none = {
      ...mockAll(),
      '/api/v1/performance/opportunities?limit=50': envelopeOf(
        payload({ kind: 'performance-opportunities', items: [], total: 0 }),
      ),
    }
    mockFetch(none)
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities-empty')).toBeInTheDocument())
    expect(screen.queryByTestId('opportunities')).not.toBeInTheDocument()
  })

  it('surfaces an unreachable API as an error state', async () => {
    rejectFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('capital-error')).toBeInTheDocument())
  })

  it('shows the statistical scope wherever a number appears', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('performance-summary')).toBeInTheDocument())
    expect(screen.getByTestId('performance-summary')).toHaveTextContent('observation_all_accepts')
  })
})
