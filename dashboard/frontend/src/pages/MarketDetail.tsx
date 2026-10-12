/** Market detail: quotes, depth, model view, and recent shadow decisions. */
import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { usePoll } from '../api/client'
import { ProbabilityBars, type Series } from '../components/charts'
import { DecisionBadge, FreshnessPill, ModeBadge, StatusChip } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Banner, Card, EmptyState, Field, SectionTitle, StatCard } from '../components/ui'
import {
  daysUntil,
  fmtAge,
  fmtDateTime,
  fmtProb,
  fmtUsd,
  fmtUsdDepth,
} from '../format'
import { useNow } from '../useNow'
import type { ForecastsData, MarketDetailData } from '../types'

export default function MarketDetail() {
  const { marketId = '' } = useParams()
  const now = useNow(5000)
  const detail = usePoll<MarketDetailData>(`/api/v1/markets/${encodeURIComponent(marketId)}`, 10000)
  const forecasts = usePoll<ForecastsData>('/api/v1/forecasts', 30000)

  const market = detail.envelope?.data?.market
  const depth = detail.envelope?.data?.depth
  const recentDecisions = detail.envelope?.data?.recent_decisions ?? []
  const recentStatus = detail.envelope?.data?.recent_decisions_status
  const recentReason = detail.envelope?.data?.recent_decisions_reason
  const noRecentRows = recentStatus === 'unavailable' || recentDecisions.length === 0

  const distributions =
    forecasts.envelope?.data?.distributions?.filter(
      (d) => market?.target_date && d.target_date === market.target_date,
    ) ?? []
  const modelNames = [...new Set(distributions.map((d) => d.model))]

  const series: Series[] = modelNames.map((model, i) => {
    const dist = distributions.find((d) => d.model === model)!
    return {
      name: model,
      color: ['#239B78', '#5B8DEF', '#D6A044', '#8B9AB8'][i % 4],
      points: dist.buckets.map((b) => ({ label: b.label, value: b.probability })),
    }
  })

  if (!detail.envelope && detail.error) {
    return (
      <Card>
        <EmptyState title="无法加载市场详情" reason={detail.error} testId="detail-error" />
      </Card>
    )
  }

  if (detail.envelope && !market) {
    return (
      <>
        <PageHeader title="市场详情" english="Market" envelope={detail.envelope} />
        <Card>
          <EmptyState
            title="未找到该市场"
            reason={detail.envelope.data.reason}
            testId="detail-not-found"
          />
          <div className="text-center">
            <Link to="/markets" className="text-sm text-accent hover:underline">
              ← 返回市场列表
            </Link>
          </div>
        </Card>
      </>
    )
  }

  const book = market?.book

  return (
    <>
      <PageHeader
        title="市场详情"
        english="Market"
        subtitle={market?.question}
        envelope={detail.envelope}
        right={
          market && (
            <StatusChip
              value={market.closed ? 'stopped' : market.tradeable ? 'live' : 'unknown'}
              label={market.closed ? 'CLOSED' : market.tradeable ? 'ACTIVE' : 'INACTIVE'}
            />
          )
        }
      />

      <Link
        to="/markets"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted hover:text-accent"
      >
        <ArrowLeft size={14} /> 返回列表
      </Link>

      {market && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard label="目标日期" value={market.target_date ?? '—'} sub={daysUntil(market.target_date) ?? undefined} />
            <StatCard label="温度区间" value={market.bracket_label || '—'} sub="bracket" />
            <StatCard
              label="YES Bid"
              value={book?.available ? fmtUsd(book.best_bid) : '—'}
              sub={book?.available ? `深度 ${fmtUsdDepth(book.bid_depth_usd)}` : book?.reason ?? '无盘口'}
            />
            <StatCard
              label="YES Ask"
              value={book?.available ? fmtUsd(book.best_ask) : '—'}
              sub={book?.available ? `深度 ${fmtUsdDepth(book.ask_depth_usd)}` : book?.reason ?? '无盘口'}
            />
            <StatCard
              label="Spread"
              value={book?.available ? fmtUsd(book.spread) : '—'}
              sub="盘口快照 · 非可执行价格"
              tone={book?.stale ? 'warn' : 'neutral'}
            />
            <StatCard
              label="模型概率"
              value={
                market.model_probability !== null && market.model_probability !== undefined
                  ? fmtProb(market.model_probability)
                  : 'N/A'
              }
              tone="accent"
              sub={
                market.model_source === 'market-snapshot'
                  ? 'shadow market-snapshot'
                  : '暂未接入 producer'
              }
            />
          </div>

          <Card title="盘口 / Order Book" hint="最近一次归档快照的 L2 报价">
            {book?.available ? (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <FreshnessPill
                    freshness={{
                      state: book.stale ? 'stale' : 'fresh',
                      age_seconds: book.age_seconds,
                      as_of_utc: book.received_at_utc,
                    }}
                    now={now}
                  />
                  <span className="tnum text-xs text-muted">
                    levels: {book.levels_bid} bid / {book.levels_ask} ask
                  </span>
                  {book.venue_ts_utc && (
                    <span className="tnum text-xs text-muted">
                      venue 时间 {fmtDateTime(book.venue_ts_utc)}
                    </span>
                  )}
                  <span className="text-xs text-muted">来源 {book.source}</span>
                </div>
                {book.defects.length > 0 && (
                  <Banner tone="warn" title={`盘口缺陷：${book.defects.join(', ')}`}>
                    {book.reason}
                  </Banner>
                )}
                <div className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-3">
                  <Field label="Token (YES)">
                    <span className="break-all text-xs">{market.token_yes ?? '—'}</span>
                  </Field>
                  <Field label="市场 ID">
                    <span className="break-all text-xs">{market.market_id}</span>
                  </Field>
                  <Field label="Station">{market.station ?? '—'}</Field>
                </div>
                {depth && (
                  <p className="text-xs text-muted">{String(depth.note ?? '')}</p>
                )}
              </div>
            ) : (
              <EmptyState
                compact
                title="订单簿缺失或过期"
                reason={book?.reason ?? '归档尾部中没有该 token 的盘口。'}
                testId="detail-book-empty"
              />
            )}
          </Card>

          <Card
            title="模型概率分布 · Temperature Probability"
            hint="同一目标日、各模型的温度桶概率（来自 forecast-snapshot）"
          >
            {series.length > 0 ? (
              <ProbabilityBars series={series} testId="detail-probability-chart" />
            ) : (
              <EmptyState
                compact
                title="概率分布暂未接入"
                reason={
                  forecasts.envelope?.data?.distributions_reason ??
                  'Shadow 尚未发布 forecast-snapshot；接入后此处按温度桶展示各模型概率。'
                }
                testId="detail-distribution-empty"
              />
            )}
            {market.model_probability !== null && market.model_probability !== undefined && (
              <div className="mt-3 rounded-tile bg-accent-soft px-4 py-3 text-sm">
                本区间（{market.bracket_label}）当前模型概率{' '}
                <span className="tnum font-semibold text-accent">
                  {fmtProb(market.model_probability)}
                </span>
                {market.net_edge_usd !== null && market.net_edge_usd !== undefined && (
                  <>
                    {' '}
                    · 理论净 Edge{' '}
                    <span className="tnum font-semibold text-accent">
                      {market.net_edge_usd >= 0 ? '+' : ''}
                      {fmtUsd(market.net_edge_usd)} USD/股
                    </span>
                  </>
                )}
              </div>
            )}
          </Card>

          <Card
            title="最近 Shadow 决策"
            hint="ACCEPT 表示符合当前模型与风险过滤条件，不代表已验证的盈利机会"
          >
            {noRecentRows ? (
              <EmptyState
                compact
                title={recentDecisions.length === 0 ? '该市场暂无记录的决策' : '决策明细暂未接入'}
                reason={recentReason}
                testId="detail-decisions-empty"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-sm">
                  <thead>
                    <tr className="border-b border-line text-left text-xs text-muted">
                      <th className="pb-2 font-medium">时间</th>
                      <th className="pb-2 font-medium">方向</th>
                      <th className="pb-2 font-medium">状态</th>
                      <th className="pb-2 font-medium">模式</th>
                      <th className="pb-2 text-right font-medium">概率</th>
                      <th className="pb-2 text-right font-medium">Edge</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentDecisions.slice(0, 20).map((row: any) => (
                      <tr key={row.decision_id} className="border-b border-line/60 last:border-0">
                        <td className="tnum py-2">{fmtDateTime(row.decided_at_utc)}</td>
                        <td className="py-2 text-xs uppercase">{row.direction ?? '—'}</td>
                        <td className="py-2">
                          <DecisionBadge state={row.state} />
                        </td>
                        <td className="py-2">
                          <ModeBadge mode={row.mode} />
                        </td>
                        <td className="tnum py-2 text-right">{fmtProb(row.model_probability)}</td>
                        <td className="tnum py-2 text-right">
                          {row.net_edge_usd == null
                            ? '—'
                            : `${row.net_edge_usd >= 0 ? '+' : ''}${fmtUsd(row.net_edge_usd)}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <SectionTitle hint={`数据更新 ${fmtAge(detail.envelope?.freshness.age_seconds)}`}>
            元数据
          </SectionTitle>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <StatCard label="Event" value={market.event_id ?? '—'} sub="gamma event id" />
            <StatCard
              label="状态"
              value={
                market.active ? (market.accepting_orders && !market.closed ? 'OPEN' : 'PAUSED') : 'INACTIVE'
              }
              sub={market.closed ? 'closed' : 'active / acceptingOrders'}
            />
            <StatCard label="最近更新" value={fmtDateTime(market.updated_at_utc)} sub="book received" />
          </div>
        </div>
      )}
    </>
  )
}
