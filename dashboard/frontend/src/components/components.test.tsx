import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DistributionBar, ProbabilityBars } from './charts'
import { DecisionBadge, FreshnessPill, ModeBadge, StatusChip } from './status'
import { EmptyState, StatCard } from './ui'

describe('StatusChip states', () => {
  it('renders HEALTHY with the accent state', () => {
    const { container } = render(<StatusChip value="healthy" testId="chip" />)
    const chip = container.querySelector('[data-testid="chip"]')!
    expect(chip).toHaveAttribute('data-state', 'healthy')
    expect(chip).toHaveTextContent('HEALTHY')
  })

  it('renders unhealthy as danger', () => {
    const { container } = render(<StatusChip value="unhealthy" testId="chip" />)
    expect(container.querySelector('[data-testid="chip"]')).toHaveAttribute(
      'data-state',
      'unhealthy',
    )
  })

  it('falls back to UNKNOWN for missing values', () => {
    const { container } = render(<StatusChip value={null} testId="chip" />)
    expect(container.querySelector('[data-testid="chip"]')).toHaveTextContent('UNKNOWN')
  })
})

describe('DecisionBadge / ModeBadge', () => {
  it('colours ACCEPT as accent and REJECT as warn', () => {
    const { container } = render(
      <>
        <DecisionBadge state="ACCEPT" />
        <DecisionBadge state="REJECT" />
        <DecisionBadge state="NO_TRADE" />
      </>,
    )
    expect(container.querySelector('[data-testid="decision-badge-ACCEPT"]')).toHaveClass(
      'text-accent',
    )
    expect(container.querySelector('[data-testid="decision-badge-REJECT"]')).toHaveClass(
      'text-warn',
    )
    expect(container.querySelector('[data-testid="decision-badge-NO_TRADE"]')).toHaveClass(
      'text-ink',
    )
  })

  it('marks REPLAY distinctly from LIVE', () => {
    const { container } = render(<ModeBadge mode="REPLAY" />)
    expect(container.querySelector('[data-testid="mode-badge"]')).toHaveTextContent('REPLAY')
  })
})

describe('FreshnessPill', () => {
  it('shows fresh as updated-N-seconds-ago', () => {
    const { container } = render(
      <FreshnessPill
        freshness={{ state: 'fresh', age_seconds: 5, as_of_utc: new Date(Date.now() - 5000).toISOString() }}
        now={Date.now()}
        testId="pill"
      />,
    )
    expect(container.querySelector('[data-testid="pill"]')).toHaveAttribute('data-state', 'fresh')
    expect(container.querySelector('[data-testid="pill"]')).toHaveTextContent('更新于')
  })

  it('flags stale data explicitly', () => {
    const { container } = render(
      <FreshnessPill freshness={{ state: 'stale', age_seconds: 900 }} now={Date.now()} testId="pill" />,
    )
    expect(container.querySelector('[data-testid="pill"]')).toHaveTextContent('STALE')
  })

  it('shows missing / unavailable honestly', () => {
    const { container, rerender } = render(
      <FreshnessPill freshness={{ state: 'missing' }} testId="pill" />,
    )
    expect(container.querySelector('[data-testid="pill"]')).toHaveTextContent('数据缺失')
    rerender(<FreshnessPill freshness={{ state: 'unavailable' }} testId="pill" />)
    expect(container.querySelector('[data-testid="pill"]')).toHaveTextContent('暂未接入')
  })
})

describe('DistributionBar', () => {
  it('renders proportional segments', () => {
    const { container } = render(
      <DistributionBar accept={3} reject={30} noTrade={9} testId="dist" />,
    )
    const bar = container.querySelector('[data-testid="dist"]')!
    const segments = Array.from(bar.children) as HTMLElement[]
    expect(segments).toHaveLength(4)
    // 3/42, 30/42, 9/42, 0/42
    expect(parseFloat(segments[0].style.width)).toBeCloseTo((3 / 42) * 100, 1)
    expect(parseFloat(segments[1].style.width)).toBeCloseTo((30 / 42) * 100, 1)
  })

  it('renders a neutral track when everything is zero', () => {
    const { container } = render(
      <DistributionBar accept={0} reject={0} noTrade={0} testId="dist" />,
    )
    const bar = container.querySelector('[data-testid="dist"]')!
    expect(bar.children).toHaveLength(0)
  })
})

describe('ProbabilityBars', () => {
  it('renders the chart container and its legend without crashing', () => {
    render(
      <ProbabilityBars
        series={[
          {
            name: 'calibrated',
            color: '#239B78',
            points: [
              { label: '80°F', value: 0.4 },
              { label: '81°F', value: 0.6 },
            ],
          },
        ]}
        testId="prob"
      />,
    )
    expect(screen.getByTestId('prob')).toBeInTheDocument()
    expect(screen.getByText('calibrated')).toBeInTheDocument()
  })
})

describe('StatCard / EmptyState', () => {
  it('shows N/A-style dash for missing values and a reason in EmptyState', () => {
    render(
      <StatCard label="活跃市场" value="—" sub="暂无数据" testId="stat" />,
    )
    expect(screen.getByTestId('stat')).toHaveTextContent('活跃市场')
    render(<EmptyState title="暂未接入" reason="等待 producer" />)
    expect(screen.getByText('暂未接入')).toBeInTheDocument()
    expect(screen.getByText('等待 producer')).toBeInTheDocument()
  })
})
