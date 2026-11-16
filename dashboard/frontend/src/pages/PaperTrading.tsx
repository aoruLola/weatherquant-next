import { useMemo, useState } from 'react'
import { usePoll } from '../api/client'
import { Sparkline } from '../components/charts'
import PageHeader from '../components/PageHeader'
import { Badge } from '../components/status'
import { Banner, Card, EmptyState, StatCard } from '../components/ui'
import { fmtDateTime, fmtInt, fmtPct, fmtUsd } from '../format'
import type { PaperFills, PaperSummary } from '../types'

function money(value: number | null | undefined, digits = 2): string {
  return value == null ? 'N/A' : `${fmtUsd(value, digits)} USDC`
}

function percent(value: number | null | undefined): string {
  return value == null ? 'N/A' : fmtPct(value, 2)
}

function number(value: number | null | undefined, digits = 2): string {
  return value == null ? 'N/A' : fmtUsd(value, digits)
}

const TH = 'whitespace-nowrap px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-muted'
const TD = 'px-3 py-2.5 text-sm text-ink'

function PaperBadge({ testId = 'paper-mode-badge' }: { testId?: string }) {
  return (
    <span data-testid={testId}>
      <Badge tone="warn" title="虚拟模拟账户，无真实交易">
        模拟 / PAPER
      </Badge>
    </span>
  )
}

