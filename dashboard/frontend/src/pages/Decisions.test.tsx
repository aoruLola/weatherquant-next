import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { decisionsFixture } from '../test/fixtures'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import Decisions, { groupConsecutiveAccepts } from './Decisions'
import type { DecisionRow } from '../types'

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <MemoryRouter>
      <Decisions />
    </MemoryRouter>,
  )
}

describe('groupConsecutiveAccepts', () => {
  const row = (id: string, state: string, market = 'm-81', direction = 'buy_yes'): DecisionRow => ({
    decision_id: id,
    market_id: market,
    state,
    direction,
    reasons: [],
  })

  it('collapses repeated same-market ACCEPTs into one group', () => {
    const rows = [
      row('1', 'ACCEPT'),
      row('2', 'ACCEPT'),
      row('3', 'ACCEPT'),
      row('4', 'REJECT'),
    ]
    const grouped = groupConsecutiveAccepts(rows)
    expect(grouped).toHaveLength(2)
    expect(grouped[0]).toMatchObject({ kind: 'group', count: 3 })
    expect(grouped[1]).toMatchObject({ kind: 'row', count: 1 })
  })

  it('never groups across different markets or directions', () => {
    const rows = [
      row('1', 'ACCEPT', 'm-81', 'buy_yes'),
      row('2', 'ACCEPT', 'm-80', 'buy_yes'),
      row('3', 'ACCEPT', 'm-81', 'sell_yes'),
    ]
    const grouped = groupConsecutiveAccepts(rows)
    expect(grouped).toHaveLength(3)
    expect(grouped.every((g) => g.kind === 'row')).toBe(true)
  })

  it('does not group non-ACCEPT states', () => {
    const rows = [row('1', 'REJECT'), row('2', 'REJECT')]
    expect(groupConsecutiveAccepts(rows).every((g) => g.kind === 'row')).toBe(true)
  })
})

