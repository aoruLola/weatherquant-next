/**
 * Status vocabulary: every state string the API can produce has exactly one
 * visual encoding here, so HEALTHY looks the same on every page and REPLAY can
 * never be mistaken for LIVE.
 */
import type { ReactNode } from 'react'
import { fmtAge } from '../format'
import type { Freshness, FreshnessState } from '../types'

type Tone = 'accent' | 'warn' | 'danger' | 'neutral' | 'muted'

const TONE_CLASS: Record<Tone, string> = {
  accent: 'bg-accent-soft text-accent border-accent/30',
  warn: 'bg-warn-soft text-warn border-warn/40',
  danger: 'bg-danger-soft text-danger border-danger/40',
  neutral: 'bg-neutral-soft text-ink border-line',
  muted: 'bg-neutral-soft text-muted border-line',
}

function toneFor(value: string | null | undefined): Tone {
  const v = (value ?? '').toLowerCase()
  if (['healthy', 'live', 'fresh', 'ok', 'connected', 'running'].includes(v)) return 'accent'
  if (['degraded', 'stale', 'replay', 'starting', 'warn', 'one_sided', 'expired'].includes(v))
    return 'warn'
  if (['unhealthy', 'fatal', 'corrupt', 'down', 'failed', 'error'].includes(v)) return 'danger'
  if (['stopped', 'stopped_clean'].includes(v)) return 'muted'
  return 'muted'
}

export function StatusChip({
  value,
  label,
  title,
  testId,
}: {
  value: string | null | undefined
  label?: string
  title?: string
  testId?: string
}) {
  const tone = toneFor(value)
  const text = label ?? (value ? String(value).toUpperCase() : 'UNKNOWN')
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold tracking-wide ${TONE_CLASS[tone]}`}
      title={title}
      data-testid={testId}
      data-state={(value ?? 'unknown').toLowerCase()}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {text}
    </span>
  )
}

const STATE_LABEL: Record<string, string> = {
  ACCEPT: 'ACCEPT',
  REJECT: 'REJECT',
  NO_TRADE: 'NO_TRADE',
  DATA_UNAVAILABLE: 'DATA_UNAVAILABLE',
  CANDIDATE: 'CANDIDATE',
}

/** Decision states have fixed colours: ACCEPT teal, REJECT low-sat orange,
 *  NO_TRADE neutral, data problems red. */
export function DecisionBadge({ state }: { state: string }) {
  const upper = (state ?? '').toUpperCase()
  const tone: Tone =
    upper === 'ACCEPT'
      ? 'accent'
      : upper === 'REJECT'
        ? 'warn'
        : upper === 'NO_TRADE'
          ? 'neutral'
          : upper === 'DATA_UNAVAILABLE'
            ? 'danger'
            : 'muted'
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold ${TONE_CLASS[tone]}`}
      data-testid={`decision-badge-${upper}`}
    >
      {STATE_LABEL[upper] ?? upper}
    </span>
  )
}

export function ModeBadge({ mode }: { mode?: string | null }) {
  const value = (mode ?? 'UNKNOWN').toUpperCase()
  const tone: Tone = value === 'LIVE' ? 'accent' : value === 'REPLAY' ? 'warn' : 'muted'
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold ${TONE_CLASS[tone]}`}
      title={
        value === 'REPLAY'
          ? 'REPLAY：基于历史归档的回放评估，不是当时可做出的决策'
          : undefined
      }
      data-testid="mode-badge"
    >
      {value}
    </span>
  )
}

/** Which layer stopped, in the operator's terms. */
const LAYER_LABEL: Record<NonNullable<Freshness['lagging_layer']>, string> = {
  shadow_export: '导出停摆',
  ingestion: '摄入滞后',
  publication: '发布滞后',
  publish_interval: '发布超期',
}

/**
 * The full layer breakdown, for the tooltip.
 *
 * `snapshot_age` is labelled as an age, never as a fault: with a 20-minute
 * publish cadence a healthy snapshot is routinely 19 minutes old, and the
 * backend only calls it a fault once it passes the publish interval.
 */
function layerTitle(freshness: Freshness): string | undefined {
  const lines: string[] = []
  const lag = (label: string, value: number | null | undefined) => {
    if (typeof value === 'number') lines.push(`${label}：${fmtAge(value)}`)
  }
  if (freshness.lagging_layer) lines.push(`滞后层：${LAYER_LABEL[freshness.lagging_layer]}`)
  lag('导出数据滞后', freshness.export_data_lag_seconds)
  lag('摄入滞后', freshness.ingestion_lag_seconds)
  lag('发布滞后', freshness.publication_lag_seconds)
  lag('快照年龄', freshness.snapshot_age_seconds)
  if (typeof freshness.publish_interval_seconds === 'number') {
    lines.push(`发布间隔：${fmtAge(freshness.publish_interval_seconds)}`)
  }
  if (freshness.reason) lines.push(freshness.reason)
  return lines.length ? lines.join('\n') : undefined
}

export function FreshnessPill({
  freshness,
  now,
  testId,
}: {
  freshness: Freshness | undefined
  now?: number
  testId?: string
}) {
  if (!freshness) return null
  const state: FreshnessState = freshness.state
  // Re-derive age client-side so the pill ticks while the poll interval runs.
  const age =
    freshness.as_of_utc && now
      ? (now - new Date(freshness.as_of_utc).getTime()) / 1000
      : freshness.age_seconds ?? null

  const tone: Tone =
    state === 'fresh'
      ? 'accent'
      : state === 'stale' || state === 'unavailable'
        ? 'warn'
        : state === 'corrupt'
          ? 'danger'
          : 'muted'

  const base =
    state === 'fresh'
      ? `更新于 ${fmtAge(age)}`
      : state === 'stale'
        ? `STALE · ${fmtAge(age)}`
        : state === 'missing'
          ? '数据缺失'
          : state === 'corrupt'
            ? '数据损坏'
            : '暂未接入'

  // Naming the layer is the whole point: "stale" alone sends an operator
  // looking at the wrong service. Only shown when something is behind.
  const text = freshness.lagging_layer
    ? `${base} · ${LAYER_LABEL[freshness.lagging_layer]}`
    : base

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium ${TONE_CLASS[tone]}`}
      title={layerTitle(freshness)}
      data-testid={testId ?? 'freshness-pill'}
      data-state={state}
      data-lagging-layer={freshness.lagging_layer ?? undefined}
    >
      {text}
    </span>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: Tone
  title?: string
}) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold ${TONE_CLASS[tone]}`}
      title={title}
    >
      {children}
    </span>
  )
}
