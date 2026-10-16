/**
 * Performance: theoretical edge beside realised outcomes.
 *
 * The layout exists to keep two families of number apart. On the left,
 * what the model expected; on the right, what settlement proved. They are
 * never averaged, never netted, and never presented in one figure - a page
 * that merged them would be the most convincing lie this system could tell.
 *
 * Three invariants:
 *
 * 1. A missing snapshot reads as missing with a reason. It never reads as
 *    zero, because a dashboard with no backfill and a dashboard with no
 *    edge look identical if both render "0".
 * 2. Every theoretical figure sits under its own warnings, carried from
 *    the API rather than restated here.
 * 3. Actual profit is N/A. There is no real trading, so there is no
 *    realised return to show, and no chart that implies one.
 */
import { useEffect, useMemo, useState } from 'react'
import { usePoll } from '../api/client'
import { FreshnessPill } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Banner, Card, EmptyState, SectionTitle, StatCard, inputClass } from '../components/ui'
import { fmtInt, fmtPct, fmtUsd } from '../format'
import type { PerformancePayload } from '../types'

function warningsOf(payload?: PerformancePayload | null): string[] {
  return payload?.warnings ?? []
}

/**
 * A snapshot that was never published still arrives as an object - the API
 * returns `{reason, statistical_scope: [], warnings: []}` with an empty
 * `data`. Treating any truthy payload as present would render the zero
 * state of a dashboard that has data, which is exactly the confusion this
 * page exists to prevent.
 */
function isMissing(payload?: PerformancePayload | null): boolean {
  if (!payload) return true
  const body = payload.data
  return !body || Object.keys(body).length === 0
}

function ScopeLine({ payload }: { payload?: PerformancePayload | null }) {
  const scope = payload?.statistical_scope ?? []
  if (scope.length === 0) return null
  return <span className="text-xs text-muted">统计范围：{scope.join(' · ')}</span>
}

function MissingCard({ payload, title }: { payload?: PerformancePayload | null; title: string }) {
  return (
    <Card title={title}>
      <EmptyState
        title="不可用"
        reason={payload?.reason ?? '研究回填尚未发布该快照'}
        testId="performance-missing"
      />
    </Card>
  )
}

