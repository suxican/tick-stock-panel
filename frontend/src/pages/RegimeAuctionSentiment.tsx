// 情绪周期新维度: 竞价情绪 (集合竞价 09:25 定版) —— 市场环境页「情绪周期」视图。
// 只展示, 不参与阶段判定; 口径见 docs/superpowers/specs/2026-11-20-auction-sentiment-design.md。
import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Sunrise } from 'lucide-react'
import { api, type AuctionSentimentPayload, type AuctionSentimentRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

/** 竞价情绪数据 (与时间轴当日快照共用同一查询)。 */
export function useAuctionSentiment(start?: string, end?: string) {
  return useQuery<AuctionSentimentPayload>({
    queryKey: QK.regimeAuctionSentiment(start, end),
    queryFn: () => api.regimeAuctionSentiment(start, end),
    staleTime: 5 * 60 * 1000,
    enabled: !!start && !!end,
  })
}

const ratio = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const signed = (v: number | null | undefined) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`)
const count = (v: number | null | undefined) => (v == null ? '—' : String(v))
const times = (v: number | null | undefined) => (v == null ? '—' : `${v.toFixed(2)} 倍`)
const series = (rows: AuctionSentimentRow[], key: 'high_open_ratio' | 'prev_mean_pct') =>
  rows.map(r => (r[key] == null ? null : Math.round((r[key] as number) * 1000) / 10))

type TileKey = 'prev_mean_pct' | 'prev_weak_count' | 'high_open_ratio' | 'amount_ratio_5d'
const TILES: { key: TileKey; label: string; hint: string; fmt: (v: number | null) => string }[] = [
  { key: 'prev_mean_pct', label: '昨涨停竞价均值', hint: '上一交易日收盘封住的股, 今天集合竞价涨幅的平均值 (赚钱效应)', fmt: signed },
  { key: 'prev_weak_count', label: '昨涨停竞价 ≤-3%', hint: '上一交易日收盘封住的股中, 今天竞价跌 3% 以上的家数 (核按钮)', fmt: count },
  { key: 'high_open_ratio', label: '高开占比', hint: '有竞价成交的股中竞价涨幅为正的占比', fmt: ratio },
  { key: 'amount_ratio_5d', label: '竞价成交额', hint: '全市场竞价成交额相对前 5 个交易日均值的倍数', fmt: times },
]

export function AuctionSentimentCard({ start, end }: { start?: string; end?: string }) {
  const q = useAuctionSentiment(start, end)
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
      yAxis: { type: 'value', axisLabel: { color: ct.text, fontSize: 10, formatter: '{value}%' },
               splitLine: { lineStyle: { color: ct.grid } } },
      series: [
        { name: '高开占比', type: 'line', data: series(rows, 'high_open_ratio'), showSymbol: false, lineStyle: { width: 1.5 } },
        { name: '昨涨停竞价均值', type: 'line', data: series(rows, 'prev_mean_pct'), showSymbol: false, lineStyle: { width: 1.5 } },
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
        <div className="regime-section-title"><Sunrise aria-hidden="true" /><h2>竞价情绪</h2></div>
        <div className="regime-section-hint">
          最新 {latest.date} · 昨涨停 {latest.prev_sealed} 家 · 竞价涨停 {count(latest.limit_up_open)} · 跌停 {count(latest.limit_down_open)} · 只展示, 不参与阶段判定
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
      <div ref={ref} className="regime-chart" role="img" aria-label="竞价高开占比与昨日涨停股竞价均值历史趋势图" />
      <p className="mt-1 text-[11px] text-muted" title={`竞价涨幅中位数 ${signed(latest.median_pct)}; 昨涨停竞价高开占比 ${ratio(latest.prev_high_open_ratio)}`}>
        数据自 {q.data?.first_date ?? '—'} · 竞价涨停/跌停 2020 起 · 剔除 ST 与无涨跌幅新股 · 昨日涨停以日线收盘为准
      </p>
    </div>
  )
}
