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
  if (['degraded', 'stale', 'replay', 'starting', 'warn', 'one_sided'].includes(v)) return 'warn'
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

  const text =
    state === 'fresh'
      ? `更新于 ${fmtAge(age)}`
      : state === 'stale'
        ? `STALE · ${fmtAge(age)}`
        : state === 'missing'
          ? '数据缺失'
          : state === 'corrupt'
            ? '数据损坏'
            : '暂未接入'

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium ${TONE_CLASS[tone]}`}
      title={freshness.reason ?? undefined}
      data-testid={testId ?? 'freshness-pill'}
      data-state={state}
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