export default function Performance() {
  const [capital, setCapital] = useState('100')
  const [mode, setMode] = useState('portfolio')
  // The detail table is paged and filtered on the *server*: the browser never
  // receives the history, so it cannot be asked to slice it.
  const [pageSize, setPageSize] = useState('50')
  const [cursor, setCursor] = useState<string | null>(null)
  const [model, setModel] = useState('')
  const [direction, setDirection] = useState('')
  const [modeFilter, setModeFilter] = useState('')
  const [outcome, setOutcome] = useState('')
  const [targetDate, setTargetDate] = useState('')
  const [lastGeneration, setLastGeneration] = useState<string | null>(null)

  const summary = usePoll<PerformancePayload>('/api/v1/performance/summary', 30000)
  const edge = usePoll<PerformancePayload>('/api/v1/performance/edge', 30000)
  const winRate = usePoll<PerformancePayload>('/api/v1/performance/win-rate', 60000)

  const opportunitiesQuery = useMemo(() => {
    const params = new URLSearchParams()
    params.set('limit', pageSize)
    if (cursor) params.set('cursor', cursor)
    if (model) params.set('model', model)
    if (direction) params.set('direction', direction)
    if (modeFilter) params.set('mode', modeFilter)
    if (outcome) params.set('outcome', outcome)
    if (targetDate) params.set('target_date', targetDate)
    return `/api/v1/performance/opportunities?${params.toString()}`
  }, [pageSize, cursor, model, direction, modeFilter, outcome, targetDate])

  const opportunities = usePoll<PerformancePayload>(opportunitiesQuery, 60000)
  const models = usePoll<PerformancePayload>('/api/v1/performance/models', 60000)


  const capitalQuery = useMemo(() => {
    const parsed = Number(capital)
    const params = new URLSearchParams()
    // Invalid input is not sent: an empty or negative stake would be a 422
    // the UI chose to provoke, and the panel shows why instead.
    if (!Number.isFinite(parsed) || parsed <= 0) return null
    params.set('capital_usdc', String(parsed))
    params.set('mode', mode)
    return `/api/v1/performance/reference-capital?${params.toString()}`
  }, [capital, mode])

  // Slower than the rest on purpose: the on-demand stake is sized over the
  // published detail window, and it only changes when the snapshot does. A
  // change to the stake or the mode refetches immediately through the path.
  const capitalResult = usePoll<PerformancePayload>(capitalQuery ?? '', 300000, {
    enabled: capitalQuery !== null,
  })

  const summaryData = summary.envelope?.data?.data ?? {}
  const edgeData = edge.envelope?.data?.data ?? {}
  const winRateData = winRate.envelope?.data?.data ?? {}
  const opportunityRows = opportunities.envelope?.data?.items ?? []
  const opportunityPage = opportunities.envelope?.data?.data ?? {}
  const window = opportunityPage.window ?? {}
  const opportunityFilters = opportunityPage.filters ?? {}
  // Loose on purpose: the payload is a snapshot, not a fixed schema.
  const capitalData: Record<string, any> = capitalResult.envelope?.data ?? {}
  // The published whole-history block, and the on-demand window answer. Two
  // different accounts; never one row.
  const lifetimeCapital: Record<string, any> = summaryData.reference_capital ?? {}

  const generation = opportunities.envelope?.data?.generation_id ?? null
  const generationChanged =
    generation !== null && lastGeneration !== null && generation !== lastGeneration && cursor !== null
  useEffect(() => {
    if (generation !== null && generation !== lastGeneration) setLastGeneration(generation)
  }, [generation, lastGeneration])

  const anyFilter = Boolean(model || direction || modeFilter || outcome || targetDate)
  const resetToFirstPage = () => setCursor(null)

  const theoretical = [
    summary.envelope?.data,
    edge.envelope?.data,
    capitalResult.envelope?.data,
  ].flatMap(warningsOf)

  return (
    <>
      <PageHeader
        title="绩效分析"
        english="Performance"
        subtitle="理论 Edge 与已验证结果分列呈现 · 只读研究，无真实交易"
        envelope={summary.envelope}
        right={<FreshnessPill freshness={summary.envelope?.freshness} />}
      />

      {/* The disclaimer is unconditional: no filter, empty state or error
          hides the fact that nothing here was ever traded. */}
      <Banner tone="warn" title="口径声明" testId="performance-disclaimer">
        理论 Edge 是模型在自身概率下的期望值，不是利润、不是资产增长、
        不是独立交易机会的期望收益总额。系统不下单，实际收益恒为 N/A。
        ACCEPT 表示通过过滤条件，不代表已验证的盈利机会。
      </Banner>

      {/* ---- 5.1 统计总览 ---- */}
      <SectionTitle hint={summary.envelope?.data ? `样本 ${summary.envelope?.data?.sample_size ?? 0}` : '暂无快照'}>
        统计总览
      </SectionTitle>
      {isMissing(summary.envelope?.data) ? (
        <MissingCard payload={summary.envelope?.data} title="绩效总览" />
      ) : (
        <Card testId="performance-summary" className="mb-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              testId="stat-observation-edge-sum"
              label="Observation Edge Sum"
              value={fmtUsd(summaryData.observation_edge_sum)}
              tone="accent"
              hint="重复评估之和：同一机会每个周期各计一次"
              sub="非利润 · 非独立机会收益"
            />
            <StatCard
              testId="stat-unique-edge-sum"
              label="Unique Opportunity Edge Sum"
              value={fmtUsd(summaryData.unique_opportunity_edge_sum)}
              sub="去重后的独立机会代表 Edge"
            />
            <StatCard
              testId="stat-avg-edge"
              label="每份合约平均 Edge"
              value={fmtUsd(summaryData.average_net_edge)}
              sub="per share"
            />
            <StatCard
              testId="stat-win-rate"
              label="已验证方向胜率"
              value={
                summaryData.realized_directional_win_rate == null
                  ? 'N/A'
                  : fmtPct(summaryData.realized_directional_win_rate)
              }
              tone={summaryData.realized_directional_win_rate == null ? 'neutral' : 'accent'}
              sub="仅 RESOLVED + 已验证"
              hint={
                typeof summaryData.insufficient_reason === 'string'
                  ? summaryData.insufficient_reason
                  : undefined
              }
            />
            <StatCard
              testId="stat-verified-opps"
              label="已验证独立机会"
              value={fmtInt(summaryData.verified_opportunities)}
              sub={`已结算市场 ${fmtInt(summaryData.settled_markets)}`}
            />
            <StatCard
              testId="stat-opportunity-episodes"
              label="独立机会 episodes"
              value={fmtInt(summaryData.opportunity_episodes)}
              sub={`ACCEPT 观测 ${fmtInt(summaryData.accept_observations)}`}
            />
            <StatCard
              testId="stat-win-counts"
              label="WIN / LOSS"
              value={`${fmtInt((summaryData.counts?.WIN as number) ?? 0)} / ${fmtInt(
                (summaryData.counts?.LOSS as number) ?? 0,
              )}`}
              sub={`PENDING ${fmtInt((summaryData.counts?.PENDING as number) ?? 0)}`}
            />
            <StatCard
              testId="stat-distinct-markets"
              label="涉及独立市场"
              // The opportunity-scoped count. `coverage` publishes a
              // different one over every decision read, and the two used to
              // share this name.
              value={fmtInt(summaryData.accept_opportunity_distinct_markets)}
              sub={`独立目标日 ${fmtInt(summaryData.accept_opportunity_distinct_target_dates)}`}
            />
          </div>
          <div className="mt-3 space-y-1 border-t border-line pt-3">
            <ScopeLine payload={summary.envelope?.data} />
            {theoretical.slice(0, 4).map((w, i) => (
              <div key={i} className="text-xs text-muted">
                · {w}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* ---- 5.2 Edge 分析 ---- */}
      <SectionTitle hint="按模型 / 方向 / 目标日分组，附分位数与每日趋势">Edge 分析</SectionTitle>
      {isMissing(edge.envelope?.data) ? (
        <MissingCard payload={edge.envelope?.data} title="Edge 分布" />
      ) : (
        <Card testId="performance-edge" className="mb-4">
          {Object.keys(edgeData.by_model ?? {}).length === 0 ? (
            <EmptyState title="尚无 ACCEPT 样本" reason="等待 Shadow 决策积累" testId="edge-empty" />
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {['mean', 'median', 'p05', 'p95'].map((key) => (
                  <StatCard
                    key={key}
                    label={key}
                    value={fmtUsd((edgeData.overall as any)?.[key])}
                    sub={`n = ${fmtInt((edgeData.overall as any)?.count ?? 0)}`}
                  />
                ))}
              </div>
              <div>
                <div className="mb-2 text-xs font-medium text-ink">按模型</div>
                <ul className="space-y-1.5 text-xs">
                  {Object.entries((edgeData.by_model as any) ?? {}).map(([model, stats]) => (
                    <li key={model} className="flex justify-between gap-3">
                      <span className="text-muted">{model}</span>
                      <span className="tnum">
                        mean {fmtUsd((stats as any).mean)} · n={fmtInt((stats as any).count)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="mb-2 text-xs font-medium text-ink">按 Edge 区间</div>
                <ul className="space-y-1.5 text-xs">
                  {Object.entries((edgeData.by_edge_band as any) ?? {}).map(([band, stats]) => (
                    <li key={band} className="flex justify-between gap-3">
                      <span className="text-muted">{band}</span>
                      <span className="tnum">
                        {fmtInt((stats as any)?.count)} 次评估 · mean{' '}
                        {fmtUsd((stats as any)?.mean)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
          <div className="mt-3 border-t border-line pt-3">
            <ScopeLine payload={edge.envelope?.data} />
          </div>
        </Card>
      )}

      {/* ---- 5.3 胜率分析 ---- */}
      <SectionTitle hint="未结算与不可验证样本不进分母">胜率分析</SectionTitle>
      {isMissing(winRate.envelope?.data) ? (
        <MissingCard payload={winRate.envelope?.data} title="已验证胜率" />
      ) : (
        <Card testId="performance-win-rate" className="mb-4">
          <WinRateView payload={winRate.envelope?.data} overall={winRateData.overall as any} />
          <div className="mt-3 space-y-1 border-t border-line pt-3">
            <ScopeLine payload={winRate.envelope?.data} />
            {warningsOf(winRate.envelope?.data).map((w, i) => (
              <div key={i} className="text-xs text-muted">
                · {w}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* ---- Reference capital ---- */}
      <SectionTitle hint="研究假设，非真实资金 · 复用 execution/pricing.py">参考本金</SectionTitle>
      {/*
        Two simulations live on this page and they are not the same account.
        The upper block is the *lifetime* figure the research engine published:
        every episode it has consumed, simulated once at publish time. The
        lower one is what the stake and mode below produce, sized over the
        published detail window. They are labelled, and their counts are never
        mixed into one row.
      */}
      <Card testId="lifetime-capital" className="mb-4">
        <div className="mb-2 flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium">全历史参考本金（终身）</span>
          <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
            scope = {String(lifetimeCapital.scope ?? 'lifetime')}
          </span>
          <span className="text-xs text-muted">
            覆盖全部已消费历史 · 发布时模拟一次，不由本页每次请求重算
          </span>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="参考本金" value={fmtUsd(lifetimeCapital.capital_usdc)} sub="hypothetical" />
          <StatCard
            label="可执行 / 不可执行"
            value={`${fmtInt(lifetimeCapital.executable_count ?? 0)} / ${fmtInt(
              lifetimeCapital.not_executable_count ?? 0,
            )}`}
            sub={`资金状态 ${String(lifetimeCapital.funding_state ?? '—')}`}
          />
          <StatCard
            label="峰值资金利用率"
            value={fmtPct(Number(lifetimeCapital.peak_utilization ?? lifetimeCapital.utilization ?? 0))}
            sub="peak open / capital"
          />
          <StatCard
            label="全历史模拟净收益"
            value={
              lifetimeCapital.simulated_net_profit_usdc == null
                ? 'N/A'
                : fmtUsd(Number(lifetimeCapital.simulated_net_profit_usdc))
            }
            sub={
              lifetimeCapital.simulated_net_profit_usdc == null
                ? String(lifetimeCapital.funding_state_reason ?? '证据不足')
                : '已结算'
            }
          />
        </div>
        <div className="mt-2 space-y-0.5 text-xs text-muted" data-testid="lifetime-capital-detail">
          <div>
            统计区间 {String(lifetimeCapital.statistical_window?.first_seen_utc_from ?? '—')} →{' '}
            {String(lifetimeCapital.statistical_window?.last_seen_utc_to ?? '—')} · episode{' '}
            {fmtInt(lifetimeCapital.statistical_window?.episodes_simulated ?? 0)}
          </div>
          <div>
            数据水位 {String((lifetimeCapital.data_watermark ?? [])[0] ?? '—')} · 结算覆盖{' '}
            {fmtInt(lifetimeCapital.settlement_coverage?.decisive_episodes ?? 0)} /{' '}
            {fmtInt(lifetimeCapital.settlement_coverage?.episodes ?? 0)} 个 episode 有定论
          </div>
          <div>
            不可执行原因：{' '}
            {Object.entries((lifetimeCapital.not_executable_reason_counts as any) ?? {}).length === 0
              ? '无'
              : Object.entries((lifetimeCapital.not_executable_reason_counts as any) ?? {})
                  .map(([reason, count]) => `${reason} ${fmtInt(Number(count))}`)
                  .join(' · ')}
          </div>
        </div>
        <div className="mt-2 border-t border-line pt-2">
          {(lifetimeCapital.warnings as string[] | undefined)?.map((w, i) => (
            <div key={i} className="text-xs text-muted">
              · {w}
            </div>
          ))}
        </div>
      </Card>

      <Card testId="reference-capital" className="mb-4">
        <div className="mb-2 flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium">按窗口模拟</span>
          <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
            scope = window
          </span>
          <span className="text-xs text-muted">
            只覆盖明细窗口 · 独立本金假设，不是终身账户某段时间的余额
          </span>
        </div>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted">Reference Capital (USDC)</span>
            <input
              type="number"
              min="0"
              step="10"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
              className={`${inputClass} tnum w-36`}
              aria-label="参考本金"
              data-testid="capital-input"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted">模式</span>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              className={inputClass}
              aria-label="参考本金模式"
              data-testid="capital-mode"
            >
              <option value="portfolio">组合 portfolio</option>
              <option value="single_opportunity">单机会 single_opportunity</option>
            </select>
          </label>
        </div>

        {capitalQuery === null ? (
          <Banner tone="warn" title="参考本金必须大于 0" testId="capital-invalid" />
        ) : capitalResult.loading && !capitalResult.envelope ? (
          <EmptyState title="正在计算" reason="按当前本金与模式模拟" testId="capital-loading" />
        ) : capitalResult.error ? (
          <EmptyState title="无法获取" reason={capitalResult.error} testId="capital-error" />
        ) : (
          <div className="space-y-3" data-testid="capital-result">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard
                label="参考本金"
                value={fmtUsd(capitalData.capital_usdc)}
                sub="hypothetical"
              />
              <StatCard
                label="参考资金利用率"
                value={
                  capitalData.utilization == null ? '—' : fmtPct(Number(capitalData.utilization))
                }
                sub="peak open / capital"
              />
              <StatCard
                label="可执行机会"
                value={`${fmtInt(capitalData.executable_count ?? 0)} / ${fmtInt(
                  (capitalData.executable_count ?? 0) + (capitalData.not_executable_count ?? 0),
                )}`}
                sub="可被盘口证据支撑"
              />
              <StatCard
                label="盘口容量不足"
                value={fmtInt(capitalData.capacity_limited_count ?? 0)}
                sub="所需资金超过当时深度"
              />
            </div>

            <div className="text-xs text-muted">
              {capitalData.mode === 'single_opportunity' ? (
                <>
                  <div>
                    每个机会各自持有参考本金，互不占用
                  </div>
                  <div>
                    可执行机会的独立理论收益：
                    {(capitalData.per_opportunity_expected_edge ?? []).length === 0
                      ? 'N/A — 无可执行机会'
                      : `${(capitalData.per_opportunity_expected_edge as number[]).length} 个机会，各自独立`}
                  </div>
                  <div className="font-medium">
                    不提供合并总额：十个"各自投入 N"的答案不是一个持有十份本金的账户
                  </div>
                </>
              ) : (
                <>
                  <div>
                    理论预期收益（组合）：
                    {capitalData.theoretical_expected_net_edge_usdc == null
                      ? 'N/A — 无可执行机会'
                      : fmtUsd(capitalData.theoretical_expected_net_edge_usdc)}
                  </div>
                  <div>
                    理论预期 ROI：
                    {capitalData.theoretical_expected_roi == null
                      ? 'N/A'
                      : fmtPct(Number(capitalData.theoretical_expected_roi))}
                  </div>
                  <div>
                    同时占用峰值：{fmtUsd(capitalData.peak_open_allocation_usdc)} · 余额：
                    {fmtUsd(capitalData.balance_remaining_usdc)}
                  </div>
                </>
              )}
              <div>
                经历史数据验证的模拟收益：
                {capitalData.backtestable === false
                  ? `N/A — ${(capitalData.not_backtestable_reasons ?? []).join('；') || '证据不足'}`
                  : '见明细'}
              </div>
              <div className="font-medium">实际收益：N/A（系统不下单，无真实交易）</div>
            </div>

            {capitalData.backtestable === false && (
              <Banner tone="info" title="不可回测" testId="capital-not-backtestable">
                {(capitalData.not_backtestable_reasons ?? []).join('；')}
              </Banner>
            )}

            <div className="border-t border-line pt-3">
              <ScopeLine payload={capitalResult.envelope?.data} />
              {warningsOf(capitalResult.envelope?.data).map((w, i) => (
                <div key={i} className="text-xs text-muted">
                  · {w}
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* ---- 5.4 历史机会表 ---- */}
      <SectionTitle hint="去重后的独立机会 · 同市场多 episode 不是独立结算事件">
        历史机会
      </SectionTitle>
      {opportunities.loading && !opportunities.envelope ? (
        <Card>
          <EmptyState title="正在加载" reason="读取已发布的机会分页" testId="opportunities-loading" />
        </Card>
      ) : opportunities.error ? (
        <Card>
          <EmptyState
            title="无法获取"
            reason={
              opportunities.status === 409
                ? '游标属于上一个已发布批次；请回到第一页重新开始'
                : `请求失败（${opportunities.error}）`
            }
            testId="opportunities-error"
          />
        </Card>
      ) : isMissing(opportunities.envelope?.data) ? (
        <MissingCard payload={opportunities.envelope?.data} title="机会列表" />
      ) : (
        <Card testId="opportunities-table">
          <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
            <label className="flex flex-col gap-1">
              <span className="text-muted">模型</span>
              <select
                className={inputClass}
                value={model}
                onChange={(e) => {
                  setModel(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="filter-model"
              >
                <option value="">全部</option>
                {((models.envelope?.data?.data?.models as string[]) ?? []).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">方向</span>
              <select
                className={inputClass}
                value={direction}
                onChange={(e) => {
                  setDirection(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="filter-direction"
              >
                <option value="">全部</option>
                <option value="buy_yes">buy_yes</option>
                <option value="sell_yes">sell_yes</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">模式</span>
              <select
                className={inputClass}
                value={modeFilter}
                onChange={(e) => {
                  setModeFilter(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="filter-mode"
              >
                <option value="">全部</option>
                <option value="LIVE">LIVE</option>
                <option value="REPLAY">REPLAY</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">结算</span>
              <select
                className={inputClass}
                value={outcome}
                onChange={(e) => {
                  setOutcome(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="filter-outcome"
              >
                <option value="">全部</option>
                {['WIN', 'LOSS', 'PENDING', 'DISPUTED', 'UNVERIFIABLE', 'UNAVAILABLE'].map(
                  (value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ),
                )}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">目标日</span>
              <input
                type="date"
                className={inputClass}
                value={targetDate}
                onChange={(e) => {
                  setTargetDate(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="filter-target-date"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">每页</span>
              <select
                className={inputClass}
                value={pageSize}
                onChange={(e) => {
                  setPageSize(e.target.value)
                  resetToFirstPage()
                }}
                data-testid="page-size"
              >
                {['25', '50', '100', '200'].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            {anyFilter && (
              <button
                type="button"
                className="rounded border border-line px-2 py-1 text-muted"
                onClick={() => {
                  setModel('')
                  setDirection('')
                  setModeFilter('')
                  setOutcome('')
                  setTargetDate('')
                  resetToFirstPage()
                }}
                data-testid="filter-clear"
              >
                清除筛选
              </button>
            )}
          </div>

          {generationChanged && (
            <div className="mb-3 rounded border border-line bg-panel px-3 py-2 text-xs text-muted" data-testid="generation-changed">
              研究批次已更新（{lastGeneration} → {generation}）。游标属于上一批，页码不通用；
              请回到第一页。
              <button
                type="button"
                className="ml-2 underline"
                onClick={resetToFirstPage}
                data-testid="generation-reset"
              >
                回到第一页
              </button>
            </div>
          )}

          {opportunityRows.length === 0 ? (
            <EmptyState
              title={anyFilter ? '没有符合条件的机会' : '尚无独立机会'}
              reason={
                opportunities.envelope?.data?.reason ??
                (anyFilter ? '筛选条件下该窗口内没有 episode' : '等待 ACCEPT 积累并形成 episode')
              }
              testId="opportunities-empty"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm" data-testid="opportunities">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="pb-2 font-medium">首次 ACCEPT</th>
                    <th className="pb-2 font-medium">目标日</th>
                    <th className="pb-2 font-medium">市场 / 区间</th>
                    <th className="pb-2 font-medium">模型</th>
                    <th className="pb-2 font-medium">方向</th>
                    <th className="pb-2 text-right font-medium">理论净 Edge</th>
                    <th className="pb-2 text-right font-medium">模型概率</th>
                    <th className="pb-2 text-right font-medium">评估价格</th>
                    <th className="pb-2 text-right font-medium">持续</th>
                    <th className="pb-2 text-right font-medium">重复</th>
                    <th className="pb-2 font-medium">结算</th>
                  </tr>
                </thead>
                <tbody>
                  {opportunityRows.map((row: Record<string, any>) => (
                    <tr
                      key={row.episode_id}
                      className="border-b border-line/50 last:border-0"
                      data-testid="opportunity-row"
                    >
                      <td className="py-2 text-xs">{row.first_seen_utc ?? '—'}</td>
                      <td className="py-2 text-xs">{row.target_date ?? '—'}</td>
                      <td className="max-w-[220px] truncate text-xs">
                        {row.bracket_label ?? '—'}
                      </td>
                      <td className="py-2 text-xs">{row.model_name ?? '—'}</td>
                      <td className="py-2 text-xs">{row.direction ?? '—'}</td>
                      <td className="tnum py-2 text-right text-xs">{fmtUsd(row.first_edge)}</td>
                      <td className="tnum py-2 text-right text-xs">
                        {row.first_model_probability == null
                          ? '—'
                          : fmtPct(row.first_model_probability)}
                      </td>
                      <td className="tnum py-2 text-right text-xs">
                        {row.first_market_price == null ? '—' : fmtUsd(row.first_market_price)}
                      </td>
                      <td className="tnum py-2 text-right text-xs">
                        {row.duration_seconds == null
                          ? '—'
                          : `${Math.round(Number(row.duration_seconds) / 60)}m`}
                      </td>
                      <td className="tnum py-2 text-right text-xs">
                        {fmtInt(row.observation_count)}
                      </td>
                      <td className="py-2 text-xs">
                        {row.outcome ?? <span className="text-muted">未结算</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span data-testid="page-summary">
              {opportunityRows.length === 0
                ? '本页 0 条'
                : `${fmtInt(Number(opportunityPage.offset ?? 0) + 1)}–${fmtInt(
                    Number(opportunityPage.offset ?? 0) + opportunityRows.length,
                  )} / ${fmtInt(opportunities.envelope?.data?.total ?? 0)} 条`}
              {Object.keys(opportunityFilters).length > 0 && (
                <> · 筛选 {Object.entries(opportunityFilters).map(([k, v]) => `${k}=${v}`).join(' · ')}</>
              )}
            </span>
            <span className="flex items-center gap-2">
              <button
                type="button"
                className="rounded border border-line px-2 py-1 disabled:opacity-40"
                disabled={opportunities.envelope?.data?.prev_cursor == null}
                onClick={() => setCursor(String(opportunities.envelope?.data?.prev_cursor))}
                data-testid="page-prev"
              >
                上一页
              </button>
              <button
                type="button"
                className="rounded border border-line px-2 py-1 disabled:opacity-40"
                disabled={opportunities.envelope?.data?.next_cursor == null}
                onClick={() => setCursor(String(opportunities.envelope?.data?.next_cursor))}
                data-testid="page-next"
              >
                下一页
              </button>
            </span>
          </div>

          {window && window.complete === false && (
            <div className="mt-2 text-xs text-muted" data-testid="window-note">
              明细仅覆盖最近 {fmtInt(window.episodes)} 条（{fmtInt(window.days)} 天窗口，共{' '}
              {fmtInt(window.episodes_all)} 条）。上方汇总、Edge 分布与胜率覆盖全部历史。
            </div>
          )}
          <div className="mt-2 border-t border-line pt-2">
            <ScopeLine payload={opportunities.envelope?.data} />
          </div>
        </Card>
      )}

      {/* ---- 模型对比 ---- */}
      <SectionTitle hint="Edge 分布与已验证胜率并列，不做加权排名">模型对比</SectionTitle>
      {isMissing(models.envelope?.data) ? (
        <MissingCard payload={models.envelope?.data} title="模型比较" />
      ) : (
        <Card testId="performance-models">
          <ul className="space-y-1.5 text-xs">
            {Object.entries((models.envelope?.data?.data?.by_model_edge as any) ?? {}).map(
              ([model, stats]) => (
                <li key={model} className="flex justify-between gap-3">
                  <span className="text-muted">{model}</span>
                  <span className="tnum">
                    mean {fmtUsd((stats as any).mean)} · n={fmtInt((stats as any).count)}
                  </span>
                </li>
              ),
            )}
          </ul>
          <div className="mt-3 border-t border-line pt-3">
            <div className="text-xs text-muted">
              {models.envelope?.data?.data?.note ??
                'Brier / LogLoss / ECE 等样本外评分由研究评估模块提供。'}
            </div>
            <ScopeLine payload={models.envelope?.data} />
          </div>
        </Card>
      )}
    </>
  )
}

function WinRateView({
  payload,
  overall,
}: {
  payload?: PerformancePayload | null
  overall: any
}) {
  if (!overall) {
    return (
      <EmptyState
        title="无已结算样本"
        reason={payload?.reason ?? '等待官方结算与验证'}
        testId="win-rate-empty"
      />
    )
  }
  const ci = overall.confidence_interval as [number, number] | null
  return (
    <div className="space-y-3" data-testid="win-rate-view">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          testId="stat-wr"
          label="胜率"
          value={overall.win_rate == null ? 'N/A' : fmtPct(overall.win_rate)}
          tone={overall.win_rate == null ? 'neutral' : 'accent'}
          hint={overall.reason ?? undefined}
          sub={overall.method ?? '—'}
        />
        <StatCard
          testId="stat-wr-samples"
          label="有效样本"
          value={fmtInt(overall.valid_samples)}
          sub="WIN + LOSS"
        />
        <StatCard
          testId="stat-wr-dates"
          label="独立目标日"
          value={fmtInt(overall.distinct_target_dates)}
          sub="聚类单位"
        />
        <StatCard
          testId="stat-wr-ci"
          label="95% 置信区间"
          value={ci ? `${fmtPct(ci[0])} – ${fmtPct(ci[1])}` : 'N/A'}
          sub={ci ? overall.method : '样本不足'}
        />
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        {['WIN', 'LOSS', 'PENDING', 'DISPUTED', 'UNVERIFIABLE'].map((key) => (
          <StatCard key={key} label={key} value={fmtInt(overall.counts?.[key] ?? 0)} />
        ))}
      </div>
      {overall.reason && (
        <Banner tone="info" title="样本不足" testId="win-rate-reason">
          {overall.reason}
        </Banner>
      )}
    </div>
  )
}
