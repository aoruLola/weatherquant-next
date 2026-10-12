/** Flat-card primitives: the building blocks every page composes from. */
import type { ReactNode } from 'react'
import { AlertTriangle, Inbox, RefreshCw } from 'lucide-react'

export function Card({
  title,
  hint,
  actions,
  children,
  className = '',
  bodyClassName = '',
  testId,
}: {
  title?: ReactNode
  hint?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  testId?: string
}) {
  return (
    <section className={`card ${className}`} data-testid={testId}>
      {(title || hint || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-[15px] font-semibold text-ink">{title}</h2>}
            {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={`p-5 ${bodyClassName}`}>{children}</div>
    </section>
  )
}

export function StatCard({
  label,
  value,
  sub,
  tone = 'neutral',
  hint,
  testId,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: 'neutral' | 'accent' | 'warn' | 'danger'
  hint?: string
  testId?: string
}) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent'
      : tone === 'warn'
        ? 'text-warn'
        : tone === 'danger'
          ? 'text-danger'
          : 'text-ink'
  return (
    <div className="card px-5 py-4" data-testid={testId}>
      <div className="flex items-center gap-1.5">
        <span className="text-xs font-medium text-muted">{label}</span>
        {hint && (
          <span className="text-muted" title={hint}>
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
              <circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" strokeWidth="1" />
              <text x="6" y="8.5" textAnchor="middle" fontSize="7" fill="currentColor">
                ?
              </text>
            </svg>
          </span>
        )}
      </div>
      <div className={`tnum mt-1.5 text-2xl font-semibold leading-none ${toneClass}`}>
        {value}
      </div>
      {sub && <div className="mt-1.5 text-xs text-muted">{sub}</div>}
    </div>
  )
}

export function EmptyState({
  title,
  reason,
  icon,
  compact = false,
  testId,
}: {
  title: string
  reason?: string | null
  icon?: ReactNode
  compact?: boolean
  testId?: string
}) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center ${compact ? 'py-6' : 'py-12'}`}
      data-testid={testId ?? 'empty-state'}
    >
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-neutral-soft text-muted">
        {icon ?? <Inbox size={18} />}
      </div>
      <p className="text-sm font-medium text-ink">{title}</p>
      {reason && <p className="mt-1 max-w-md text-xs leading-relaxed text-muted">{reason}</p>}
    </div>
  )
}

export function Banner({
  tone = 'info',
  title,
  children,
  testId,
}: {
  tone?: 'info' | 'warn' | 'danger' | 'accent'
  title?: ReactNode
  children?: ReactNode
  testId?: string
}) {
  const toneClass =
    tone === 'warn'
      ? 'border-warn/40 bg-warn-soft text-ink'
      : tone === 'danger'
        ? 'border-danger/40 bg-danger-soft text-ink'
        : tone === 'accent'
          ? 'border-accent/40 bg-accent-soft text-ink'
          : 'border-line bg-surface text-ink'
  return (
    <div
      className={`flex items-start gap-2.5 rounded-tile border px-4 py-3 text-sm ${toneClass}`}
      data-testid={testId}
    >
      {tone !== 'info' && (
        <AlertTriangle
          size={16}
          className={`mt-0.5 shrink-0 ${tone === 'danger' ? 'text-danger' : tone === 'warn' ? 'text-warn' : 'text-accent'}`}
        />
      )}
      <div className="min-w-0">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className="mt-0.5 text-xs leading-relaxed text-muted">{children}</div>}
      </div>
    </div>
  )
}

export function SectionTitle({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline gap-2">
      <h2 className="text-sm font-semibold text-ink">{children}</h2>
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </div>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className="tnum mt-0.5 text-sm text-ink">{children}</div>
    </div>
  )
}

export const selectClass =
  'h-9 rounded-tile border border-line bg-surface px-3 text-sm text-ink outline-none focus:border-accent'

export const inputClass =
  'h-9 rounded-tile border border-line bg-surface px-3 text-sm text-ink outline-none placeholder:text-muted focus:border-accent'

export function RefreshHint({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1 rounded-tile border border-line px-2.5 py-1.5 text-xs text-muted transition-colors hover:border-accent hover:text-accent"
    >
      <RefreshCw size={12} /> 刷新
    </button>
  )
}
