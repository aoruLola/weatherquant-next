/**
 * Decisions: the shadow's live decision stream.
 *
 * Two invariants the layout enforces:
 *
 * 1. The ACCEPT disclaimer is always on screen - filters, empty states,
 *    errors, nothing hides it.
 * 2. Consecutive ACCEPTs on the same market+direction are rendered as ONE
 *    grouped observation ("连续 ×N"), because N repeats of one view are not
 *    N independent opportunities.
 */
import { useMemo, useState } from 'react'
import { usePoll } from '../api/client'
import { DecisionBadge, FreshnessPill, ModeBadge, StatusChip } from '../components/status'
import PageHeader from '../components/PageHeader'
import {
  Banner,
  Card,
  EmptyState,
  inputClass,
  SectionTitle,
  selectClass,
  StatCard,
} from '../components/ui'
import { fmtDateTime, fmtInt, fmtProb, fmtUsd } from '../format'
import type { DecisionRow, DecisionsData } from '../types'

interface DisplayRow {
  kind: 'row' | 'group'
  row: DecisionRow
  count: number
}

/** Collapse runs of consecutive same-market ACCEPT rows into one display row. */
export function groupConsecutiveAccepts(rows: DecisionRow[]): DisplayRow[] {
  const out: DisplayRow[] = []
  let index = 0
  while (index < rows.length) {
    const row = rows[index]
    const isAccept = row.state.toUpperCase() === 'ACCEPT'
    if (isAccept) {
      let run = 1
      while (
        index + run < rows.length &&
        rows[index + run].state.toUpperCase() === 'ACCEPT' &&
        rows[index + run].market_id === row.market_id &&
        rows[index + run].direction === row.direction
      ) {
        run += 1
      }
      out.push({ kind: run > 1 ? 'group' : 'row', row, count: run })
      index += run
    } else {
      out.push({ kind: 'row', row, count: 1 })
      index += 1
    }
  }
  return out
}

const KNOWN_MODELS = ['calibrated', 'fusion', 'ecmwf_raw', 'gfs_raw']

