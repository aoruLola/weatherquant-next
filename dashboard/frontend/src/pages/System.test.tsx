import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { collectorHealth, shadowHealth, shadowReport } from '../test/fixtures'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import type { SystemData } from '../types'
import System from './System'

afterEach(() => vi.unstubAllGlobals())

function systemFixture(overrides: Partial<SystemData> = {}): SystemData {
  return {
    collector: collectorHealth,
    collector_metrics: {
      present: true,
      counters: collectorHealth.stats,
      trends: {},
      trend_window_seconds: null,
      updated_at_utc: null,
      reason: null,
    },
    shadow: shadowHealth,
    report: shadowReport,
    trends: {},
    storage: {
      collector_db_bytes: collectorHealth.db_bytes ?? null,
      collector_raw_bytes: collectorHealth.raw_bytes ?? null,
      disk_free_percent: collectorHealth.disk_free_percent ?? null,
      disk_total_bytes: collectorHealth.disk_total_bytes ?? null,
      shadow_db_bytes: null,
    },
    notes: [
      'Shadow 数据库大小：Shadow 心跳未包含该字段（暂未接入）',
      '趋势线仅覆盖本 Dashboard 进程启动后的观测窗口，不是历史全量',
    ],
    unavailable: [],
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <System />
    </MemoryRouter>,
  )
}

describe('System page', () => {
  it('shows the WS queue numbers the new collector publishes', async () => {
    mockFetch({ '/api/v1/system': envelopeOf(systemFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByText('WS 队列深度')).toBeInTheDocument())
    expect(screen.getByText('2 / 512')).toBeInTheDocument()
    expect(screen.getByText('WS 队列高水位')).toBeInTheDocument()
    expect(screen.getByText('11')).toBeInTheDocument()
    expect(screen.getByText('WS 丢帧')).toBeInTheDocument()
    expect(screen.getByText('REST sweep 延迟')).toBeInTheDocument()
    expect(screen.getByText('42 ms')).toBeInTheDocument()
    // the trend tile is sampling, not absent
    expect(screen.getByTestId('trend-WS 队列积压')).toHaveTextContent('采样中')
  })

  it('falls back to honest N/A when the heartbeat carries no telemetry', async () => {
    const legacy = systemFixture()
    legacy.collector = { ...collectorHealth, telemetry: {} }
    mockFetch({ '/api/v1/system': envelopeOf(legacy) })
    renderPage()
    await waitFor(() => expect(screen.getByText('WS messages')).toBeInTheDocument())
    expect(screen.queryByText('WS 队列深度')).not.toBeInTheDocument()
    expect(screen.getByTestId('trend-WS 队列积压')).toHaveTextContent(
      'Collector 心跳未包含队列字段',
    )
    expect(screen.getByText(/Shadow 数据库大小/)).toBeInTheDocument()
  })

  it('renders the collector/shadow sections with mode badges', async () => {
    mockFetch({ '/api/v1/system': envelopeOf(systemFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('sys-shadow-chip')).toBeInTheDocument())
    expect(screen.getByTestId('sys-shadow-chip')).toHaveTextContent('LIVE')
    expect(screen.getByText('Shadow')).toBeInTheDocument()
    expect(screen.getByTestId('sys-collector-chip')).toHaveAttribute('data-state', 'healthy')
  })

  it('surfaces REPLAY as REPLAY, never as LIVE', async () => {
    const replay = systemFixture()
    replay.shadow = { ...shadowHealth, mode: 'REPLAY' }
    mockFetch({ '/api/v1/system': envelopeOf(replay) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('sys-shadow-chip')).toBeInTheDocument())
    expect(screen.getByTestId('sys-shadow-chip')).toHaveTextContent('REPLAY')
    expect(screen.getByTestId('sys-shadow-chip')).toHaveAttribute('data-state', 'replay')
  })

  it('renders an error state when the API is unreachable', async () => {
    rejectFetch()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('system-error')).toBeInTheDocument())
  })
})
