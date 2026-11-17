/**
 * Export -> Performance Consume -> Publish -> Snapshot, with the exact
 * watermark each verdict came from.
 *
 * The card never fills a gap with a plausible-looking value: a missing
 * watermark, age or threshold renders as N/A, and an unreadable layer renders
 * UNKNOWN with the backend's reason, because "no data" and "zero" are
 * different facts.
 */
import { fmtAgeShort, fmtDateTime } from '../format'
import type {
  Freshness,
  HealthLayerName,
  LayeredHealthData,
  LayerStatus,
  ServiceHealth,
} from '../types'
import { Banner, Card, EmptyState } from './ui'
import { FreshnessPill, StatusChip } from './status'

const LAYER_LABEL: Record<HealthLayerName, string> = {
  export: 'Export 导出',
  performance_consume: 'Performance Consume 摄入',
  publish: 'Publish 发布',
  snapshot: 'Snapshot 快照',
}

const SERVICE_LABEL: Record<string, string> = {
  collector: 'Collector 服务',
  shadow: 'Shadow 服务',
  paper: 'Paper 服务',
}

const SERVICE_ORDER = ['collector', 'shadow', 'paper'] as const

function textOrNA(value: string | null | undefined): string {
  return value && value.length > 0 ? value : 'N/A'
}

function timeOrNA(iso: string | null | undefined): string {
  return iso ? fmtDateTime(iso) : 'N/A'
}

function secondsOrNA(seconds: number | null | undefined): string {
  return typeof seconds === 'number' ? fmtAgeShort(seconds) : 'N/A'
}

function serviceOrNA(
  services: Record<string, ServiceHealth>,
  name: string,
): ServiceHealth {
  return (
    services[name] ?? {
      name,
      status: 'UNKNOWN' as LayerStatus,
      reason: '服务状态未返回（N/A）',
    }
  )
}

export function LayeredHealthCard({
  data,
  freshness,
  now,
  reason,
}: {
  data?: LayeredHealthData
  freshness?: Freshness
  now?: number
  reason?: string | null
}) {
  return (
    <Card
      title="分层健康"
      hint="Export → Performance Consume → Publish → Snapshot · 每层水位来自其自身数据源"
      actions={
        <FreshnessPill
          freshness={freshness}
          now={now}
          testId="layered-health-freshness"
        />
      }
      testId="layered-health-card"
    >
      {!data ? (
        <EmptyState
          compact
          title="N/A · 分层健康暂不可用"
          reason={reason ?? '接口暂未返回分层健康数据'}
          testId="layered-health-unavailable"
        />
      ) : (
        <>
          <div
            className="grid gap-2 sm:grid-cols-3"
            data-testid="layered-services"
          >
            {SERVICE_ORDER.map((name) => {
              const service = serviceOrNA(data.services, name)
              return (
                <div
                  key={name}
                  className="flex items-center justify-between gap-2 rounded-tile border border-line px-3 py-2"
                  data-testid={`service-${name}`}
                >
                  <span className="text-xs font-medium text-ink">
                    {SERVICE_LABEL[name]}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="tnum text-[11px] text-muted">
                      {secondsOrNA(service.age_seconds)}
                    </span>
                    <StatusChip
                      value={service.status}
                      testId={`service-${name}-status`}
                      title={service.reason ?? undefined}
                    />
                  </div>
                </div>
              )
            })}
          </div>

          {data.lagging_layer ? (
            <div className="mt-3">
              <Banner
                tone="warn"
                title={`滞后层：${LAYER_LABEL[data.lagging_layer]}`}
                testId="layered-lagging"
              >
                {textOrNA(data.lagging_basis)}
              </Banner>
            </div>
          ) : null}

          <div className="mt-4 overflow-x-auto rounded-tile border border-line">
            <div className="min-w-[720px]">
              <div className="grid grid-cols-[1.7fr_0.75fr_1.15fr_0.75fr_0.85fr] gap-3 border-b border-line bg-neutral-soft px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-muted">
                <span>层 / 来源</span>
                <span>状态</span>
                <span>Watermark</span>
                <span>Age</span>
                <span>阈值</span>
              </div>
              {data.layers.map((layer) => (
                <div
                  key={layer.name}
                  className="grid grid-cols-[1.7fr_0.75fr_1.15fr_0.75fr_0.85fr] gap-3 border-b border-line px-3 py-2.5 last:border-b-0"
                  data-testid={`layer-${layer.name}`}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink">
                      {LAYER_LABEL[layer.name]}
                    </div>
                    <div
                      className="truncate text-[11px] text-muted"
                      title={layer.source}
                    >
                      {layer.source}
                    </div>
                    {layer.reason ? (
                      <div
                        className={`mt-0.5 text-[11px] ${
                          layer.status === 'OK' ? 'text-muted' : 'text-warn'
                        }`}
                        data-testid={`layer-${layer.name}-reason`}
                      >
                        {layer.reason}
                      </div>
                    ) : null}
                  </div>
                  <div>
                    <StatusChip
                      value={layer.status}
                      testId={`layer-${layer.name}-status`}
                      title={layer.reason ?? undefined}
                    />
                  </div>
                  <div className="tnum text-xs text-ink">
                    {timeOrNA(layer.watermark_utc)}
                  </div>
                  <div className="tnum text-xs text-ink">
                    {secondsOrNA(layer.age_seconds)}
                  </div>
                  <div className="tnum text-xs text-muted">
                    {secondsOrNA(layer.threshold_seconds)}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <p
            className="mt-3 text-[11px] text-muted"
            data-testid="layered-generation"
          >
            generation：{textOrNA(data.generation_id)} ·{' '}
            {textOrNA(data.generation_source)}
          </p>
        </>
      )}
    </Card>
  )
}
