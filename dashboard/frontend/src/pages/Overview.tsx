/**
 * Overview: the five-second answer - is the system up, is the data fresh, is
 * the model running, did anything new happen?
 *
 * Every number here comes from the API's own payload; when a producer is
 * absent the card shows its reason instead of a zero.
 */
import { AlertTriangle, ArrowRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import { usePoll } from '../api/client'
import { DistributionBar, ModelVsMarketBars } from '../components/charts'
import { DecisionBadge, ModeBadge, StatusChip } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Banner, Card, EmptyState, SectionTitle, StatCard } from '../components/ui'
import { fmtAge, fmtAgeShort, fmtBytes, fmtInt, fmtProb, fmtUsd } from '../format'
import type { OverviewData } from '../types'

function ApiError({ error, refresh }: { error: string; refresh: () => void }) {
  return (
    <Card>
      <EmptyState
        icon={<AlertTriangle size={18} />}
        title="无法获取 Dashboard 数据"
        reason={`${error}。后端可能正在重启，页面会自动重试。`}
        testId="overview-error"
      />
      <div className="mt-2 text-center">
        <button
          type="button"
          onClick={refresh}
          className="rounded-tile border border-line px-3 py-1.5 text-xs text-muted hover:border-accent hover:text-accent"
        >
          立即重试
        </button>
      </div>
    </Card>
  )
}

