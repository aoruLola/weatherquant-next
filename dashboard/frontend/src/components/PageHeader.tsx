import type { ReactNode } from 'react'
import { useNow } from '../useNow'
import type { Envelope } from '../types'
import { FreshnessPill } from './status'

/** Page title row: title, status chips and the freshness of what is shown. */
export default function PageHeader({
  title,
  english,
  subtitle,
  right,
  envelope,
  testId,
}: {
  title: string
  english: string
  subtitle?: string
  right?: ReactNode
  envelope?: Envelope<unknown> | null
  testId?: string
}) {
  const now = useNow(1000)
  return (
    <header
      className="mb-5 flex flex-wrap items-end justify-between gap-3"
      data-testid={testId ?? 'page-header'}
    >
      <div>
        <div className="flex items-baseline gap-2.5">
          <h1 className="text-xl font-bold tracking-tight text-ink sm:text-[22px]">{title}</h1>
          <span className="text-xs font-medium uppercase tracking-widest text-muted">
            {english}
          </span>
        </div>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {right}
        {envelope && <FreshnessPill freshness={envelope.freshness} now={now} />}
        {envelope && (
          <span className="tnum text-xs text-muted">
            更新于{' '}
            {new Date(envelope.generated_at_utc).toLocaleTimeString('zh-CN', { hour12: false })}
          </span>
        )}
      </div>
    </header>
  )
}