export default function Decisions() {
  const [stateFilter, setStateFilter] = useState('')
  const [mode, setMode] = useState('')
  const [model, setModel] = useState('')
  const [marketId, setMarketId] = useState('')
  const [window, setWindow] = useState('')

  const sinceIso = useMemo(() => {
    if (!window) return ''
    const hours = Number(window)
    if (!Number.isFinite(hours)) return ''
    return new Date(Date.now() - hours * 3600_000).toISOString()
  }, [window])

  const query = useMemo(() => {
    const params = new URLSearchParams()
    if (stateFilter) params.set('state', stateFilter)
    if (mode) params.set('mode', mode)
    if (model) params.set('model', model)
    if (marketId.trim()) params.set('market_id', marketId.trim())
    if (sinceIso) params.set('since', sinceIso)
    params.set('limit', '200')
    const qs = params.toString()
    return `/api/v1/decisions${qs ? `?${qs}` : ''}`
  }, [stateFilter, mode, model, marketId, sinceIso])

  const { envelope, error } = usePoll<DecisionsData>(query, 5000)
  const data = envelope?.data
  const totals = data?.totals ?? null
  const grouped = useMemo(
    () => groupConsecutiveAccepts(data?.rows ?? []),
    [data],
  )

  return (
    <>
      <PageHeader
        title="Shadow 决策"
        english="Decisions"
        subtitle="只读决策观察 · 无真实交易"
        envelope={envelope}
        right={
          <>
            <StatusChip
              value={data?.totals_status === 'fresh' ? 'live' : data?.totals_status ?? 'unknown'}
              label={`totals ${data?.totals_status ?? 'MISSING'}`}
              title={`来源：${data?.totals_source ?? '未发布'}`}
            />
            <StatusChip
              value={data?.rows_status === 'unavailable' ? 'unknown' : data?.rows_status ?? 'unknown'}
              label={`rows ${data?.rows_status ?? '—'}`}
              title={data?.rows_reason ?? undefined}
            />
          </>
        }
      />

      {/* The disclaimer is unconditional and cannot be filtered away. */}
      <Banner tone="warn" title="关于 ACCEPT" testId="accept-disclaimer">
        ACCEPT 表示符合当前模型与风险过滤条件，不代表已验证的盈利机会；系统没有真实成交，
        也不会生成收益曲线。REPLAY 数据是历史回放，不是当时可做出的决策。
      </Banner>

      <div className="my-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          testId="stat-accept"
          label="ACCEPT"
          value={fmtInt(totals?.accepted ?? 0)}
          tone="accent"
          sub={`LIVE ${fmtInt(totals?.live_accepted ?? 0)} · REPLAY ${fmtInt(totals?.replay_accepted ?? 0)}`}
        />
        <StatCard
          testId="stat-reject"
          label="REJECT"
          value={fmtInt(totals?.rejected ?? 0)}
          tone="warn"
          sub="被风险/数据门槛拒绝"
        />
        <StatCard
          testId="stat-no-trade"
          label="NO_TRADE"
          value={fmtInt(totals?.no_trade ?? 0)}
          sub="无候选 / 无需交易"
        />
        <StatCard
          testId="stat-evaluated"
          label="已评估 evaluated"
          value={fmtInt(totals?.evaluated ?? 0)}
          sub={totals ? String(totals.window ?? '') : 'report 未发布'}
        />
      </div>

      {/*
        How many *independent* observations the ACCEPT total actually holds.
        The engine re-evaluates every open market every cycle, so the raw
        count is a count of evaluations - showing it alone is how a shadow
        log starts to sound like a track record.
      */}
      {totals?.accept_scope ? (
        <Card
          testId="accept-scope"
          className="mb-4"
          title="ACCEPT 的独立度"
          hint={String(totals.accept_scope.note ?? '')}
          bodyClassName="p-3.5"
        >
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <StatCard
              testId="stat-accept-observations"
              label="ACCEPT 观测次数"
              value={fmtInt(totals.accept_scope.accepted_observations ?? 0)}
              tone="accent"
              sub="每个周期各记一次"
            />
            <StatCard
              testId="stat-accept-markets"
              label="独立市场"
              value={fmtInt(totals.accept_scope.accepted_distinct_markets ?? 0)}
              sub="distinct market_id"
            />
            <StatCard
              testId="stat-accept-tokens"
              label="独立 Token"
              value={fmtInt(totals.accept_scope.accepted_distinct_tokens ?? 0)}
              sub="distinct token_yes"
            />
            <StatCard
              testId="stat-accept-repeats"
              label="重复周期信号"
              value={fmtInt(totals.accept_scope.accepted_repeats ?? 0)}
              tone="warn"
              sub="同一市场同方向的再次计数"
            />
            <StatCard
              testId="stat-accept-models"
              label="产生决策的模型"
              value={fmtInt(totals.accept_scope.models_with_decisions ?? 0)}
              sub={`独立目标日 ${fmtInt(totals.accept_scope.accepted_distinct_target_dates ?? 0)}`}
            />
          </div>
        </Card>
      ) : (
        totals && (
          <Card testId="accept-scope-unavailable" className="mb-4" title="ACCEPT 的独立度">
            <EmptyState
              title="不可用"
              reason="当前 Shadow report 未包含 accept_scope 维度（需要含该字段的版本）"
              testId="accept-scope-reason"
            />
          </Card>
        )
      )}

      {!totals && (
        <div className="mb-4">
          <Banner tone="danger" title="决策聚合不可用" testId="totals-missing">
            {envelope?.freshness.reason ?? 'Shadow report 快照缺失或过期。'} 决策流的累计统计
            来自 shadow report；行级明细来自 recent-decisions 快照。
          </Banner>
        </div>
      )}

      {/* filters */}
      <Card className="mb-4" bodyClassName="p-3.5">
        <div className="flex flex-wrap items-center gap-2.5" data-testid="decision-filters">
          <select
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            className={selectClass}
            aria-label="状态筛选"
            data-testid="filter-state"
          >
            <option value="">全部状态</option>
            <option value="ACCEPT">ACCEPT</option>
            <option value="REJECT">REJECT</option>
            <option value="NO_TRADE">NO_TRADE</option>
            <option value="DATA_UNAVAILABLE">DATA_UNAVAILABLE</option>
          </select>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            className={selectClass}
            aria-label="模式筛选"
            data-testid="filter-mode"
          >
            <option value="">LIVE + REPLAY</option>
            <option value="LIVE">仅 LIVE</option>
            <option value="REPLAY">仅 REPLAY</option>
          </select>
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className={selectClass}
            aria-label="模型筛选"
            data-testid="filter-model"
          >
            <option value="">全部模型</option>
            {KNOWN_MODELS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select
            value={window}
            onChange={(e) => setWindow(e.target.value)}
            className={selectClass}
            aria-label="时间窗口"
            data-testid="filter-window"
          >
            <option value="">全部时间</option>
            <option value="1">最近 1 小时</option>
            <option value="6">最近 6 小时</option>
            <option value="24">最近 24 小时</option>
          </select>
          <input
            value={marketId}
            onChange={(e) => setMarketId(e.target.value)}
            placeholder="market_id…"
            className={`${inputClass} w-full sm:w-52`}
            aria-label="市场筛选"
          />
          {(stateFilter || mode || model || marketId || window) && (
            <button
              type="button"
              onClick={() => {
                setStateFilter('')
                setMode('')
                setModel('')
                setMarketId('')
                setWindow('')
              }}
              className="text-xs text-muted underline hover:text-accent"
            >
              清除筛选
            </button>
          )}
          <span className="tnum ml-auto text-xs text-muted">
            {data ? `${data.total_rows ?? data.rows.length} 行` : '—'}
          </span>
        </div>
      </Card>

      {!envelope && error && (
        <Card>
          <EmptyState title="无法获取决策数据" reason={error} testId="decisions-error" />
        </Card>
      )}

      {envelope && data && (
        <Card title="决策流" hint="按时间倒序 · 同市场同方向的连续 ACCEPT 已分组">
          {data.rows_status === 'unavailable' ? (
            <EmptyState
              title="决策明细暂未接入"
              reason={
                data.rows_reason ??
                '需要 Shadow 引擎发布 recent-decisions 快照（docs/dashboard-data-contract.md §4）。'
              }
              testId="rows-unavailable"
            />
          ) : data.rows.length === 0 ? (
            <EmptyState
              title="没有符合条件的决策"
              reason="当前筛选下没有行；放宽条件或等待下一个周期。"
              testId="rows-empty"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm" data-testid="decisions-table">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="pb-2 font-medium">决策时间</th>
                    <th className="pb-2 font-medium">市场 / 区间</th>
                    <th className="pb-2 font-medium">方向</th>
                    <th className="pb-2 font-medium">状态</th>
                    <th className="pb-2 font-medium">模式</th>
                    <th className="pb-2 text-right font-medium">模型概率</th>
                    <th className="pb-2 text-right font-medium">可执行价格</th>
                    <th className="pb-2 text-right font-medium">理论净 Edge</th>
                    <th className="pb-2 font-medium">拒绝原因</th>
                    <th className="pb-2 text-right font-medium">盘口年龄</th>
                  </tr>
                </thead>
                <tbody>
                  {grouped.map(({ kind, row, count }, i) => (
                    <tr
                      key={`${row.decision_id}-${i}`}
                      className="border-b border-line/60 align-top last:border-0"
                      data-testid="decision-row"
                      data-state={row.state}
                    >
                      <td className="tnum py-2.5 pr-3 text-xs">
                        {fmtDateTime(row.decided_at_utc)}
                      </td>
                      <td className="max-w-[220px] py-2.5 pr-3">
                        <div className="truncate text-xs font-medium">
                          {row.market_question || row.market_id}
                        </div>
                        <div className="tnum text-[11px] text-muted">{row.bracket_label}</div>
                      </td>
                      <td className="py-2.5 pr-3 text-xs uppercase">{row.direction ?? '—'}</td>
                      <td className="py-2.5 pr-3">
                        <div className="flex items-center gap-1.5">
                          <DecisionBadge state={row.state} />
                          {kind === 'group' && (
                            <span
                              className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold text-white"
                              title="同一市场、同一方向的连续 ACCEPT 观测；这是 1 个机会被重复观察，不是 N 个独立机会"
                              data-testid="accept-group-badge"
                            >
                              连续 ×{count}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2.5 pr-3">
                        <ModeBadge mode={row.mode} />
                      </td>
                      <td className="tnum py-2.5 pr-3 text-right">
                        {fmtProb(row.model_probability)}
                      </td>
                      <td className="tnum py-2.5 pr-3 text-right">
                        {row.best_ask != null || row.best_bid != null
                          ? fmtUsd(row.direction?.startsWith('buy') ? row.best_ask : row.best_bid)
                          : '—'}
                      </td>
                      <td className="tnum py-2.5 pr-3 text-right">
                        {row.net_edge_usd == null ? (
                          '—'
                        ) : (
                          <span className={row.net_edge_usd > 0 ? 'text-accent' : 'text-muted'}>
                            {row.net_edge_usd > 0 ? '+' : ''}
                            {fmtUsd(row.net_edge_usd)}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 pr-3 text-xs text-muted">
                        {row.primary_reason ?? (row.reasons.length ? row.reasons.join(', ') : '—')}
                        {kind === 'group' && (
                          <div className="text-[11px] text-warn">连续观测已合并</div>
                        )}
                      </td>
                      <td className="tnum py-2.5 text-right text-xs text-muted">
                        {row.snapshot_age_ms != null ? `${row.snapshot_age_ms} ms` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-xs text-muted">
            <FreshnessPill freshness={envelope.freshness} />
            <span>
              模型 {String(data.filters_applied['model'] ?? '全部')} · 阈值版本{' '}
              {data.rows[0]?.thresholds_version ?? '—'}
            </span>
            <span className="flex items-center gap-2">
              最近周期：{data.last_cycle ? `${fmtInt((data.last_cycle as any).decisions)} 次评估` : '—'}
            </span>
          </div>
        </Card>
      )}

      <div className="mt-4">
        <SectionTitle hint="累计统计与行级明细分别有独立的新鲜度状态">
          主要拒绝原因（primary reasons）
        </SectionTitle>
        <Card>
          {totals?.primary_reasons && Object.keys(totals.primary_reasons).length > 0 ? (
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {Object.entries(totals.primary_reasons as Record<string, number>)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([reason, count]) => (
                  <li
                    key={reason}
                    className="flex items-center justify-between rounded-tile bg-canvas px-3 py-2 text-sm"
                  >
                    <span className="text-ink">{reason}</span>
                    <span className="tnum font-semibold">{fmtInt(count)}</span>
                  </li>
                ))}
            </ul>
          ) : (
            <EmptyState
              compact
              title="暂无拒绝原因数据"
              reason={
                totals
                  ? 'report 中 primary_reasons 为空。'
                  : 'shadow report 未发布（MISSING）。'
              }
              testId="reasons-empty"
            />
          )}
        </Card>
      </div>
    </>
  )
}
