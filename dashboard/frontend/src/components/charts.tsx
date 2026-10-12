/**
 * Chart components. Recharts for anything plotted against axes; plain CSS for
 * the one proportion bar (a stack of widths needs no charting library).
 *
 * Palette discipline: series colours are fixed here once so every chart reads
 * as one system - token colours for ACCEPT/REJECT/NO_TRADE, a restrained
 * categorical set for multi-model comparison.
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

export const SERIES_COLORS = ['#239B78', '#5B8DEF', '#D6A044', '#8B9AB8', '#D66C6C']
export const ACCEPT_COLOR = '#239B78'
export const REJECT_COLOR = '#D6A044'
export const NO_TRADE_COLOR = '#9AA4B2'

const AXIS_STYLE = { fill: '#6B7280', fontSize: 11 } as const
const GRID_COLOR = '#E7EAF0'

export function DistributionBar({
  accept,
  reject,
  noTrade,
  other = 0,
  testId,
}: {
  accept: number
  reject: number
  noTrade: number
  other?: number
  testId?: string
}) {
  const total = accept + reject + noTrade + other
  const seg = (value: number) => (total > 0 ? (value / total) * 100 : 0)
  if (total === 0) {
    return <div className="h-2.5 w-full rounded-full bg-neutral-soft" data-testid={testId} />
  }
  return (
    <div
      className="flex h-2.5 w-full overflow-hidden rounded-full bg-neutral-soft"
      data-testid={testId ?? 'distribution-bar'}
      role="img"
      aria-label={`决策分布：ACCEPT ${accept}，REJECT ${reject}，NO_TRADE ${noTrade}`}
    >
      <div style={{ width: `${seg(accept)}%`, background: ACCEPT_COLOR }} />
      <div style={{ width: `${seg(reject)}%`, background: REJECT_COLOR }} />
      <div style={{ width: `${seg(noTrade)}%`, background: NO_TRADE_COLOR }} />
      <div style={{ width: `${seg(other)}%`, background: '#D66C6C' }} />
    </div>
  )
}

export interface SeriesPoint {
  label: string
  value: number
}

export interface Series {
  name: string
  color: string
  points: SeriesPoint[]
}

/** Grouped column chart: X = temperature buckets, Y = probability (%). */
export function ProbabilityBars({
  series,
  height = 260,
  testId,
}: {
  series: Series[]
  height?: number
  testId?: string
}) {
  const labels = series[0]?.points.map((p) => p.label) ?? []
  const merged = labels.map((label, i) => {
    const row: Record<string, string | number> = { label }
    for (const s of series) row[s.name] = (s.points[i]?.value ?? 0) * 100
    return row
  })
  if (merged.length === 0) return null
  return (
    <div data-testid={testId ?? 'probability-bars'} className="w-full">
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={merged} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid stroke={GRID_COLOR} vertical={false} />
            <XAxis dataKey="label" tick={AXIS_STYLE} tickLine={false} axisLine={{ stroke: GRID_COLOR }} />
            <YAxis
              tick={AXIS_STYLE}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: number) => `${Math.round(v)}%`}
              width={44}
            />
            <Tooltip
              formatter={(value: any, name: any) => [
                `${Number(value ?? 0).toFixed(1)}%`,
                String(name),
              ]}
              cursor={{ fill: 'rgba(23,33,43,0.04)' }}
              contentStyle={{ border: '1px solid #E7EAF0', borderRadius: 10, fontSize: 12 }}
            />
            {series.map((s) => (
              <Bar key={s.name} dataKey={s.name} fill={s.color} radius={[3, 3, 0, 0]} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        {series.map((s) => (
          <span key={s.name} className="flex items-center gap-1.5 text-xs text-muted">
            <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />
            {s.name}
          </span>
        ))}
      </div>
    </div>
  )
}

/** Model probability vs market bid/ask, one group per market. */
export function ModelVsMarketBars({
  rows,
  height = 240,
  testId,
}: {
  rows: { label: string; model: number; bid: number | null; ask: number | null }[]
  height?: number
  testId?: string
}) {
  const data = rows.map((r) => ({
    label: r.label,
    模型概率: r.model * 100,
    Bid: r.bid === null ? null : r.bid * 100,
    Ask: r.ask === null ? null : r.ask * 100,
  }))
  if (data.length === 0) return null
  return (
    <div data-testid={testId ?? 'model-vs-market'} className="w-full">
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid stroke={GRID_COLOR} vertical={false} />
            <XAxis dataKey="label" tick={AXIS_STYLE} tickLine={false} axisLine={{ stroke: GRID_COLOR }} />
            <YAxis
              tick={AXIS_STYLE}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: number) => `${Math.round(v)}%`}
              width={44}
            />
            <Tooltip
              formatter={(value: any, name: any) => [
                `${Number(value ?? 0).toFixed(1)}%`,
                String(name),
              ]}
              cursor={{ fill: 'rgba(23,33,43,0.04)' }}
              contentStyle={{ border: '1px solid #E7EAF0', borderRadius: 10, fontSize: 12 }}
            />
            <Bar dataKey="模型概率" fill={ACCEPT_COLOR} radius={[3, 3, 0, 0]} />
            <Bar dataKey="Bid" fill="#5B8DEF" radius={[3, 3, 0, 0]} />
            <Bar dataKey="Ask" fill="#8B9AB8" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: ACCEPT_COLOR }} />
          模型概率
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: '#5B8DEF' }} />
          市场 Bid
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: '#8B9AB8' }} />
          市场 Ask
        </span>
        <span className="text-muted/80">报价为盘口快照，非可执行价格</span>
      </div>
    </div>
  )
}

/** Compact trend line for one observed counter. */
export function Sparkline({
  points,
  color = '#239B78',
  height = 56,
  testId,
}: {
  points: { t: string; v: number }[]
  color?: string
  height?: number
  testId?: string
}) {
  if (points.length < 2) {
    return (
      <div
        className="flex items-center justify-center rounded-tile bg-canvas text-xs text-muted"
        style={{ height }}
        data-testid={testId ?? 'sparkline'}
      >
        暂无趋势（观测中）
      </div>
    )
  }
  return (
    <div style={{ height }} data-testid={testId ?? 'sparkline'}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 6, right: 4, left: 4, bottom: 0 }}>
          <Line
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.8}
            dot={false}
            isAnimationActive={false}
          />
          <Tooltip
            formatter={(value: any) => [String(value ?? 0), '']}
            labelFormatter={(_label: any, payload: any) => {
              const t = payload?.[0]?.payload?.t
              return t ? new Date(t).toLocaleTimeString('zh-CN', { hour12: false }) : ''
            }}
            contentStyle={{ border: '1px solid #E7EAF0', borderRadius: 10, fontSize: 12 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
