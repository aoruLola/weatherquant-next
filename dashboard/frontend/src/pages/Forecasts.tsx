/**
 * Forecasts: ECMWF / GFS / Fusion / Calibrated outputs for KLGA.
 *
 * Point-in-time discipline: every distribution carries the run it came from
 * and the model's training cutoff, and actual-observation data is explicitly
 * marked as not yet available - never backfilled into "what was known then".
 */
import { useMemo, useState } from 'react'
import { usePoll } from '../api/client'
import { ProbabilityBars, type Series } from '../components/charts'
import { Badge, FreshnessPill } from '../components/status'
import PageHeader from '../components/PageHeader'
import { Banner, Card, EmptyState, SectionTitle, StatCard } from '../components/ui'
import { fmtDateTime, fmtInt, fmtPct } from '../format'
import type { ForecastsData } from '../types'

const MODEL_COLORS = ['#239B78', '#5B8DEF', '#D6A044', '#8B9AB8', '#D66C6C']

function bucketOrder(labels: string[][]): string[] {
  const seen: string[] = []
  for (const list of labels) {
    for (const label of list) if (!seen.includes(label)) seen.push(label)
  }
  return seen
}

export default function Forecasts() {
  const { envelope, error } = usePoll<ForecastsData>('/api/v1/forecasts', 30000)
  const data = envelope?.data

  const [selectedModels, setSelectedModels] = useState<string[]>([])
  const [targetDate, setTargetDate] = useState<string>('')

  const datesWithDistributions = useMemo(
    () => [...new Set((data?.distributions ?? []).map((d) => d.target_date ?? ''))].filter(Boolean),
    [data],
  )
  const activeDate =
    targetDate || datesWithDistributions[0] || data?.target_dates[0] || ''

  const activeDistributions = (data?.distributions ?? []).filter(
    (d) => !activeDate || d.target_date === activeDate,
  )
  const modelNames = [...new Set(activeDistributions.map((d) => d.model))]
  const selection =
    selectedModels.length > 0
      ? selectedModels.filter((m) => modelNames.includes(m))
      : modelNames.length > 0
        ? [data?.models.find((m) => m.is_primary)?.name ?? modelNames[0]]
        : []

  const orderedLabels = bucketOrder(activeDistributions.map((d) => d.buckets.map((b) => b.label)))
  const series: Series[] = selection
    .map((model, i) => {
      const dist = activeDistributions.find((d) => d.model === model)
      if (!dist) return null
      const byLabel = new Map(dist.buckets.map((b) => [b.label, b.probability]))
      return {
        name: model,
        color: MODEL_COLORS[i % MODEL_COLORS.length],
        points: orderedLabels.map((label) => ({
          label,
          value: byLabel.get(label) ?? 0,
        })),
      }
    })
    .filter((s): s is Series => s !== null)

  const selectedDist = activeDistributions.find((d) => d.model === selection[0])
  const coverage = data?.coverage ?? null

  const toggleModel = (model: string) => {
    setSelectedModels((prev) => {
      const base = prev.length > 0 ? prev : selection
      return base.includes(model)
        ? base.filter((m) => m !== model)
        : [...base, model]
    })
  }

  return (
    <>
      <PageHeader
        title="天气预测"
        english="Forecasts"
        subtitle={`${data?.station ?? 'KLGA'} · 各模型最高气温概率分布`}
        envelope={envelope}
        right={
          <>
            {data && data.target_dates.length > 0 && (
              <span className="tnum text-xs text-muted">
                目标日 {data.target_dates.join(' / ')}
              </span>
            )}
            <Badge tone="accent" title="决策使用的主模型">
              primary: {data?.models.find((m) => m.is_primary)?.name ?? '—'}
            </Badge>
          </>
        }
      />

      {!envelope && error && (
        <Card>
          <EmptyState title="无法获取预测数据" reason={error} testId="forecasts-error" />
        </Card>
      )}

      {data && (
        <div className="space-y-5">
          {/* model cards */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {data.models.map((m) => (
              <StatCard
                key={m.name}
                testId={`model-card-${m.name}`}
                label={m.name}
                value={fmtInt(m.predictions)}
                tone={m.is_primary ? 'accent' : 'neutral'}
                sub={
                  <>
                    {m.is_primary && <Badge tone="accent">PRIMARY</Badge>}{' '}
                    <span className="tnum">
                      最近 run {fmtDateTime(m.latest_run_utc)}
                    </span>
                  </>
                }
              />
            ))}
            {data.models.length === 0 && (
              <Card className="sm:col-span-2 xl:col-span-4">
                <EmptyState
                  compact
                  title="暂无模型信息"
                  reason={data.reason ?? 'Shadow report 尚未发布。'}
                  testId="models-empty"
                />
              </Card>
            )}
          </div>

          {/* the main chart */}
          <Card
            title="Temperature Probability Distribution"
            hint="X 轴：温度区间（°F） · Y 轴：概率（%）"
            actions={
              datesWithDistributions.length > 1 ? (
                <select
                  className="h-8 rounded-tile border border-line bg-surface px-2 text-xs"
                  value={activeDate}
                  onChange={(e) => setTargetDate(e.target.value)}
                  aria-label="目标日期"
                >
                  {datesWithDistributions.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              ) : undefined
            }
          >
            {modelNames.length > 0 ? (
              <>
                <div className="mb-3 flex flex-wrap gap-2" data-testid="model-toggles">
                  {modelNames.map((model) => {
                    const active = selection.includes(model)
                    return (
                      <button
                        key={model}
                        type="button"
                        onClick={() => toggleModel(model)}
                        className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                          active
                            ? 'border-accent bg-accent-soft text-accent'
                            : 'border-line bg-surface text-muted hover:border-accent/50'
                        }`}
                        data-testid={`toggle-${model}`}
                        aria-pressed={active}
                      >
                        {model}
                      </button>
                    )
                  })}
                </div>
                <ProbabilityBars series={series} testId="forecast-chart" />
                <div className="mt-3 grid grid-cols-1 gap-2 border-t border-line pt-3 text-xs text-muted sm:grid-cols-3">
                  <div>
                    预测获取时间（run）：{' '}
                    <span className="tnum text-ink">
                      {fmtDateTime(selectedDist?.forecast_run_time_utc)}
                    </span>
                  </div>
                  <div>
                    训练截止：{' '}
                    <span className="tnum text-ink">
                      {fmtDateTime(selectedDist?.training_cutoff_utc)}
                    </span>
                  </div>
                  <div>
                    预测最大温度：{' '}
                    <span className="tnum text-ink">
                      {selectedDist?.predicted_max_f != null
                        ? `${selectedDist.predicted_max_f.toFixed(1)}°F`
                        : '—'}
                    </span>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState
                title="概率分布暂未接入"
                reason={
                  data.distributions_reason ??
                  '需要 Shadow 发布 forecast-snapshot；接入前此处不展示任何模拟数据。'
                }
                testId="forecast-empty"
              />
            )}
          </Card>

          {/* coverage + observation honesty */}
          <div>
            <SectionTitle hint="来自 shadow report coverage">覆盖与数据状态</SectionTitle>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
              <StatCard
                label="预报覆盖 forecast coverage"
                value={
                  coverage?.forecast_coverage != null
                    ? fmtPct(Number(coverage.forecast_coverage))
                    : '—'
                }
                sub={
                  coverage
                    ? `${coverage.forecast_available ?? '—'} / ${coverage.forecast_needed ?? '—'}`
                    : 'report 未提供'
                }
              />
              <StatCard
                label="盘口覆盖 orderbook coverage"
                value={
                  coverage?.orderbook_coverage != null
                    ? fmtPct(Number(coverage.orderbook_coverage))
                    : '—'
                }
                sub={
                  coverage
                    ? `${coverage.open_markets_with_a_book ?? '—'} / ${coverage.open_markets ?? '—'} 个活跃市场`
                    : 'report 未提供'
                }
              />
              <StatCard
                label="已训练模型 versions"
                value={fmtInt(coverage?.models_tracked?.length ?? data.models.length)}
                sub={(coverage?.models_tracked ?? data.models.map((m) => m.name)).join(', ')}
              />
            </div>
          </div>

          <Banner tone="warn" title="实际观测与结算数据暂未接入" testId="observation-banner">
            {data.observation_reason}。图中展示的全部是“当时可用的预测”，不会混入未来才发生的观测。
          </Banner>

          <div className="flex items-center gap-2">
            <FreshnessPill freshness={envelope!.freshness} />
            <span className="text-xs text-muted">
              数据来源：forecast-snapshot（如已接入）+ shadow report 聚合
            </span>
          </div>
        </div>
      )}
    </>
  )
}
