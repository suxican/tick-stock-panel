import { useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import type { MarketGamePsychology } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { cn } from '@/lib/cn'

const palette = { risk_appetite: '#3b82f6', panic_pressure: '#f59e0b', profit_pressure: '#a855f7', repair_support: '#14b8a6' }
const panel = 'min-w-0 rounded-card border border-border bg-surface/80 p-4'
const scoreText = (value: number | null) => value == null ? '—' : value.toFixed(1)
const deltaText = (value: number | null) => value == null ? '较前日无法比较' : `较前日 ${value > 0 ? '+' : ''}${value.toFixed(1)} 分`
function facts(items: string[]) { return items.length ? <ul className="space-y-1">{items.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>证据不足，暂不判断。</p> }
function metricValue(value: number | null, unit: string) {
  if (value == null) return '—'
  if (unit === 'ratio' || unit === 'return') return `${(value * 100).toFixed(2)}%`
  return `${value.toLocaleString('zh-CN', { maximumFractionDigits: 3 })}${unit === 'yuan' ? ' 元' : ''}`
}

export function PsychologyPanel({ psychology }: { psychology?: MarketGamePsychology | null }) {
  const [selected, setSelected] = useState<string | null>(null)
  const theme = useChartTheme()
  const active = psychology?.dimensions.find(item => item.id === selected) ?? psychology?.dimensions[0]
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    color: psychology?.dimensions.map(item => palette[item.id]),
    tooltip: { trigger: 'axis', backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder, textStyle: { color: theme.tooltipText } },
    legend: { top: 0, textStyle: { color: theme.text, fontSize: 11 } },
    grid: { left: 35, right: 15, top: 45, bottom: 35 },
    xAxis: { type: 'category', boundaryGap: false, data: psychology?.history.map(item => item.date), axisLabel: { color: theme.text, fontSize: 10 }, axisLine: { lineStyle: { color: theme.border } } },
    yAxis: { type: 'value', min: 0, max: 100, interval: 25, axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { lineStyle: { color: theme.grid } } },
    series: psychology?.dimensions.map(item => ({ name: item.label, type: 'line' as const, showSymbol: false, connectNulls: false, smooth: false, lineStyle: { width: 2 }, data: psychology.history.map(row => row[item.id]) })),
  }), [psychology, theme])
  if (!psychology) return <section aria-label="情绪多维刻度" className={panel}><h2 className="text-sm font-semibold">情绪与心理刻度尚未记录</h2><p className="mt-2 text-sm leading-relaxed text-secondary">这份旧报告没有四维评分及其历史。可查看下方冻结的行为假设，或重新生成计划获取新分析。</p></section>
  const hasHistory = psychology.history.some(row => psychology.dimensions.some(dimension => row[dimension.id] != null))
  return <>
    <section aria-label="情绪多维刻度" className={panel}>
      <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">情绪多维刻度</h2><span className="text-xs text-secondary">市场行为代理 · 0–100 分</span></div>
      <p className="mt-2 max-w-prose text-xs leading-relaxed text-secondary">{psychology.summary}</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{psychology.dimensions.map(dimension => <button key={dimension.id} type="button" aria-pressed={active?.id === dimension.id} onClick={() => setSelected(dimension.id)} className={cn('min-w-0 rounded-btn border p-3 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', active?.id === dimension.id ? 'border-accent/70 bg-accent/5' : 'border-border bg-base/40 hover:bg-elevated')}>
        <div className="flex items-center justify-between gap-2 text-xs"><span className="font-medium">{dimension.label}</span><span className="text-secondary">{dimension.level}</span></div>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-2"><span className="font-mono text-2xl font-semibold">{scoreText(dimension.score)}</span><span className="text-xs text-secondary">{deltaText(dimension.delta)}</span></div>
        <div role="meter" aria-label={`${dimension.label}评分`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={dimension.score ?? undefined} aria-valuetext={dimension.score == null ? '证据不足，无法评分' : `${dimension.score} 分`} className="mt-3 h-1.5 overflow-hidden rounded-full bg-elevated">{dimension.score != null && <div className="h-full rounded-full" style={{ width: `${Math.max(0, Math.min(100, dimension.score))}%`, backgroundColor: palette[dimension.id] }} />}</div>
        <div className="mt-1 flex justify-between font-mono text-[10px] text-secondary"><span>0</span><span>50</span><span>100</span></div>
        <p className="mt-2 text-xs text-secondary">样本 {dimension.sample_size.toLocaleString()} · 覆盖 {(dimension.coverage * 100).toFixed(0)}%</p>
      </button>)}</div>
      <p className="mt-3 text-xs leading-relaxed text-secondary">分数表示该维度的强度，不是胜率。恐慌或获利了结压力越高，并不代表机会越大；这些指标无法识别真实散户身份。</p>
    </section>
    <div className="grid items-start gap-4 xl:grid-cols-[1.15fr_1fr]">
      <section aria-label="心理刻度历史" className={panel}><h2 className="text-sm font-semibold">心理刻度历史</h2><p className="mt-1 text-xs leading-relaxed text-secondary">仅展示报告冻结的历史序列；缺值断开，不补零，不插值。</p>
        {hasHistory ? <ReactECharts option={option} notMerge style={{ height: 290 }} opts={{ renderer: 'svg' }} /> : <p className="flex h-52 items-center justify-center text-sm text-secondary">可核验历史不足，暂不绘制曲线。</p>}
        {hasHistory && <details className="text-xs"><summary className="min-h-8 cursor-pointer py-2 text-secondary">查看历史数值</summary><div className="max-h-56 overflow-auto"><table className="w-full min-w-[430px] text-left"><caption className="sr-only">报告冻结的心理刻度历史数值</caption><thead><tr><th scope="col" className="py-2 font-medium">日期</th>{psychology.dimensions.map(item => <th key={item.id} scope="col" className="py-2 font-medium">{item.label}</th>)}</tr></thead><tbody>{psychology.history.map(row => <tr key={row.date} className="border-t border-border"><td className="py-2 font-mono">{row.date}</td>{psychology.dimensions.map(item => <td key={item.id} className="py-2 font-mono">{scoreText(row[item.id])}</td>)}</tr>)}</tbody></table></div></details>}
      </section>
      {active && <section aria-label={`${active.label}解释`} className={panel}>
        <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">{active.label} · 证据拆解</h2><span className="text-xs text-secondary">点击上方刻度切换</span></div>
        <dl className="mt-4 space-y-3 text-xs leading-relaxed">
          <div><dt className="mb-1 font-medium">可观察事实</dt><dd className="text-secondary">{facts(active.facts)}</dd></div>
          <div><dt className="mb-1 font-medium">行为解释</dt><dd className="text-secondary">{active.interpretation}</dd></div>
          <div><dt className="mb-1 font-medium">另一种解释</dt><dd className="text-secondary">{active.alternative}</dd></div>
          <div><dt className="mb-1 font-medium">确认条件</dt><dd className="text-secondary">{facts(active.confirm)}</dd></div>
          <div><dt className="mb-1 font-medium">推翻条件</dt><dd className="text-secondary">{facts(active.invalidation)}</dd></div>
        </dl>
        {active.metrics.length > 0 && <details className="mt-3 border-t border-border pt-2 text-xs"><summary className="min-h-8 cursor-pointer py-2 font-medium">计算指标与分位</summary><dl className="space-y-2">{active.metrics.map(metric => <div key={metric.id} className="flex flex-wrap justify-between gap-2"><dt className="text-secondary">{metric.label}</dt><dd className="font-mono">{metricValue(metric.value, metric.unit)}<span className="ml-2 text-secondary">分位 {metric.percentile == null ? '—' : `${metric.percentile.toFixed(0)}%`}</span></dd></div>)}</dl></details>}
        {active.limitations.length > 0 && <div className="mt-3 text-xs leading-relaxed text-secondary">{facts(active.limitations)}</div>}
      </section>}
    </div>
    {psychology.limitations.length > 0 && <div className="text-xs leading-relaxed text-secondary">{facts(psychology.limitations)}</div>}
  </>
}
