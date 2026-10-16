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
          by_edge_band: { '0.05_0.10': { count: 2, mean: 0.065, p95: 0.07 } },
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
        prev_cursor: null,
        generation_id: 'gen-1',
        data: {
          collection: 'episodes',
          returned: 1,
          total: 1,
          offset: 0,
          limit: 50,
          filters: {},
          window: { episodes: 1, episodes_all: 1, days: 90, complete: true },
          statistical_scope: ['opportunity_episodes'],
          warnings: [],
        },
      }),
    ),
    '/api/v1/performance/models': envelopeOf(
      payload({ kind: 'performance-models', data: { by_model_edge: {} } }),
    ),
    '/api/v1/performance/reference-capital?capital_usdc=100&mode=portfolio': envelopeOf(
      // Flat, as the endpoint returns it: the numbers sit on the payload, not
      // inside a `data` block. A nested fixture would leave the panel's figures
      // unasserted, which is how it went unnoticed that they were undefined.
      payload({
        kind: 'performance-summary',
        statistical_scope: ['reference_capital_hypothetical', 'opportunity_episodes'],
        warnings: ['Reference capital is a research assumption, not a funded account.'],
        scope: 'window',
        mode: 'portfolio',
        capital_usdc: 100,
        initial_capital_usdc: 100,
        utilization: 0.4,
        peak_utilization: 0.4,
        executable_count: 3,
        not_executable_count: 4,
        capacity_limited_count: 1,
        allocated_total_usdc: 40,
        peak_open_allocation_usdc: 40,
        balance_remaining_usdc: 60,
        settled_count: 0,
        funding_state: 'fills_unsettled',
        funding_state_reason: '3 position(s) were sized but no settlement was applied',
        simulated_net_profit_usdc: null,
        simulated_roi: null,
        simulated_result_reason: 'no simulated profit is reported: capital is still committed',
        theoretical_expected_net_edge_usdc: 1.2,
        theoretical_expected_roi: 0.03,
        backtestable: false,
        not_backtestable_reasons: ['no episode could be sized from retained book levels'],
        window: { complete: true, episodes: 3, days: 90 },
        window_completeness: 'complete',
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

  it('shows the lifetime and the window capital apart, each labelled', async () => {
    // Two simulations, two accounts. The lifetime block comes from the
    // published summary and covers everything; the block below it is the
    // on-demand stake over the detail window. Neither may be read as the other.
    const routes = mockAll()
    routes['/api/v1/performance/summary'] = envelopeOf(
      payload({
        data: {
          ...(payload().data as Record<string, any>),
          reference_capital: {
            scope: 'lifetime',
            capital_usdc: 100,
            executable_count: 0,
            not_executable_count: 12,
            peak_utilization: 0,
            funding_state: 'no_fills_simulated',
            funding_state_reason: 'no opportunity could be sized from the evidence available',
            simulated_net_profit_usdc: null,
            not_executable_reason_counts: { NO_BOOK: 12 },
            statistical_window: { first_seen_utc_from: '2026-10-01T00:00:00+00:00', last_seen_utc_to: '2026-10-13T12:00:00+00:00', episodes_simulated: 12 },
            data_watermark: ['2026-10-15T05:45:04+00:00', 'd000001'],
            settlement_coverage: { decisive_episodes: 3, episodes: 12 },
            warnings: ['Reference capital is a research assumption.'],
          },
        },
      }),
    )
    mockFetch(routes)
    renderPage()

    await waitFor(() => expect(screen.getByTestId('lifetime-capital')).toBeInTheDocument())
    const lifetime = screen.getByTestId('lifetime-capital')
    expect(lifetime).toHaveTextContent('scope = lifetime')
    expect(lifetime).toHaveTextContent('全历史模拟净收益')
    expect(lifetime).toHaveTextContent('N/A')
    expect(lifetime).toHaveTextContent('no_fills_simulated')
    expect(screen.getByTestId('lifetime-capital-detail')).toHaveTextContent('NO_BOOK 12')
    expect(screen.getByTestId('lifetime-capital-detail')).toHaveTextContent('3 / 12')

    // And the on-demand block is labelled as a window, not merged into it.
    await waitFor(() => expect(screen.getByTestId('capital-result')).toBeInTheDocument())
    expect(screen.getByTestId('reference-capital')).toHaveTextContent('scope = window')
  })

  it('renders the edge band as a distribution, not as a list of values', async () => {
    mockFetch(mockAll())
    renderPage()

    await waitFor(() => expect(screen.getByTestId('performance-edge')).toBeInTheDocument())
    // The band used to publish every value and the UI counted them; it now
    // publishes the count and the mean, and the page shows both.
    expect(screen.getByTestId('performance-edge')).toHaveTextContent('2 次评估')
  })
})

/**
 * Paging is the point of this round: the browser must never receive the
 * history. These tests hold that line from the UI side - a page request is a
 * page request, and the filters travel in the query rather than being applied
 * to rows the browser already has.
 */
describe('Performance page: paged detail', () => {
  function pagedFetch() {
    const rows = (from: number, count: number) =>
      Array.from({ length: count }, (_, index) => ({
        episode_id: `ep-${from + index}`,
        first_seen_utc: `2026-10-13T12:${String(from + index).padStart(2, '0')}:00+00:00`,
        target_date: '2026-10-14',
        bracket_label: '81°F',
        model_name: 'calibrated',
        direction: 'buy_yes',
        first_edge: 0.05,
        duration_seconds: 600,
        observation_count: 3,
        outcome: 'WIN',
      }))

    const base = mockAll()
    // mockFetch picks the longest matching prefix, so the single-page fixture
    // has to go before the paged one can answer.
    delete base['/api/v1/performance/opportunities?limit=50']
    return mockFetch({
      ...base,
      '/api/v1/performance/models': envelopeOf(
        payload({
          kind: 'performance-models',
          data: { models: ['calibrated', 'gfs_raw'], by_model_edge: {} },
        }),
      ),
      '/api/v1/performance/opportunities': (path: string) => {
        const query = new URLSearchParams(path.split('?')[1] ?? '')
        const second = query.get('cursor') === 'cursor-2'
        const filtered = query.has('model')
        return envelopeOf(
          payload({
            kind: 'performance-opportunities',
            generation_id: 'gen-1',
            items: filtered ? [] : second ? rows(2, 2) : rows(0, 2),
            total: filtered ? 0 : 4,
            next_cursor: second || filtered ? null : 'cursor-2',
            prev_cursor: second ? 'cursor-0' : null,
            data: {
              collection: 'episodes',
              returned: 2,
              total: filtered ? 0 : 4,
              offset: second ? 2 : 0,
              limit: 50,
              filters: filtered ? { model: query.get('model') } : {},
              window: { episodes: 4, episodes_all: 40, days: 90, complete: false },
              statistical_scope: ['opportunity_episodes', 'window:90d'],
              warnings: [],
            },
          }),
        )
      },
    })
  }

  it('asks the server for the next page instead of slicing rows it has', async () => {
    const fetchMock = pagedFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities')).toBeInTheDocument())
    expect(screen.getAllByTestId('opportunity-row')).toHaveLength(2)
    expect(screen.getByTestId('page-summary')).toHaveTextContent('1–2 / 4 条')

    screen.getByTestId('page-next').click()

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call: any[]) => String(call[0]))
      expect(urls.some((url) => url.includes('cursor=cursor-2'))).toBe(true)
    })
    // Page two, and the row set changed: the server sent different rows.
    await waitFor(() =>
      expect(screen.getByTestId('page-summary')).toHaveTextContent('3–4 / 4 条'),
    )
  })

  it('offers the previous page only when there is one', async () => {
    pagedFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities')).toBeInTheDocument())
    expect(screen.getByTestId('page-prev')).toBeDisabled()

    screen.getByTestId('page-next').click()
    await waitFor(() => expect(screen.getByTestId('page-prev')).not.toBeDisabled())

    screen.getByTestId('page-prev').click()
    await waitFor(() => expect(screen.getByTestId('page-summary')).not.toHaveTextContent('1–2 / 4'))
  })

  it('sends a filter to the server and returns to the first page', async () => {
    const fetchMock = pagedFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities')).toBeInTheDocument())
    screen.getByTestId('page-next').click()
    await waitFor(() => expect(screen.getByTestId('page-prev')).not.toBeDisabled())

    const select = screen.getByTestId('filter-model') as HTMLSelectElement
    select.value = 'calibrated'
    select.dispatchEvent(new Event('change', { bubbles: true }))

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call: any[]) => String(call[0]))
      const last = urls[urls.length - 1] ?? ''
      expect(last).toContain('model=calibrated')
      expect(last).not.toContain('cursor=')
    })
    await waitFor(() => expect(screen.getByTestId('opportunities-empty')).toBeInTheDocument())
  })

  it('says the detail is a window, and that the aggregates are not', async () => {
    pagedFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('window-note')).toBeInTheDocument())
    expect(screen.getByTestId('window-note')).toHaveTextContent('共 40 条')
    expect(screen.getByTestId('window-note')).toHaveTextContent('覆盖全部历史')
  })

  it('does not show the previous mode answer under the new mode label', async () => {
    // Switching the mode is a different question. Reusing the last answer would
    // render the portfolio figures under the single-opportunity heading.
    const fetchMock = mockFetch({
      ...mockAll(),
      '/api/v1/performance/reference-capital': (path: string) => {
        const single = path.includes('single_opportunity')
        return envelopeOf(
          payload({
            kind: 'performance-summary',
            statistical_scope: ['reference_capital_hypothetical'],
            warnings: [],
            scope: 'window',
            mode: single ? 'single_opportunity' : 'portfolio',
            capital_usdc: 100,
            executable_count: single ? 7 : 3,
            not_executable_count: 0,
            results_total: single ? 7 : 3,
            window: { complete: true, episodes: 3, days: 90 },
            window_completeness: 'complete',
          }),
        )
      },
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('capital-result')).toBeInTheDocument())
    expect(screen.getByTestId('capital-result')).toHaveTextContent('3 / 7')

    const select = screen.getByTestId('capital-mode') as HTMLSelectElement
    select.value = 'single_opportunity'
    select.dispatchEvent(new Event('change', { bubbles: true }))

    const urls = fetchMock.mock.calls.map((call: any[]) => String(call[0]))
    expect(urls.some((url) => url.includes('mode=single_opportunity'))).toBe(true)
    // And the portfolio answer is gone while the new one is in flight.
    await waitFor(() =>
      expect(screen.getByTestId('capital-result')).toHaveTextContent('7 / 7'),
    )
    expect(screen.getByTestId('capital-result')).not.toHaveTextContent('3 / 7')
  })

  it('shows a reason when the window is empty after filtering', async () => {
    pagedFetch()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('opportunities')).toBeInTheDocument())
    const select = screen.getByTestId('filter-model') as HTMLSelectElement
    select.value = 'calibrated'
    select.dispatchEvent(new Event('change', { bubbles: true }))

    await waitFor(() => expect(screen.getByTestId('opportunities-empty')).toBeInTheDocument())
    expect(screen.getByTestId('opportunities-empty')).toHaveTextContent('没有符合条件')
  })
})
