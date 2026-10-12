/**
 * System: Collector and Shadow side by side, with trends over the window this
 * dashboard process has actually observed. Metrics the producers do not
 * publish are shown as N/A with a reason - never as an invented number.
 */
import { usePoll } from '../api/client'
import { Sparkline } from '../components/charts'
import { FreshnessPill, StatusChip, ModeBadge } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Banner, Card, EmptyState } from '../components/ui'
import { fmtAge, fmtAgeShort, fmtBytes, fmtDateTime, fmtInt } from '../format'
import type { SystemData } from '../types'

function TrendTile({
  title,
  points,
  naReason,
  color,
}: {
  title: string
  points?: { t: string; v: number }[]
  naReason?: string
  color?: string
}) {
  return (
    <div className="rounded-tile border border-line p-3" data-testid={`trend-${title}`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-ink">{title}</span>
        <span className="text-[10px] text-muted">本进程观测窗口</span>
      </div>
      {points && points.length >= 2 ? (
        <Sparkline points={points} color={color} testId={`sparkline-${title}`} />
      ) : (
        <div className="flex h-[56px] items-center justify-center rounded-tile bg-canvas px-2 text-center text-[11px] text-muted">
          N/A · {naReason ?? '暂未接入'}
        </div>
      )}
    </div>
  )
}

function MetricTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-tile border border-line px-3.5 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="tnum mt-1 text-lg font-semibold text-ink">{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </div>
  )
}

