/**
 * Markets: card-first view of every market the dashboard can see.
 *
 * Cards, not a giant table (per the spec): a market is a unit of attention,
 * and its quote / freshness / model view fit a card far better than a row.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { usePoll } from '../api/client'
import { FreshnessPill, StatusChip } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Card, EmptyState, inputClass, selectClass } from '../components/ui'
import { daysUntil, fmtAge, fmtProb, fmtUsd, fmtUsdDepth } from '../format'
import { useNow } from '../useNow'
import type { MarketSummary, MarketsData } from '../types'

/**
 * What the page opens on, and what "clear filters" returns to.
 *
 * The archive tail also carries markets that closed months ago. Defaulting to
 * `tradeable` - and marking the ones whose New York day has already ended -
 * is how this page avoids presenting expired and current markets as one list.
 */
const DEFAULT_STATUS = 'tradeable'

/**
 * Current open state comes from the server's timezone-aware schedule check.
 * `open_now === null` means the inputs were insufficient: show UNKNOWN rather
 * than inheriting the venue's `tradeable` flag as if it were a clock.
 */
function openState(market: MarketSummary): {
  value: string
  label: string
  title: string
} {
  if (market.open_now === true) {
    return {
      value: 'live',
      label: 'OPEN',
      title: `依据 ${market.open_now_source ?? 'market schedule'}；开始时刻含、结束时刻不含`,
    }
  }
  if (market.open_now === false) {
    return {
      value: 'stopped',
      label: 'CLOSED',
      title: market.open_now_reason ?? '真实交易时段之外或交易所不允许下单',
    }
  }
  return {
    value: 'unknown',
    label: 'UNKNOWN',
    title:
      market.open_now_reason ??
      '缺少带时区的真实起止时间，无法判断当前是否开放（不会默认开放）',
  }
}

function MarketCard({ market }: { market: MarketSummary }) {
  const book = market.book
  const quoteFresh = Boolean(book?.available && !book.stale)
  const bookAge = book?.age_seconds ?? null
  const open = openState(market)

  return (
    <Link
      to={`/markets/${market.market_id}`}
      className="card group block px-5 py-4 transition-colors hover:border-accent/50"
      data-testid="market-card"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="line-clamp-2 text-sm font-semibold leading-snug text-ink group-hover:text-accent">
          {market.question || market.market_id}
        </h3>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusChip
            value={open.value}
            label={open.label}
            title={open.title}
            testId="market-open-state"
          />
          {market.target_day_ended && (
            <span
              className="text-[10px] text-warn"
              title="目标日在纽约已过去；这是日程信息，不等于交易所的结束时间"
            >
              目标日已过
            </span>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span className="tnum font-medium text-ink">{market.bracket_label || 'N/A'}</span>
        <span className="tnum" title={market.target_date_reason ?? undefined}>
          目标日 {market.target_date ?? 'N/A'}
          {daysUntil(market.target_date) && ` · ${daysUntil(market.target_date)}`}
        </span>
        {market.station && <span className="uppercase">{market.station}</span>}
        <span title={market.rules_reason ?? market.schedule_reason ?? undefined}>
          规则 {market.rules_source ?? 'N/A'}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line pt-3">
        <div>
          <div className="text-[11px] text-muted">YES Bid</div>
          <div className="tnum text-base font-semibold text-ink">
            {book?.available ? fmtUsd(book.best_bid) : 'N/A'}
          </div>
        </div>
        <div>
          <div className="text-[11px] text-muted">YES Ask</div>
          <div className="tnum text-base font-semibold text-ink">
            {book?.available ? fmtUsd(book.best_ask) : 'N/A'}
          </div>
        </div>
        <div>
          <div className="text-[11px] text-muted">Spread</div>
          <div className="tnum text-base font-semibold text-ink">
            {book?.available ? fmtUsd(book.spread) : 'N/A'}
          </div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-tile bg-canvas px-3 py-2">
          <div className="text-muted">可用深度 (Top)</div>
          <div className="tnum mt-0.5 font-medium">
            {book?.available
              ? `B ${fmtUsdDepth(book.bid_depth_usd)} / A ${fmtUsdDepth(book.ask_depth_usd)}`
              : 'N/A'}
          </div>
        </div>
        <div className="rounded-tile bg-canvas px-3 py-2">
          <div className="text-muted">模型概率 / Edge</div>
          <div className="tnum mt-0.5 font-medium">
            {market.model_probability !== null && market.model_probability !== undefined ? (
              <span className="text-accent">{fmtProb(market.model_probability)}</span>
            ) : (
              <span className="text-muted" title={market.model_reason ?? undefined}>
                N/A
              </span>
            )}
            <span className="ml-1.5 text-muted" title="理论净 Edge，非已实现收益">
              {market.net_edge_usd !== null && market.net_edge_usd !== undefined
                ? `${market.net_edge_usd >= 0 ? '+' : ''}${fmtUsd(market.net_edge_usd)}`
                : 'N/A'}
            </span>
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between text-[11px]">
        {book?.available ? (
          <span className={quoteFresh ? 'text-muted' : 'font-medium text-warn'}>
            {quoteFresh
              ? `盘口 ${fmtAge(bookAge)}`
              : `STALE · ${fmtAge(book?.age_seconds)}`}
          </span>
        ) : (
          <span className="text-muted" title={book?.reason ?? undefined}>
            无盘口（{book?.reason ?? '未在归档尾部'}）
          </span>
        )}
        <span className="text-muted opacity-0 transition-opacity group-hover:opacity-100">
          查看详情 →
        </span>
      </div>
    </Link>
  )
}

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(id)
  }, [value, delayMs])
  return debounced
}

