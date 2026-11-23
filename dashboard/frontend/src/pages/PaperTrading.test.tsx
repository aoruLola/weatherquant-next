import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { envelopeOf, mockFetch } from '../test/mock'
import type { PaperFills, PaperSummary } from '../types'
import PaperTrading from './PaperTrading'

afterEach(() => vi.unstubAllGlobals())

function summary(overrides: Partial<PaperSummary> = {}): PaperSummary {
  return {
    status: 'ok',
    mode: 'PAPER',
    initial_capital_usdc: 100,
    available_usdc: 60,
    occupied_usdc: 40,
    cost_equity_usdc: 100,
    equity_usdc: 84,
    realized_pnl_usdc: 2.5,
    unrealized_pnl_usdc: -16,
    roi: -0.16,
    max_drawdown: 0.04,
    drawdown_basis: 'ledger_cost',
    equity_curve: {
      basis: 'ledger_cost',
      points: [
        { t: '2026-11-17T01:00:00Z', v: 100, seq: 1 },
        { t: '2026-11-17T02:00:00Z', v: 100, seq: 2 },
      ],
    },
    positions: [
      {
        market_id: 'market-1',
        token_id: 'token-yes',
        shares: 40,
        avg_cost_usd_per_share: 1,
        cost_basis_usdc: 40,
        mark_usd_per_share: 0.6,
        market_value_usdc: 24,
        unrealized_pnl_usdc: -16,
        mark_status: 'OK',
      },
    ],
    marks_status: 'fresh',
    rejection_summary: [{ reason: 'NOT_EXECUTABLE', count: 3 }],
    rejections: [
      {
        decision_id: 'rejection-1',
        market_id: 'market-1',
        refusal_reason: 'NOT_EXECUTABLE',
        refusal_detail: 'no executable evidence',
        decision_time_utc: '2026-11-17T02:00:00Z',
      },
    ],
    ledger_consistent: true,
    conservation_ok: true,
    fill_count: 1,
    ...overrides,
  }
}

function unavailableSummary(): PaperSummary {
  return {
    status: 'unavailable',
    mode: 'PAPER',
    reason: 'Paper is offline',
    initial_capital_usdc: null,
    available_usdc: null,
    occupied_usdc: null,
    cost_equity_usdc: null,
    equity_usdc: null,
    realized_pnl_usdc: null,
    unrealized_pnl_usdc: null,
    roi: null,
    max_drawdown: null,
    drawdown_basis: 'ledger_cost',
    equity_curve: { basis: 'ledger_cost', points: [] },
    positions: [],
    marks_status: 'unavailable',
    marks_reason: 'Paper is offline',
    rejection_summary: [],
    rejections: [],
    ledger_consistent: null,
    conservation_ok: null,
    fill_count: null,
  }
}

function fills(decisionId: string, nextCursor: string | null = null): PaperFills {
  return {
    status: 'ok',
    mode: 'PAPER',
    fills: [
      {
        decision_id: decisionId,
        market_id: 'market-1',
        token_id: 'token-yes',
        direction: 'buy_yes',
        side: 'BUY',
        requested_shares: 10,
        filled_shares: 10,
        avg_price_usd_per_share: 0.55,
        fill_cost_usd: 5.5,
        fees_usd: 0.01,
        complete: true,
        filled_at_utc: '2026-11-17T02:00:00Z',
      },
    ],
    limit: 20,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <PaperTrading />
    </MemoryRouter>,
  )
}