export default function System() {
  const { envelope, error } = usePoll<SystemData>('/api/v1/system', 5000)
  const data = envelope?.data

  const collector = data?.collector
  const shadow = data?.shadow
  const report = data?.report
  const trends = data?.trends ?? {}
  const stats = collector?.stats ?? {}
  const lastCycle = shadow?.last_cycle ?? null
  const dq = report?.data_quality ?? null
  const reasons = (report?.decisions?.primary_reasons ?? {}) as Record<string, number>

  return (
    <>
      <PageHeader
        title="系统监控"
        english="System"
        subtitle="Collector 与 Shadow 的运行状态"
        envelope={envelope}
        right={
          <>
            <StatusChip
              value={collector?.health_status ?? 'unknown'}
              label={`Collector ${(collector?.health_status ?? 'UNKNOWN').toUpperCase()}`}
              testId="sys-collector-chip"
            />
            <StatusChip
              value={
                collector?.present === false
                  ? 'missing'
                  : (shadow?.mode ?? '') === 'REPLAY'
                    ? 'replay'
                    : (shadow?.mode ?? '') === 'LIVE'
                      ? 'live'
                      : (shadow?.health_status ?? 'unknown')
              }
              label={
                shadow?.present === false
                  ? 'Shadow MISSING'
                  : `Shadow ${(shadow?.mode ?? 'UNKNOWN').toUpperCase()}`
              }
              testId="sys-shadow-chip"
            />
          </>
        }
      />

      {!envelope && error && (
        <Card>
          <EmptyState title="无法获取系统数据" reason={error} testId="system-error" />
        </Card>
      )}

      {data && (
        <div className="space-y-6">
          {/* ---------------- Collector ---------------- */}
          <Card
            title="Collector"
            hint="采集器：市场发现 / REST L2 / WebSocket / JSONL 归档 / DuckDB"
            actions={<FreshnessPill freshness={{ state: collector?.present ? (envelope!.freshness.state === 'missing' ? 'missing' : envelope!.freshness.state) : 'missing', age_seconds: envelope!.freshness.age_seconds }} />}
          >
            {collector?.present === false ? (
              <EmptyState
                title="Collector 心跳缺失"
                reason={collector.reason}
                testId="collector-missing"
              />
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
                  <MetricTile label="健康状态" value={(collector?.health_status ?? '—').toUpperCase()} sub={collector?.health_reasons?.[0]} />
                  <MetricTile label="运行时间" value={fmtAgeShort(collector?.uptime_seconds)} sub={`v${collector?.app_version ?? '—'}`} />
                  <MetricTile label="上游联系" value={fmtAge(collector?.upstream_contact_age_seconds)} sub="last_upstream_contact" />
                  <MetricTile label="WS 连接" value={collector?.ws_connected ? 'CONNECTED' : 'DOWN'} sub={`最近消息 ${fmtAge(collector?.ws_message_age_seconds)}`} />
                  <MetricTile label="REST 连续失败" value={fmtInt(collector?.rest_consecutive_failures ?? 0)} sub={`最近成功 ${fmtAge(collector?.rest_success_age_seconds)}`} />
                  <MetricTile label="订阅 Token" value={fmtInt(collector?.subscribed_tokens ?? 0)} />
                </div>

                <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
                  <MetricTile label="REST ok" value={fmtInt(stats['rest_requests_ok'] ?? 0)} />
                  <MetricTile label="REST failed" value={fmtInt(stats['rest_requests_failed'] ?? 0)} />
                  <MetricTile label="429 / rate waits" value={fmtInt(stats['rate_limit_waits'] ?? 0)} />
                  <MetricTile label="retries" value={fmtInt(stats['retries'] ?? 0)} />
                  <MetricTile label="WS messages" value={fmtInt(stats['ws_messages_received'] ?? 0)} />
                  <MetricTile label="WS reconnects" value={fmtInt(stats['ws_reconnects'] ?? 0)} />
                  <MetricTile label="errors total" value={fmtInt(stats['errors_total'] ?? 0)} />
                  {collector?.telemetry?.ws_queue ? (
                    <>
                      <MetricTile
                        label="WS 队列深度"
                        value={`${fmtInt(collector.telemetry.ws_queue.depth)} / ${fmtInt(collector.telemetry.ws_queue.capacity)}`}
                        sub="depth / capacity"
                      />
                      <MetricTile
                        label="WS 队列高水位"
                        value={fmtInt(collector.telemetry.ws_queue.high_water)}
                        sub="自启动以来"
                      />
                      <MetricTile
                        label="WS 丢帧"
                        value={fmtInt(collector.telemetry.ws_queue.frames_dropped ?? 0)}
                        sub="frames_dropped"
                      />
                      {collector.telemetry.rest_latency_last_sweep_ms != null && (
                        <MetricTile
                          label="REST sweep 延迟"
                          value={`${Number(collector.telemetry.rest_latency_last_sweep_ms).toFixed(0)} ms`}
                          sub="最近一次 sweep"
                        />
                      )}
                    </>
                  ) : null}
                </div>

                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <MetricTile label="数据库大小" value={fmtBytes(collector?.db_bytes)} sub="weatherquant.duckdb" />
                  <MetricTile label="原始归档" value={fmtBytes(collector?.raw_bytes)} sub="raw/*.jsonl" />
                  <MetricTile
                    label="磁盘余量"
                    value={`${(collector?.disk_free_percent ?? 0).toFixed(1)}%`}
                    sub={`共 ${fmtBytes(collector?.disk_total_bytes)}`}
                  />
                  <MetricTile label="数据变更" value={fmtAge(collector?.data_change_age_seconds)} sub="last_data_change" />
                </div>

                {/* trends */}
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  <TrendTile
                    title="REST 请求失败（累计）"
                    points={trends['rest_requests_failed']}
                    color="#D66C6C"
                    naReason="采样中"
                  />
                  <TrendTile
                    title="WS 队列积压"
                    points={trends['ws_queue_depth']}
                    color="#D6A044"
                    naReason={
                      collector?.telemetry?.ws_queue
                        ? '采样中'
                        : 'Collector 心跳未包含队列字段'
                    }
                  />
                  <TrendTile
                    title="Shadow 周期耗时 (ms)"
                    points={trends['cycle_duration_ms']}
                    color="#239B78"
                    naReason="等待周期采样"
                  />
                  <TrendTile
                    title="STALE_ORDERBOOK 占比"
                    naReason="report 未提供该时序"
                  />
                </div>

                {(collector?.last_error || collector?.fatal_error) && (
                  <Banner tone="danger" title="最近错误" testId="collector-error-banner">
                    {collector.fatal_error ?? collector.last_error}
                    {collector.last_error_utc && ` · ${fmtDateTime(collector.last_error_utc)}`}
                  </Banner>
                )}
              </div>
            )}
          </Card>

          {/* ---------------- Shadow ---------------- */}
          <Card
            title="Shadow"
            hint="预测 / 定价 / 决策观察引擎（只读，无交易）"
            actions={
              <span className="flex items-center gap-2">
                {shadow?.mode && <ModeBadge mode={shadow.mode} />}
                <StatusChip
                  value={shadow?.health_status ?? 'unknown'}
                  label={(shadow?.health_status ?? 'UNKNOWN').toUpperCase()}
                />
              </span>
            }
          >
            {shadow?.present === false ? (
              <EmptyState title="Shadow 心跳缺失" reason={shadow.reason} testId="shadow-missing" />
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
                  <MetricTile label="模式 mode" value={(shadow?.mode ?? '—').toUpperCase()} sub={`configured ${shadow?.configured_mode ?? '—'}`} />
                  <MetricTile label="运行时间" value={fmtAgeShort(shadow?.uptime_seconds)} sub={`cycles ${fmtInt(shadow?.cycles ?? 0)}`} />
                  <MetricTile label="当前周期" value={fmtInt((lastCycle as any)?.decisions ?? 0)} sub={`评估 · 耗时 ${fmtInt((lastCycle as any)?.duration_ms)} ms`} />
                  <MetricTile label="归档行读取" value={fmtInt((lastCycle as any)?.lines_read ?? 0)} sub={`books ${fmtInt((lastCycle as any)?.books ?? 0)} · markets ${fmtInt((lastCycle as any)?.markets ?? 0)}`} />
                  <MetricTile label="最近决策" value={fmtAge(shadow?.last_decision_age_seconds)} sub="last_decision_at" />
                  <MetricTile label="主模型" value={shadow?.primary_model ?? '—'} sub={`阈值 ${shadow?.thresholds_version ?? '—'}`} />
                </div>

                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <MetricTile
                    label="预报获取 (last cycle)"
                    value={fmtInt((lastCycle as any)?.forecasts_fetched ?? 0)}
                    sub={`predictions ${fmtInt((lastCycle as any)?.predictions ?? 0)}`}
                  />
                  <MetricTile
                    label="订单簿状态"
                    value={
                      report?.liveness?.latest_orderbook_venue_age_seconds != null
                        ? fmtAge(Number(report.liveness.latest_orderbook_venue_age_seconds))
                        : 'N/A'
                    }
                    sub="latest venue book"
                  />
                  <MetricTile
                    label="Collector 静默"
                    value={
                      report?.liveness?.collector_silence_seconds != null
                        ? fmtAge(Number(report.liveness.collector_silence_seconds))
                        : fmtAge(
                            shadow?.collector_last_seen_utc
                              ? (Date.now() -
                                  new Date(String(shadow.collector_last_seen_utc).replace(' ', 'T'))
                                    .getTime()) /
                                  1000
                              : null,
                          )
                    }
                    sub="shadow 视角"
                  />
                  <MetricTile
                    label="数据库大小"
                    value="N/A"
                    sub="Shadow 心跳未提供该字段（暂未接入）"
                  />
                </div>

                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <div className="rounded-tile border border-line p-3.5">
                    <div className="mb-2 text-xs font-medium text-ink">数据质量 data quality</div>
                    <div className="grid grid-cols-3 gap-2">
                      <MetricTile label="data gaps" value={fmtInt(dq?.data_gaps ?? 0)} />
                      <MetricTile label="errors" value={fmtInt(dq?.errors ?? 0)} />
                      <MetricTile label="事件 kinds" value={fmtInt(Object.keys(dq?.by_kind ?? {}).length)} />
                    </div>
                  </div>
                  <div className="rounded-tile border border-line p-3.5">
                    <div className="mb-2 text-xs font-medium text-ink">主要拒绝原因 top reasons</div>
                    {Object.keys(reasons).length > 0 ? (
                      <ul className="space-y-1.5 text-xs">
                        {Object.entries(reasons)
                          .sort((a, b) => b[1] - a[1])
                          .slice(0, 5)
                          .map(([reason, count]) => (
                            <li key={reason} className="flex justify-between gap-3">
                              <span className="text-muted">{reason}</span>
                              <span className="tnum font-semibold">{fmtInt(count)}</span>
                            </li>
                          ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-muted">暂无（report 未发布或为空）</p>
                    )}
                  </div>
                </div>

                {report?.warnings?.length ? (
                  <Banner tone="warn" title="Shadow report 警告" testId="report-warnings">
                    <ul className="list-inside list-disc space-y-1">
                      {report.warnings.slice(0, 3).map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </Banner>
                ) : null}
              </div>
            )}
          </Card>

          {/* ---------------- notes ---------------- */}
          <Banner tone="info" title="指标口径说明" testId="system-notes">
            <ul className="list-inside list-disc space-y-1">
              {data.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          </Banner>
        </div>
      )}
    </>
  )
}