export default function Markets() {
  const [date, setDate] = useState('')
  const [status, setStatus] = useState(DEFAULT_STATUS)
  const [book, setBook] = useState('all')
  const [decision, setDecision] = useState('')
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebounced(query, 300)
  const navigate = useNavigate()
  const now = useNow(5000)

  const search = useMemo(() => {
    const params = new URLSearchParams()
    if (date) params.set('date', date)
    if (status !== 'all') params.set('status', status)
    if (book !== 'all') params.set('book', book)
    if (decision) params.set('decision', decision)
    if (debouncedQuery.trim()) params.set('q', debouncedQuery.trim())
    params.set('limit', '200')
    const qs = params.toString()
    return `/api/v1/markets${qs ? `?${qs}` : ''}`
  }, [date, status, book, decision, debouncedQuery])

  const { envelope, error, refresh } = usePoll<MarketsData>(search, 10000)
  const data = envelope?.data
  return (
    <>
      <PageHeader
        title="市场行情"
        english="Markets"
        subtitle="Polymarket · NY Daily High Temperature"
        envelope={envelope}
        right={
          <span className="tnum text-xs text-muted">
            {data ? `${data.total} 个市场` : 'N/A'} · 来源 {data?.source ?? 'N/A'}
          </span>
        }
      />

      <Card className="mb-4" bodyClassName="p-3.5">
        <div className="flex flex-wrap items-center gap-2.5" data-testid="market-filters">
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className={`${inputClass} tnum`}
            aria-label="目标日期"
          />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className={selectClass}
            aria-label="市场状态"
          >
            <option value="all">全部状态</option>
            <option value="tradeable">可交易 tradeable</option>
            <option value="active">活跃 active</option>
            <option value="closed">已关闭 closed</option>
          </select>
          <select
            value={book}
            onChange={(e) => setBook(e.target.value)}
            className={selectClass}
            aria-label="盘口状态"
          >
            <option value="all">全部盘口</option>
            <option value="fresh">新鲜 fresh</option>
            <option value="stale">过期 stale</option>
            <option value="missing">缺失 missing</option>
          </select>
          <select
            value={decision}
            onChange={(e) => setDecision(e.target.value)}
            className={selectClass}
            aria-label="最近决策"
            title="按最近一次 Shadow 决策状态筛选（需要 market-snapshot 快照）"
            data-testid="filter-decision"
          >
            <option value="">最近影子信号（全部）</option>
            <option value="ACCEPT">最近影子信号 ACCEPT</option>
            <option value="REJECT">最近 REJECT</option>
            <option value="NO_TRADE">最近 NO_TRADE</option>
          </select>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索市场 / 区间…"
            className={`${inputClass} w-full sm:w-56`}
            aria-label="搜索"
            data-testid="market-search"
          />
          {(date || status !== DEFAULT_STATUS || book !== 'all' || decision || query) && (
            <button
              type="button"
              onClick={() => {
                setDate('')
                setStatus(DEFAULT_STATUS)
                setBook('all')
                setDecision('')
                setQuery('')
              }}
              className="text-xs text-muted underline hover:text-accent"
            >
              清除筛选
            </button>
          )}
        </div>
      </Card>

      {error && !envelope && (
        <Card>
          <EmptyState
            title="无法连接 Dashboard API"
            reason={`${error}；页面会自动重试。`}
            testId="markets-error"
          />
        </Card>
      )}

      {envelope && data && data.items.length === 0 && (
        <Card>
          <EmptyState
            title="没有符合条件的市场"
            reason={
              (decision && data.source !== 'market-snapshot'
                ? '按决策筛选需要 Shadow 发布 market-snapshot（暂未接入，见数据契约文档）。'
                : null) ??
              data.reason ??
              (data.source_status === 'missing'
                ? '归档未挂载或 Collector 未运行。'
                : '尝试放宽筛选条件。')
            }
            testId="markets-empty"
          />
        </Card>
      )}

      {data && data.items.length > 0 && (
        <>
          {data.source_status !== 'fresh' && (
            <div className="mb-3">
              <FreshnessPill freshness={envelope!.freshness} now={now} />
              <span className="ml-2 text-xs text-muted">{data.reason}</span>
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {data.items.map((m) => (
              <MarketCard key={m.market_id} market={m} />
            ))}
          </div>
          <div className="mt-4 text-center">
            <button
              type="button"
              onClick={() => refresh()}
              className="text-xs text-muted underline hover:text-accent"
            >
              刷新列表
            </button>
          </div>
          <p className="mt-4 text-center text-xs text-muted">
            报价为最近盘口快照，非可执行价格；开放状态按真实起止时间与交易所标志判断，数据不足时为 UNKNOWN。{' '}
            <button type="button" className="underline" onClick={() => navigate('/decisions')}>
              查看决策
            </button>
          </p>
        </>
      )}
    </>
  )
}