describe('Decisions page', () => {
  it('keeps the shadow-signal disclaimer on screen no matter what', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('accept-disclaimer')).toBeInTheDocument())
    expect(screen.getByTestId('accept-disclaimer')).toHaveTextContent('影子信号')
    expect(screen.getByTestId('accept-disclaimer')).toHaveTextContent('不代表下单')
    expect(screen.getByTestId('accept-disclaimer')).toHaveTextContent('REPLAY')
    // The old ACCEPT-as-trade wording must be gone from the banner.
    expect(screen.getByTestId('accept-disclaimer')).not.toHaveTextContent(
      /^ACCEPT 表示$/,
    )
  })

  it('shows the disclaimer even when row detail is unavailable', async () => {
    mockFetch({
      '/api/v1/decisions': envelopeOf(
        decisionsFixture({ rows: [], rows_status: 'unavailable', rows_reason: 'producer awaits integration' }),
      ),
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('rows-unavailable')).toBeInTheDocument())
    expect(screen.getByTestId('accept-disclaimer')).toBeInTheDocument()
    expect(screen.getByTestId('rows-unavailable')).toHaveTextContent('producer awaits integration')
    // aggregates still shown beside the unavailable list
    expect(screen.getByTestId('stat-accept')).toHaveTextContent('3')
  })

  it('renders repeated ACCEPTs as one grouped observation', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())
    const table = screen.getByTestId('decisions-table')
    // fixture: 3 consecutive ACCEPTs on m-81 + 1 REJECT -> 2 display rows
    expect(within(table).getAllByTestId('decision-row')).toHaveLength(2)
    expect(within(table).getByTestId('accept-group-badge')).toHaveTextContent('连续 ×3')
    expect(within(table).getByTestId('accept-group-badge').getAttribute('title')).toContain(
      '不是 N 个独立机会',
    )
  })

  it('splits LIVE from REPLAY in the totals cards', async () => {
    const replay = decisionsFixture()
    replay.totals = {
      ...replay.totals!,
      live_accepted: 1,
      replay_accepted: 2,
    }
    mockFetch({ '/api/v1/decisions': envelopeOf(replay) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('stat-accept')).toBeInTheDocument())
    expect(screen.getByTestId('stat-accept')).toHaveTextContent('LIVE 1')
    expect(screen.getByTestId('stat-accept')).toHaveTextContent('REPLAY 2')
  })

  it('separates repeated ACCEPT cycles from independent markets', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('accept-scope')).toBeInTheDocument())
    // 3 observations of 1 market: the number that must not be read as 3 chances.
    expect(screen.getByTestId('stat-accept-observations')).toHaveTextContent('3')
    expect(screen.getByTestId('stat-accept-markets')).toHaveTextContent('1')
    expect(screen.getByTestId('stat-accept-tokens')).toHaveTextContent('1')
    expect(screen.getByTestId('stat-accept-repeats')).toHaveTextContent('2')
  })

  it('states the statistics span the database, not the process', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('totals-scope')).toBeInTheDocument())
    const scope = screen.getByTestId('totals-scope')
    // The failure this guards: a restart zeroes the heartbeat's `cycles`, and
    // reading that as these totals falling means interpreting a reboot as
    // opportunities disappearing.
    expect(scope).toHaveTextContent('restart does not reset it')
    expect(scope).toHaveTextContent('已记录 2 次运行')
    expect(scope).toHaveTextContent('Shadow 重启不会清零这些计数')
  })

  it('says the independence dimensions are unavailable rather than guessing', async () => {
    const fixture = decisionsFixture()
    const totals: Record<string, any> = { ...fixture.totals }
    delete totals.accept_scope
    mockFetch({ '/api/v1/decisions': envelopeOf({ ...fixture, totals }) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('accept-scope-unavailable')).toBeInTheDocument())
    expect(screen.getByTestId('accept-scope-reason')).toHaveTextContent('不可用')
    expect(screen.queryByTestId('stat-accept-markets')).not.toBeInTheDocument()
  })

  it('shows model, version and decision-time evidence without inventing values', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())
    const table = screen.getByTestId('decisions-table')
    expect(table).toHaveTextContent('calibrated / v1')
    expect(table).toHaveTextContent('预测 run')
    expect(table).toHaveTextContent('盘口 venue')
    expect(table).toHaveTextContent('shadow_decisions')
  })

  it('labels ACCEPT as a shadow signal, not an executable instruction', async () => {
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())
    const badge = screen.getAllByTestId('decision-badge-ACCEPT')[0]
    expect(badge).toHaveTextContent('影子信号')
    expect(badge).not.toHaveTextContent(/^ACCEPT$/)
    // the filter value stays compatible with the API even though the label moved
    expect(screen.getByTestId('filter-state')).toHaveValue('')
    expect(within(screen.getByTestId('filter-state')).getByText('影子信号（ACCEPT）')).toBeInTheDocument()
  })

  it('calls the price column what it is, not what a trader would hope', async () => {
    // A snapshot of the book is an evaluation input, never a promise that the
    // price is available at that level. The header said "executable price",
    // which is exactly the claim the rest of the page spends its time denying.
    mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())
    expect(screen.getByText('评估价格（盘口快照）')).toBeInTheDocument()
    expect(screen.queryByText('可执行价格')).not.toBeInTheDocument()
  })

  it('sends filter changes to the API', async () => {
    const fetchMock = mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('filter-state'), { target: { value: 'ACCEPT' } })
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('state=ACCEPT'))).toBe(true)
    })

    fireEvent.change(screen.getByTestId('filter-mode'), { target: { value: 'LIVE' } })
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('mode=LIVE'))).toBe(true)
    })
  })

  it('sends the time window as a since instant', async () => {
    const fetchMock = mockFetch({ '/api/v1/decisions': envelopeOf(decisionsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-table')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('filter-window'), { target: { value: '1' } })
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]))
      const withSince = urls.find((u) => u.includes('since='))
      expect(withSince).toBeDefined()
      // roughly one hour back, encoded
      const raw = decodeURIComponent(withSince!.split('since=')[1].split('&')[0])
      const delta = Date.now() - new Date(raw).getTime()
      expect(delta).toBeGreaterThan(3600_000 - 60_000)
      expect(delta).toBeLessThan(3600_000 + 60_000)
    })
  })

  it('shows an error state when the API is down', async () => {
    rejectFetch()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('decisions-error')).toBeInTheDocument())
    // the disclaimer survives the outage as well
    expect(screen.getByTestId('accept-disclaimer')).toBeInTheDocument()
  })

  it('warns loudly when the aggregate totals are missing', async () => {
    mockFetch({
      '/api/v1/decisions': envelopeOf(
        decisionsFixture({ totals: null, totals_status: 'missing' }),
      ),
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('totals-missing')).toBeInTheDocument())
  })
})

describe('Decision scope API contract', () => {
  it('displays direction distinctness when the report supplies it', async () => {
    const fixture = decisionsFixture()
    fixture.totals = {
      ...fixture.totals!,
      accept_scope: {
        ...(fixture.totals?.accept_scope ?? {}),
        accepted_distinct_market_directions: 2,
      },
    }
    mockFetch({ '/api/v1/decisions': envelopeOf(fixture) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('stat-accept-directions')).toBeInTheDocument())
    expect(screen.getByTestId('stat-accept-directions')).toHaveTextContent('2')
  })
})