export default function PaperTrading() {
  const summary = usePoll<PaperSummary>('/api/v1/paper/summary', 15000)
  const [cursor, setCursor] = useState<string | null>(null)
  const [cursorHistory, setCursorHistory] = useState<(string | null)[]>([])
  const fillsPath = useMemo(() => {
    const params = new URLSearchParams({ limit: '20' })
    if (cursor) params.set('cursor', cursor)
    return `/api/v1/paper/fills?${params.toString()}`
  }, [cursor])
  const fills = usePoll<PaperFills>(fillsPath, 15000)

  const data = summary.envelope?.data
  const fillPage = fills.envelope?.data
  const positions = data?.positions ?? []
  const curve = data?.equity_curve.points ?? []

  const nextPage = () => {
    const next = fillPage?.next_cursor
    if (!next) return
    setCursorHistory((history) => [...history, cursor])
    setCursor(next)
  }
  const previousPage = () => {
    if (cursorHistory.length === 0) return
    setCursor(cursorHistory[cursorHistory.length - 1] ?? null)
    setCursorHistory((history) => history.slice(0, -1))
  }

  return (
    <>
      <PageHeader
        title="模拟交易"
        english="Paper Trading"
        envelope={summary.envelope}
        right={<PaperBadge />}
      />

      <Banner tone="warn" title="虚拟模拟账户" testId="paper-disclaimer">
        所有资金、持仓、盈亏和成交均为 Paper 模拟，不代表真实交易或真实收益。
      </Banner>

      {data?.status === 'unavailable' && (
        <div className="mt-3">
          <Banner tone="danger" title="Paper 数据不可用" testId="paper-unavailable">
            {data.reason ?? '无法读取 Paper 只读 API'}
          </Banner>
        </div>
      )}

      <div className="my-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          testId="paper-initial"
          label="初始虚拟资金"
          value={money(data?.initial_capital_usdc)}
          sub="Paper USDC"
        />
        <StatCard
          testId="paper-cash"
          label="可用现金"
          value={money(data?.available_usdc)}
          sub="模拟账户"
        />
        <StatCard
          testId="paper-occupied"
          label="占用成本"
          value={money(data?.occupied_usdc)}
          sub="未平仓成本基础"
        />
        <StatCard
          testId="paper-equity"
          label="当前净值"
          value={money(data?.equity_usdc)}
          sub={`成本口径 ${money(data?.cost_equity_usdc)}`}
          tone={data?.equity_usdc == null ? 'neutral' : 'accent'}
          hint="现金加全部持仓的有效 Best Bid 市值；缺任一行情则为 N/A"
        />
        <StatCard
          testId="paper-realized"
          label="已实现 P&L"
          value={money(data?.realized_pnl_usdc)}
          sub="账本已结算口径"
        />
        <StatCard
          testId="paper-unrealized"
          label="未实现 P&L"
          value={money(data?.unrealized_pnl_usdc)}
          sub={data?.marks_status === 'unavailable' ? '缺少有效行情' : '按当前 Best Bid'}
          hint="任一持仓缺行情时，不使用部分估值合计"
        />
        <StatCard
          testId="paper-roi"
          label="ROI"
          value={percent(data?.roi)}
          sub="相对初始虚拟资金"
        />
        <StatCard
          testId="paper-drawdown"
          label="最大回撤"
          value={percent(data?.max_drawdown)}
          sub="资金曲线 · 成本口径"
        />
      </div>

      <Card
        title="资金曲线"
        actions={<Badge>成本口径</Badge>}
        testId="paper-equity-card"
        className="mb-4"
      >
        {curve.length > 0 ? (
          <Sparkline points={curve} height={240} testId="paper-equity-chart" />
        ) : (
          <EmptyState
            title="N/A"
            reason={data?.reason ?? '暂无账本资金曲线'}
            compact
            testId="paper-equity-na"
          />
        )}
      </Card>

      <Card title="持仓" testId="paper-positions-card" className="mb-4" bodyClassName="p-0">
        {data?.status === 'ok' && positions.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[880px] border-collapse" data-testid="paper-positions-table">
              <thead className="border-b border-line bg-canvas/60">
                <tr>
                  <th className={TH}>市场 / Token</th>
                  <th className={TH}>份额</th>
                  <th className={TH}>平均成本</th>
                  <th className={TH}>成本基础</th>
                  <th className={TH}>当前 Best Bid</th>
                  <th className={TH}>当前市值</th>
                  <th className={TH}>未实现 P&L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {positions.map((position) => (
                  <tr key={`${position.market_id}:${position.token_id}`}>
                    <td className={`${TD} max-w-[260px]`}>
                      <div className="truncate font-medium" title={position.market_id}>
                        {position.market_id}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-muted" title={position.token_id}>
                        {position.token_id}
                      </div>
                    </td>
                    <td className={`${TD} tnum`}>{number(position.shares, 2)}</td>
                    <td className={`${TD} tnum`}>{number(position.avg_cost_usd_per_share, 4)}</td>
                    <td className={`${TD} tnum`}>{money(position.cost_basis_usdc)}</td>
                    <td className={`${TD} tnum`} data-testid="paper-position-mark">
                      {number(position.mark_usd_per_share, 4)}
                    </td>
                    <td className={`${TD} tnum`}>{money(position.market_value_usdc)}</td>
                    <td className={`${TD} tnum`} data-testid="paper-position-pnl">
                      {money(position.unrealized_pnl_usdc)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={data?.status === 'unavailable' ? 'N/A' : '暂无持仓'}
            reason={data?.status === 'unavailable' ? data.reason : undefined}
            compact
          />
        )}
      </Card>

      <div className="mb-4 grid gap-4 xl:grid-cols-[minmax(260px,0.65fr)_minmax(0,1.35fr)]">
        <Card title="拒绝原因汇总" testId="paper-rejection-summary">
          {data?.status === 'ok' && data.rejection_summary.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {data.rejection_summary.map((row) => (
                <span
                  key={row.reason}
                  className="inline-flex items-center gap-2 rounded-tile border border-line bg-canvas px-3 py-2 text-xs"
                >
                  <span className="font-medium text-ink">{row.reason}</span>
                  <span className="tnum font-semibold text-warn">{fmtInt(row.count)}</span>
                </span>
              ))}
            </div>
          ) : (
            <EmptyState
              title={data?.status === 'unavailable' ? 'N/A' : '暂无拒绝'}
              reason={data?.status === 'unavailable' ? data.reason : undefined}
              compact
            />
          )}
        </Card>

        <Card title="拒绝记录" testId="paper-rejections" bodyClassName="p-0">
          {data?.status === 'ok' && data.rejections.length > 0 ? (
            <div className="max-h-[360px] overflow-auto">
              <table className="w-full min-w-[620px] border-collapse">
                <thead className="border-b border-line bg-canvas/60">
                  <tr>
                    <th className={TH}>时间</th>
                    <th className={TH}>市场</th>
                    <th className={TH}>原因</th>
                    <th className={TH}>详情</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {data.rejections.map((rejection) => (
                    <tr key={rejection.decision_id}>
                      <td className={`${TD} tnum whitespace-nowrap text-xs`}>
                        {fmtDateTime(rejection.decision_time_utc)}
                      </td>
                      <td className={`${TD} max-w-[180px] truncate text-xs`} title={rejection.market_id}>
                        {rejection.market_id}
                      </td>
                      <td className={`${TD} text-xs font-medium text-warn`}>
                        {rejection.refusal_reason ?? 'N/A'}
                      </td>
                      <td className={`${TD} max-w-[280px] text-xs text-muted`}>
                        {rejection.refusal_detail ?? 'N/A'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title={data?.status === 'unavailable' ? 'N/A' : '暂无拒绝记录'}
              reason={data?.status === 'unavailable' ? data.reason : undefined}
              compact
            />
          )}
        </Card>
      </div>

      <Card
        title="模拟成交流水"
        actions={<PaperBadge testId="paper-fills-mode-badge" />}
        testId="paper-fills-card"
        bodyClassName="p-0"
      >
        {fillPage?.status === 'ok' && fillPage.fills.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] border-collapse" data-testid="paper-fills-table">
              <thead className="border-b border-line bg-canvas/60">
                <tr>
                  <th className={TH}>成交时间</th>
                  <th className={TH}>方向</th>
                  <th className={TH}>市场 / Token</th>
                  <th className={TH}>成交份额</th>
                  <th className={TH}>均价</th>
                  <th className={TH}>成交额</th>
                  <th className={TH}>费用</th>
                  <th className={TH}>状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {fillPage.fills.map((fill) => (
                  <tr key={fill.decision_id} data-testid={`paper-fill-${fill.decision_id}`}>
                    <td className={`${TD} tnum whitespace-nowrap text-xs`}>
                      {fmtDateTime(fill.filled_at_utc ?? fill.decision_time_utc)}
                    </td>
                    <td className={TD}>
                      <Badge tone={fill.side === 'BUY' ? 'accent' : 'warn'}>{fill.side}</Badge>
                    </td>
                    <td className={`${TD} max-w-[240px]`}>
                      <div className="truncate text-xs font-medium" title={fill.market_id}>
                        {fill.market_id}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-muted" title={fill.token_id}>
                        {fill.token_id}
                      </div>
                    </td>
                    <td className={`${TD} tnum`}>{number(fill.filled_shares, 2)}</td>
                    <td className={`${TD} tnum`}>{number(fill.avg_price_usd_per_share, 4)}</td>
                    <td className={`${TD} tnum`}>{money(fill.fill_cost_usd)}</td>
                    <td className={`${TD} tnum`}>{money(fill.fees_usd, 4)}</td>
                    <td className={`${TD} text-xs`}>{fill.complete ? 'COMPLETE' : 'PARTIAL'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={fillPage?.status === 'unavailable' ? 'N/A' : '暂无模拟成交'}
            reason={fillPage?.status === 'unavailable' ? fillPage.reason : undefined}
            compact
            testId="paper-fills-empty"
          />
        )}
        <div className="flex items-center justify-between border-t border-line px-4 py-3 text-xs text-muted">
          <span className="tnum">第 {cursorHistory.length + 1} 页 · 每页 20 条</span>
          <div className="flex gap-2">
            <button
              type="button"
              className="rounded-tile border border-line px-3 py-1.5 disabled:opacity-40"
              disabled={cursorHistory.length === 0}
              onClick={previousPage}
              data-testid="paper-fills-prev"
            >
              上一页
            </button>
            <button
              type="button"
              className="rounded-tile border border-line px-3 py-1.5 disabled:opacity-40"
              disabled={!fillPage?.next_cursor}
              onClick={nextPage}
              data-testid="paper-fills-next"
            >
              下一页
            </button>
          </div>
        </div>
      </Card>
    </>
  )
}