export default function Overview() {
  const { envelope, error, refresh } = usePoll<OverviewData>('/api/v1/overview', 5000)
  const data = envelope?.data

  const collector = data?.collector
  const shadow = data?.shadow
  const totals = data?.decision_totals ?? null

  const accept = Number(totals?.accepted ?? 0)
  const reject = Number(totals?.rejected ?? 0)
  const noTrade = Number(totals?.no_trade ?? 0)
  const dataUnavailable = Number(totals?.data_unavailable ?? 0)
  const evaluated = Number(totals?.evaluated ?? 0)

  const bookStale = (data?.latest_book_age_seconds ?? 0) > 900
  const sys = data?.system_health ?? {}

  return (
    <>
      <PageHeader
        title="总览"
        english="Overview"
        subtitle="Polymarket 天气市场 · KLGA 每日最高气温"
        envelope={envelope}
        right={
          <>
            <StatusChip
              value={
                collector?.present === false
                  ? 'missing'
                  : (collector?.health_status ?? 'unknown')
              }
              label={`Collector ${collector?.present === false ? 'MISSING' : (collector?.health_status ?? 'UNKNOWN').toUpperCase()}`}
              title={collector?.health_reasons?.join('; ') ?? collector?.reason ?? undefined}
              testId="chip-collector"
            />
            <StatusChip
              value={
                shadow?.present === false
                  ? 'missing'
                  : (shadow?.mode ?? 'unknown') === 'REPLAY'
                    ? 'replay'
                    : (shadow?.mode ?? 'unknown') === 'LIVE'
                      ? 'live'
                      : (shadow?.health_status ?? 'unknown')
              }
              label={
                shadow?.present === false
                  ? 'Shadow MISSING'
                  : `Shadow ${(shadow?.mode ?? 'UNKNOWN').toUpperCase()}`
              }
              title={shadow?.health_reasons?.join('; ')}
              testId="chip-shadow"
            />
          </>
        }
      />

      {!envelope && error && <ApiError error={error} refresh={refresh} />}

      {data && (
        <div className="space-y-6">
          {/* ---- group 1: core status ---- */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
            <StatCard
              testId="stat-collector"
              label="Collector 状态"
              value={
                <span className="text-[19px]">
                  {(collector?.present === false
                    ? 'MISSING'
                    : (collector?.health_status ?? 'UNKNOWN')
                  ).toUpperCase()}
                </span>
              }
              tone={
                collector?.present === false || collector?.health_status === 'unhealthy'
                  ? 'danger'
                  : collector?.health_status === 'degraded'
                    ? 'warn'
                    : 'accent'
              }
              sub={
                collector?.present === false
                  ? collector.reason
                  : `已运行 ${fmtAgeShort(collector?.uptime_seconds)} · v${collector?.app_version ?? '—'}`
              }
            />
            <StatCard
              testId="stat-shadow"
              label="Shadow 状态"
              value={
                <span className="flex items-center gap-2 text-[19px]">
                  {shadow?.present === false ? 'MISSING' : (shadow?.mode ?? 'UNKNOWN').toUpperCase()}
                  {shadow?.mode === 'REPLAY' && <ModeBadge mode="REPLAY" />}
                </span>
              }
              tone={
                shadow?.present === false || shadow?.health_status === 'unhealthy'
                  ? 'danger'
                  : shadow?.mode === 'REPLAY'
                    ? 'warn'
                    : 'accent'
              }
              sub={
                shadow?.present === false
                  ? shadow.reason
                  : `v${shadow?.app_version ?? '—'} · ${fmtInt(shadow?.cycles)} 周期 · 模型 ${shadow?.primary_model ?? '—'}`
              }
            />
            <StatCard
              testId="stat-active-markets"
              label="当前活跃市场"
              value={fmtInt(data.active_markets)}
              sub={
                data.active_markets_source === 'shadow-report'
                  ? '来源：shadow report coverage'
                  : data.active_markets_source === 'archive-tail'
                    ? '来源：归档尾部（可交易）'
                    : '暂无数据来源'
              }
              hint="open_markets：正在接受订单的市场数"
            />
            <StatCard
              testId="stat-tokens"
              label="当前订阅 Token"
              value={fmtInt(data.subscribed_tokens)}
              sub="Collector WebSocket 订阅数"
            />
            <StatCard
              testId="stat-cycle-decisions"
              label="当前周期决策"
              value={fmtInt(
                (data.current_cycle_decisions as any)?.decisions ??
                  (data.current_cycle_decisions as any)?.by_state?.['REJECT'] ??
                  0,
              )}
              sub={
                data.current_cycle_decisions
                  ? `cycle ${(data.current_cycle_decisions as any)?.cycle_id?.slice(0, 8) ?? '—'} · 耗时 ${fmtInt((data.current_cycle_decisions as any)?.duration_ms)} ms`
                  : 'Shadow 心跳未报告最近周期'
              }
              hint="最近一个 Shadow 周期内的评估次数"
            />
            <StatCard
              testId="stat-book-age"
              label="最新行情延迟"
              value={fmtAge(data.latest_book_age_seconds)}
              tone={bookStale ? 'warn' : 'neutral'}
              sub={bookStale ? '超过 15 分钟 · STALE' : '盘口在新鲜窗口内'}
            />
          </div>

          {/* ---- group 2: decision distribution ---- */}
          <div>
            <SectionTitle hint={totals ? String(totals.window ?? '') : '暂无 report'}>
              决策分布
            </SectionTitle>
            <Card
              hint="ACCEPT 表示通过当前模型与风险过滤条件，不代表已验证的盈利机会。"
              testId="decision-distribution-card"
            >
              {totals ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 gap-3">
                    <StatCard
                      testId="stat-accept"
                      label="ACCEPT"
                      value={fmtInt(accept)}
                      tone="accent"
                      sub="通过内部过滤器"
                    />
                    <StatCard
                      testId="stat-reject"
                      label="REJECT"
                      value={fmtInt(reject)}
                      tone="warn"
                      sub="被门槛拒绝"
                    />
                    <StatCard
                      testId="stat-no-trade"
                      label="NO_TRADE"
                      value={fmtInt(noTrade)}
                      sub="无需交易 / 无候选"
                    />
                  </div>
                  <DistributionBar
                    accept={accept}
                    reject={reject}
                    noTrade={noTrade}
                    other={dataUnavailable}
                    testId="overview-distribution-bar"
                  />
                  <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-muted">
                    <span className="tnum">共评估 {fmtInt(evaluated)} 次</span>
                    <span className="flex items-center gap-1.5">
                      <ModeBadge mode="LIVE" />
                      <span className="tnum">ACCEPT {fmtInt(Number(totals.live_accepted ?? 0))}</span>
                    </span>
                    <span className="flex items-center gap-1.5">
                      <ModeBadge mode="REPLAY" />
                      <span className="tnum">ACCEPT {fmtInt(Number(totals.replay_accepted ?? 0))}</span>
                    </span>
                    <span title={String(totals.window ?? '')}>窗口：全库累计（重启不清零）</span>
                    <Link
                      to="/decisions"
                      className="ml-auto inline-flex items-center gap-1 font-medium text-accent hover:underline"
                    >
                      查看决策流 <ArrowRight size={13} />
                    </Link>
                  </div>
                </div>
              ) : (
                <EmptyState
                  title="决策聚合暂不可用"
                  reason={envelope?.freshness.reason ?? 'Shadow 尚未发布 report 快照。'}
                  testId="decision-totals-empty"
                />
              )}
            </Card>
          </div>

          {/* ---- group 3: model vs market ---- */}
          <div>
            <SectionTitle hint="模型概率 vs 盘口报价（报价为快照，非可执行价格）">
              Model vs Market
            </SectionTitle>
            <Card testId="model-vs-market-card">
              {data.model_vs_market.length > 0 ? (
                <div className="space-y-4">
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[640px] text-sm">
                      <thead>
                        <tr className="border-b border-line text-left text-xs text-muted">
                          <th className="pb-2 font-medium">市场</th>
                          <th className="pb-2 text-right font-medium">模型概率</th>
                          <th className="pb-2 text-right font-medium">Bid</th>
                          <th className="pb-2 text-right font-medium">Ask</th>
                          <th className="pb-2 text-right font-medium">理论净 Edge</th>
                          <th className="pb-2 text-right font-medium">最近决策</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.model_vs_market.slice(0, 5).map((m) => (
                          <tr key={m.market_id} className="border-b border-line/60 last:border-0">
                            <td className="max-w-[280px] truncate py-2.5 pr-3">
                              <Link
                                to={`/markets/${m.market_id}`}
                                className="hover:text-accent hover:underline"
                              >
                                {m.question || m.market_id}
                              </Link>
                            </td>
                            <td className="tnum py-2.5 text-right font-semibold text-accent">
                              {fmtProb(m.model_probability)}
                            </td>
                            <td className="tnum py-2.5 text-right">{fmtUsd(m.book?.best_bid)}</td>
                            <td className="tnum py-2.5 text-right">{fmtUsd(m.book?.best_ask)}</td>
                            <td className="tnum py-2.5 text-right">
                              {m.net_edge_usd === null || m.net_edge_usd === undefined
                                ? '—'
                                : `${m.net_edge_usd >= 0 ? '+' : ''}${fmtUsd(m.net_edge_usd)}`}
                            </td>
                            <td className="py-2.5 text-right">
                              {m.last_decision ? (
                                <DecisionBadge state={String(m.last_decision.state)} />
                              ) : (
                                <span className="text-muted">—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <ModelVsMarketBars
                    rows={data.model_vs_market.slice(0, 5).map((m) => ({
                      label: m.bracket_label || m.market_id.slice(0, 8),
                      model: m.model_probability ?? 0,
                      bid: m.book?.best_bid ?? null,
                      ask: m.book?.best_ask ?? null,
                    }))}
                    testId="overview-model-vs-market-chart"
                  />
                </div>
              ) : (
                <EmptyState
                  title="Model vs Market 暂未接入"
                  reason={
                    data.model_vs_market_reason ??
                    '模型概率与理论 Edge 只存在于 Shadow 数据库；需要 Shadow 集成市场快照生产者后展示。'
                  }
                  testId="model-vs-market-empty"
                />
              )}
              <p className="mt-4 border-t border-line pt-3 text-xs text-muted">
                Bid / Ask 为最新盘口快照的报价，不是可执行价格；理论净 Edge 是模型视角的期望值
                （成本后），不是收益。系统不产生任何收益曲线。
              </p>
            </Card>
          </div>

          {/* ---- group 4: system health ---- */}
          <div>
            <SectionTitle hint="数据管线健康与存储">系统健康</SectionTitle>
            <Card testId="system-health-card">
              <div className="grid grid-cols-1 gap-x-8 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">REST 健康</span>
                    <StatusChip value={sys.rest?.state ?? 'unknown'} testId="sys-rest" />
                  </div>
                  <div className="tnum text-xs text-muted">
                    连续失败 {fmtInt(sys.rest?.consecutive_failures ?? 0)} · 最近成功{' '}
                    {fmtAge(sys.rest?.last_success_age_seconds)}
                  </div>

                  <div className="flex items-center justify-between pt-1">
                    <span className="text-sm font-medium">WebSocket 健康</span>
                    <StatusChip value={sys.ws?.state ?? 'unknown'} testId="sys-ws" />
                  </div>
                  <div className="tnum text-xs text-muted">
                    最近消息 {fmtAge(sys.ws?.message_age_seconds)}
                  </div>
                  {collector?.telemetry?.ws_queue && (
                    <div className="tnum text-xs text-ink" data-testid="ws-queue">
                      队列 {fmtInt(collector.telemetry.ws_queue.depth)} /{' '}
                      {fmtInt(collector.telemetry.ws_queue.capacity)} · 高水位{' '}
                      {fmtInt(collector.telemetry.ws_queue.high_water)} · 丢帧{' '}
                      {fmtInt(collector.telemetry.ws_queue.frames_dropped ?? 0)}
                    </div>
                  )}
                </div>

                <div className="space-y-2">
                  <div className="text-sm font-medium">数据采集延迟</div>
                  <div className="tnum space-y-1 text-xs text-muted">
                    <div>上游联系：{fmtAge(sys.data_collection?.upstream_contact_age_seconds)}</div>
                    <div>盘口快照：{fmtAge(sys.data_collection?.book_age_seconds)}</div>
                    <div>
                      Shadow 视角静默：{fmtAge(sys.data_collection?.collector_silence_seconds)}
                    </div>
                  </div>

                  <div className="flex items-center justify-between pt-1">
                    <span className="text-sm font-medium">磁盘余量</span>
                    <span
                      className={`tnum text-sm font-semibold ${
                        (sys.storage?.disk_free_percent ?? 100) < 15 ? 'text-warn' : 'text-ink'
                      }`}
                    >
                      {(sys.storage?.disk_free_percent ?? 0).toFixed(1)}%
                    </span>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-sm font-medium">存储状态</div>
                  <div className="tnum space-y-1 text-xs text-muted">
                    <div>Collector DB：{fmtBytes(sys.storage?.collector_db_bytes)}</div>
                    <div>原始归档：{fmtBytes(sys.storage?.collector_raw_bytes)}</div>
                    <div>
                      Shadow DB：
                      {sys.storage?.shadow_db_bytes != null ? (
                        fmtBytes(sys.storage.shadow_db_bytes)
                      ) : (
                        <>
                          暂未接入{' '}
                          <span className="opacity-70">（心跳未提供该字段）</span>
                        </>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center justify-between pt-1">
                    <span className="text-sm font-medium">Shadow 模式</span>
                    <span className="flex items-center gap-2">
                      {sys.shadow_mode ? (
                        <ModeBadge mode={String(sys.shadow_mode)} />
                      ) : (
                        <span className="text-xs text-muted">—</span>
                      )}
                    </span>
                  </div>
                </div>
              </div>

              {Array.isArray(sys.recent_events) && sys.recent_events.length > 0 && (
                <div className="mt-4 border-t border-line pt-3">
                  <div className="mb-2 text-sm font-medium">最近异常 / 警告</div>
                  <ul className="space-y-1.5" data-testid="recent-events">
                    {sys.recent_events.slice(0, 4).map((event: any, i: number) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 rounded-tile bg-canvas px-3 py-2 text-xs"
                      >
                        <span className="font-medium text-muted">{event.source}</span>
                        <span className="min-w-0 break-words text-ink">{event.message}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>
          </div>

          {collector?.health_status === 'unhealthy' || shadow?.health_status === 'unhealthy' ? (
            <Banner tone="danger" title="存在不健康的服务" testId="unhealthy-banner">
              {[...(collector?.health_reasons ?? []), ...(shadow?.health_reasons ?? [])]
                .slice(0, 3)
                .join('；')}
            </Banner>
          ) : null}
        </div>
      )}
    </>
  )
}
