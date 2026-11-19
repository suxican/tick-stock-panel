// 情绪周期新维度: 封板质量 (炸板率 / 早盘封板占比 / 开板率 / 封单额) —— 市场环境页「情绪周期」视图。
// 只展示, 不参与阶段判定; 口径见 docs/superpowers/specs/2026-11-19-seal-quality-design.md。
import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { ShieldCheck } from 'lucide-react'
import { api, type SealQualityPayload, type SealQualityRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

/** 封板质量数据 (与时间轴当日快照共用同一查询)。 */
export function useSealQuality(start?: string, end?: string) {
  return useQuery<SealQualityPayload>({
    queryKey: QK.regimeSealQuality(start, end),
    queryFn: () => api.regimeSealQuality(start, end),
    staleTime: 5 * 60 * 1000,
    enabled: !!start && !!end,
  })
}

const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const yi = (v: number | null | undefined) => (v == null ? '—' : `${(v / 1e8).toFixed(2)}亿`)
const series = (rows: SealQualityRow[], key: 'break_rate' | 'early_share' | 'reopen_rate') =>
  rows.map(r => (r[key] == null ? null : Math.round((r[key] as number) * 1000) / 10))

const TILES: { key: 'break_rate' | 'early_share' | 'reopen_rate' | 'fd_median'; label: string; hint: string; fmt: (v: number | null) => string }[] = [
  { key: 'break_rate', label: '炸板率', hint: '盘中触及涨停但收盘未封住的占比 (收盘是否封住以日线为准)', fmt: pct },
  { key: 'early_share', label: '早盘封板占比', hint: '收盘封住的股中, 首次封板在 10:00 之前的占比', fmt: pct },
  { key: 'reopen_rate', label: '开板率', hint: '收盘封住的股中, 盘中开过板的占比', fmt: pct },
  { key: 'fd_median', label: '封单额中位数', hint: '收盘封住股的封单额中位数', fmt: yi },
]

export function SealQualityCard({ start, end }: { start?: string; end?: string }) {
  const q = useSealQuality(start, end)
  const ct = useChartTheme()
  const rows = useMemo(() => q.data?.rows ?? [], [q.data])
  const latest = q.data?.latest ?? null

  const option = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0) return null
    return {
      animation: false,
      grid: { left: 40, right: 16, top: 28, bottom: 28 },
      tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => (v == null ? '—' : `${v}%`) },
      legend: { top: 0, textStyle: { color: ct.text, fontSize: 11 } },
      xAxis: { type: 'category', data: rows.map(r => r.date), axisLabel: { color: ct.text, fontSize: 10 },
               axisLine: { lineStyle: { color: ct.border } } },
      yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: ct.text, fontSize: 10, formatter: '{value}%' },
               splitLine: { lineStyle: { color: ct.grid } } },
      series: [
        { name: '炸板率', type: 'line', data: series(rows, 'break_rate'), showSymbol: false, lineStyle: { width: 1.5 } },
        { name: '早盘封板占比', type: 'line', data: series(rows, 'early_share'), showSymbol: false, lineStyle: { width: 1.5 } },
        { name: '开板率', type: 'line', data: series(rows, 'reopen_rate'), showSymbol: false, lineStyle: { width: 1.5 } },
      ],
    }
  }, [rows, ct])

  const ref = useRef<HTMLDivElement>(null)
  const inst = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    if (!ref.current || !option) return
    if (!inst.current) {
      inst.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    }
    inst.current.setOption(option, { notMerge: true })
    inst.current.resize()
  }, [option])
  useEffect(() => {
    const el = ref.current
    const observer = typeof ResizeObserver !== 'undefined' && el
      ? new ResizeObserver(() => inst.current?.resize()) : null
    if (observer && el) observer.observe(el)
    return () => {
      observer?.disconnect()
      inst.current?.dispose()
      inst.current = null
    }
  }, [option == null])

  if (!latest || rows.length === 0) return null
  return (
    <div className="regime-panel">
      <div className="regime-section-heading">
        <div className="regime-section-title"><ShieldCheck aria-hidden="true" /><h2>封板质量</h2></div>
        <div className="regime-section-hint">
          最新 {latest.date} · 触板 {latest.touched} 家 · 封住 {latest.sealed} 家 · 只展示, 不参与阶段判定
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {TILES.map(t => {
          const value = latest[t.key]
          const rank = latest.pct?.[t.key]
          return (
            <div key={t.key} data-metric={t.key} title={t.hint}
              className="rounded-md border border-border/60 bg-elevated/30 px-2.5 py-2">
              <div className="text-[11px] text-muted">{t.label}</div>
              <div className="font-mono text-base font-semibold text-foreground">{t.fmt(value)}</div>
              {value != null && rank != null && (
                <div className="text-[10px] text-muted">近一年 {Math.round(rank)} 分位</div>
              )}
            </div>
          )
        })}
      </div>
      <div ref={ref} className="regime-chart" role="img" aria-label="炸板率、早盘封板占比与开板率历史趋势图" />
      <p className="mt-1 text-[11px] text-muted"
        title={`收盘封住的股中有涨停池明细的占比: ${pct(latest.pool_coverage)}; 2020-05 至 2023-11 上游封单额口径异常, 该区间不显示封单额`}>
        数据自 {q.data?.first_date ?? '—'} · 开板率 {q.data?.reopen_from} 起 · 剔除 ST 与上市初期新股 · 收盘是否封住以日线为准
      </p>
    </div>
  )
}