describe('PaperTrading', () => {
  it('keeps the virtual simulation identity and N/A figures when unavailable', async () => {
    mockFetch({
      '/api/v1/paper/summary': envelopeOf(unavailableSummary()),
      '/api/v1/paper/fills?limit=20': envelopeOf({
        status: 'unavailable',
        mode: 'PAPER',
        reason: 'Paper is offline',
        fills: [],
        limit: 20,
        next_cursor: null,
        has_more: false,
      } satisfies PaperFills),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('paper-unavailable')).toBeInTheDocument())
    expect(screen.getByTestId('paper-disclaimer')).toHaveTextContent('Paper 模拟')
    expect(screen.getByTestId('paper-disclaimer')).toHaveTextContent('不代表真实交易或真实收益')
    expect(screen.getByTestId('paper-mode-badge')).toHaveTextContent('模拟 / PAPER')
    expect(screen.getByTestId('paper-initial')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-realized')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-equity')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-roi')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-drawdown')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-fills-empty')).toHaveTextContent('N/A')
  })

  it('shows N/A for every quote-dependent figure when a position has no mark', async () => {
    const missingMark = summary({
      equity_usdc: null,
      unrealized_pnl_usdc: null,
      roi: null,
      marks_status: 'unavailable',
      marks_reason: 'market snapshot missing',
      positions: [
        {
          ...summary().positions[0],
          mark_usd_per_share: null,
          market_value_usdc: null,
          unrealized_pnl_usdc: null,
          mark_status: 'MARK_UNAVAILABLE',
        },
      ],
    })
    mockFetch({
      '/api/v1/paper/summary': envelopeOf(missingMark),
      '/api/v1/paper/fills?limit=20': envelopeOf(fills('fill-1')),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('paper-positions-table')).toBeInTheDocument())
    expect(screen.getByTestId('paper-position-mark')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-position-pnl')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-equity')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-equity')).toHaveTextContent('成本口径 100.00 USDC')
    expect(screen.getByTestId('paper-unrealized')).toHaveTextContent('N/A')
    expect(screen.getByTestId('paper-roi')).toHaveTextContent('N/A')
    // A missing quote does not erase valid, quote-independent ledger figures.
    expect(screen.getByTestId('paper-realized')).toHaveTextContent('2.50 USDC')
    expect(screen.getByTestId('paper-drawdown')).toHaveTextContent('4.00%')
  })

  it('renders marked positions, account metrics and rejection reasons', async () => {
    mockFetch({
      '/api/v1/paper/summary': envelopeOf(summary()),
      '/api/v1/paper/fills?limit=20': envelopeOf(fills('fill-1')),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('paper-positions-table')).toBeInTheDocument())
    expect(screen.getByTestId('paper-initial')).toHaveTextContent('100.00 USDC')
    expect(screen.getByTestId('paper-equity')).toHaveTextContent('84.00 USDC')
    expect(screen.getByTestId('paper-position-pnl')).toHaveTextContent('-16.00 USDC')
    expect(screen.getByTestId('paper-roi')).toHaveTextContent('-16.00%')
    expect(screen.getByTestId('paper-rejection-summary')).toHaveTextContent('NOT_EXECUTABLE')
    expect(screen.getByTestId('paper-rejection-summary')).toHaveTextContent('3')
    expect(screen.getByTestId('paper-rejections')).toHaveTextContent('no executable evidence')
  })

  it('pages fills forward and back with the upstream keyset cursor', async () => {
    mockFetch({
      '/api/v1/paper/summary': envelopeOf(summary()),
      '/api/v1/paper/fills?limit=20': envelopeOf(fills('fill-1', 'cursor-1')),
      '/api/v1/paper/fills?limit=20&cursor=cursor-1': envelopeOf(fills('fill-2')),
    })
    renderPage()

    await waitFor(() => expect(screen.getByTestId('paper-fill-fill-1')).toBeInTheDocument())
    expect(screen.getByTestId('paper-fills-prev')).toBeDisabled()
    fireEvent.click(screen.getByTestId('paper-fills-next'))

    await waitFor(() => expect(screen.getByTestId('paper-fill-fill-2')).toBeInTheDocument())
    expect(screen.queryByTestId('paper-fill-fill-1')).not.toBeInTheDocument()
    expect(screen.getByTestId('paper-fills-prev')).toBeEnabled()
    expect(screen.getByTestId('paper-fills-next')).toBeDisabled()

    fireEvent.click(screen.getByTestId('paper-fills-prev'))
    await waitFor(() => expect(screen.getByTestId('paper-fill-fill-1')).toBeInTheDocument())
    expect(screen.getByTestId('paper-fills-prev')).toBeDisabled()
  })
})
