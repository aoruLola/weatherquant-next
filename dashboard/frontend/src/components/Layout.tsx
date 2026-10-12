/**
 * App shell: narrow left navigation on desktop, an off-canvas drawer on
 * mobile, and the two global notices every page must carry - fixture data and
 * a lost connection to the API.
 */
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  Activity,
  BarChart3,
  CloudSun,
  LayoutDashboard,
  ListChecks,
  Menu,
  WifiOff,
  X,
} from 'lucide-react'
import { usePoll } from '../api/client'
import type { Healthz } from '../types'
import { Banner, RefreshHint } from './ui'

const NAV = [
  { to: '/', label: '总览', en: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/markets', label: '市场行情', en: 'Markets', icon: BarChart3 },
  { to: '/forecasts', label: '天气预测', en: 'Forecasts', icon: CloudSun },
  { to: '/decisions', label: 'Shadow 决策', en: 'Decisions', icon: ListChecks },
  { to: '/system', label: '系统监控', en: 'System', icon: Activity },
]

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1 px-3">
      {NAV.map(({ to, label, en, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            `flex items-center gap-3 rounded-tile px-3 py-2.5 text-sm transition-colors ${
              isActive
                ? 'bg-accent-soft font-semibold text-accent'
                : 'text-muted hover:bg-canvas hover:text-ink'
            }`
          }
          data-testid={`nav-${en.toLowerCase()}`}
        >
          <Icon size={17} className="shrink-0" />
          <span className="flex-1 whitespace-nowrap">{label}</span>
          <span className="hidden shrink-0 text-[10px] uppercase tracking-wide opacity-60 xl:inline">
            {en}
          </span>
        </NavLink>
      ))}
    </nav>
  )
}

function Brand() {
  return (
    <div className="px-6 pt-6 pb-5">
      <div className="text-[17px] font-bold tracking-tight text-ink">WeatherQuant</div>
      <div className="mt-0.5 text-[11px] font-medium uppercase tracking-[0.14em] text-accent">
        Live Operations
      </div>
    </div>
  )
}

export default function Layout() {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const location = useLocation()
  const health = usePoll<Healthz>('/healthz', 15000)

  useEffect(() => setDrawerOpen(false), [location.pathname])

  const dataset = health.envelope?.dataset
  const offline = health.error !== null && health.envelope === null

  return (
    <div className="min-h-screen bg-canvas">
      {/* desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-20 hidden w-[236px] flex-col border-r border-line bg-surface lg:flex">
        <Brand />
        <NavItems />
        <div className="mt-auto px-6 py-5 text-[11px] leading-relaxed text-muted">
          只读监控 · Read-only
          <br />
          不参与交易 / No trading
        </div>
      </aside>

      {/* mobile drawer */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" data-testid="mobile-drawer">
          <div
            className="absolute inset-0 bg-ink/30"
            onClick={() => setDrawerOpen(false)}
            aria-hidden
          />
          <div className="absolute inset-y-0 left-0 flex w-[260px] flex-col bg-surface">
            <div className="flex items-center justify-between pr-3">
              <Brand />
              <button
                type="button"
                aria-label="关闭菜单"
                onClick={() => setDrawerOpen(false)}
                className="rounded-tile p-2 text-muted hover:bg-canvas"
              >
                <X size={18} />
              </button>
            </div>
            <NavItems onNavigate={() => setDrawerOpen(false)} />
          </div>
        </div>
      )}

      <div className="lg:pl-[236px]">
        {/* top bar */}
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-surface/95 px-4 backdrop-blur-sm sm:px-6">
          <button
            type="button"
            aria-label="打开菜单"
            onClick={() => setDrawerOpen(true)}
            className="rounded-tile border border-line p-2 text-muted lg:hidden"
            data-testid="menu-button"
          >
            <Menu size={16} />
          </button>
          <div className="lg:hidden">
            <span className="text-sm font-bold text-ink">WeatherQuant</span>
          </div>
          <div className="hidden items-center gap-2 text-xs text-muted lg:flex">
            {health.envelope ? (
              <>
                <span
                  className={`inline-flex items-center gap-1.5 ${health.error ? 'text-warn' : 'text-accent'}`}
                  data-testid="api-status"
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-current" />
                  Dashboard API {health.error ? '· 重试中' : '已连接'}
                </span>
                <span className="text-line">|</span>
                <span className="tnum">
                  进程运行 {Math.floor((health.envelope.data?.uptime_seconds ?? 0) / 60)} 分钟
                </span>
              </>
            ) : (
              <span className="text-muted" data-testid="api-status">
                正在连接…
              </span>
            )}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {dataset === 'fixture' && (
              <span
                className="rounded-full border border-warn/50 bg-warn-soft px-2.5 py-1 text-[11px] font-bold text-warn"
                data-testid="fixture-badge"
              >
                FIXTURE DATA
              </span>
            )}
            {health.envelope && (
              <span className="tnum hidden text-xs text-muted sm:inline">
                {new Date(health.envelope.generated_at_utc).toLocaleTimeString('zh-CN', {
                  hour12: false,
                })}
              </span>
            )}
            <RefreshHint onClick={health.refresh} />
          </div>
        </header>

        {dataset === 'fixture' && (
          <div className="px-4 pt-4 sm:px-6">
            <Banner tone="warn" title="当前为 FIXTURE 数据" testId="fixture-banner">
              本页面由本地样例数据驱动，仅用于开发与界面验收，不代表生产环境的实时状态。
            </Banner>
          </div>
        )}
        {offline && (
          <div className="px-4 pt-4 sm:px-6">
            <Banner tone="danger" title="无法连接 Dashboard API" testId="offline-banner">
              <span className="inline-flex items-center gap-1.5">
                <WifiOff size={13} /> 与后端的连接中断，正在自动重试；页面显示的是最后一次成功获取的数据（如有）。
              </span>
            </Banner>
          </div>
        )}

        <main className="px-4 pb-14 pt-5 sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
