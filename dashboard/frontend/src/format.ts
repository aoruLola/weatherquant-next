/** Formatting helpers: one place for how a number is shown. */

export function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return Math.round(value).toLocaleString('zh-CN')
}

export function fmtPct(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** A probability that is already in 0..1 shows as %. */
export function fmtProb(value: number | null | undefined): string {
  return fmtPct(value, 1)
}

export function fmtUsd(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(digits)
}

export function fmtUsdDepth(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value >= 1000) return `$${(value / 1000).toFixed(1)}k`
  return `$${value.toFixed(value >= 100 ? 0 : 1)}`
}

export function fmtBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const s = Math.max(0, seconds)
  if (s < 90) return `${Math.round(s)} 秒前`
  if (s < 5400) return `${Math.round(s / 60)} 分钟前`
  if (s < 86400) return `${(s / 3600).toFixed(1)} 小时前`
  return `${(s / 86400).toFixed(1)} 天前`
}

export function fmtAgeShort(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const s = Math.max(0, seconds)
  if (s < 60) return `${Math.round(s)}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
}

/** Local wall-clock time for an ISO timestamp: 14:03:21 or 10-11 14:03. */
export function fmtTime(iso: string | null | undefined, withDate = false): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  if (!withDate) return `${hh}:${mm}:${ss}`
  const mon = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${mon}-${day} ${hh}:${mm}`
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false })
}

export function daysUntil(date: string | null | undefined): string | null {
  if (!date) return null
  const target = new Date(`${date}T00:00:00`)
  if (Number.isNaN(target.getTime())) return null
  const now = new Date()
  const diff = Math.round(
    (target.getTime() - new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()) /
      86400000,
  )
  if (diff === 0) return '今天'
  if (diff === 1) return '明天'
  if (diff > 1) return `${diff} 天后`
  return `${-diff} 天前`
}
