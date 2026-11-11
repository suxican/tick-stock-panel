/**
 * AI 复盘数据面板 —— 用后端快照(meta.snapshot / 归档 report.snapshot)画表格与图表,
 * AI 正文只写判断与计划, 数字统一在这里呈现, 避免模型转抄出错与重复。
 *
 * 区块: 周期状态条 / 核心指标对比表(含近 10 日迷你走势) / 成交额与涨停走势图 /
 *       涨停主线表 + 涨跌幅居前板块 / 连板梯队。
 */
import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import type { RecapSnapshot, RecapSnapshotMetric } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { useChartTheme } from '@/lib/theme'
import { HistoryChart } from '@/custom/market-game/HistoryChart'

const BULL = '#F04438'
const AMOUNT = 'rgba(59,130,246,0.55)'

function fmtValue(v: number | null | undefined, m: Pick<RecapSnapshotMetric, 'unit' | 'digits'>): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v.toFixed(m.digits)}${m.unit === '%' ? '%' : ''}`
}

/** 变化: 百分比类给差值(pp), 连板高度给差值(板), 其余给相对变化(%)。返回 [文本, 带符号数值]。 */
function fmtChange(today: number | null, base: number | null, unit: string): [string, number | null] {
  if (today == null || base == null) return ['—', null]
  if (unit === '%' || unit === '板') {
    const d = today - base
    return [`${d > 0 ? '+' : ''}${d.toFixed(0)}${unit === '%' ? 'pp' : '板'}`, d]
  }
  if (base === 0) return ['—', null]
  const r = (today - base) / base * 100
  return [`${r > 0 ? '+' : ''}${r.toFixed(0)}%`, r]
}

function Sparkline({ values }: { values: (number | null)[] }) {
  const pts = values.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] != null)
  if (pts.length < 2) return <span className="text-muted">—</span>
  const w = 64, h = 18
  const ys = pts.map(p => p[1])
  const min = Math.min(...ys), max = Math.max(...ys)
  const span = max - min || 1
  const n = values.length - 1 || 1
  const d = pts.map(([i, v]) => `${(i / n * w).toFixed(1)},${(h - 2 - (v - min) / span * (h - 4)).toFixed(1)}`).join(' ')
  const [lx, ly] = d.split(' ').pop()!.split(',')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="overflow-visible" aria-hidden>
      <polyline points={d} fill="none" stroke="currentColor" strokeWidth={1.2} className="text-secondary" />
      <circle cx={lx} cy={ly} r={1.8} className="fill-accent" />
    </svg>
  )
}

function MetricsTable({ metrics, intraday }: { metrics: RecapSnapshotMetric[]; intraday: boolean }) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-[11px] text-muted">
            <th className="py-1 pr-2 text-left font-normal">指标</th>
            <th className="px-2 py-1 text-right font-normal">{intraday ? '盘中' : '今日'}</th>
            <th className="px-2 py-1 text-right font-normal">较昨日</th>
            <th className="px-2 py-1 text-right font-normal">5日均</th>
            <th className="py-1 pl-2 text-right font-normal">近10日</th>
          </tr>
        </thead>
        <tbody>
          {metrics.map(m => {
            const [chg, chgNum] = fmtChange(m.today, m.prev, m.unit)
            return (
              <tr key={m.key} className="border-t border-border/40">
                <td className="whitespace-nowrap py-1.5 pr-2 text-secondary">{m.label}</td>
                <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{fmtValue(m.today, m)}</td>
                <td className={cn('px-2 py-1.5 text-right font-mono tabular-nums', priceColorClass(chgNum))}>{chg}</td>
                <td className="px-2 py-1.5 text-right font-mono tabular-nums text-muted">{fmtValue(m.avg5, m)}</td>
                <td className="py-1.5 pl-2 text-right"><span className="inline-flex justify-end"><Sparkline values={m.series} /></span></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function TrendChart({ snapshot }: { snapshot: RecapSnapshot }) {
  const ct = useChartTheme()
  const amount = snapshot.metrics.find(m => m.key === 'amount_yi')
  const limitUp = snapshot.metrics.find(m => m.key === 'limit_up')
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 44, right: 32, top: 26, bottom: 22 },
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { color: ct.text, fontSize: 11 } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText, fontSize: 11 },
    },
    xAxis: {
      type: 'category',
      data: snapshot.dates.map(d => d.slice(5)),
      axisLine: { lineStyle: { color: ct.border } },
      axisLabel: { color: ct.text, fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: [
      { type: 'value', name: '亿', nameTextStyle: { color: ct.text, fontSize: 10 }, scale: true,
        axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } } },
      { type: 'value', name: '涨停', nameTextStyle: { color: ct.text, fontSize: 10 }, scale: true,
        axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      { name: '成交额', type: 'bar', data: amount?.series.map(v => (v == null ? null : Math.round(v))) ?? [],
        itemStyle: { color: AMOUNT, borderRadius: [2, 2, 0, 0] }, barMaxWidth: 18 },
      { name: '涨停', type: 'line', yAxisIndex: 1, data: limitUp?.series ?? [], smooth: true,
        symbolSize: 4, lineStyle: { color: BULL, width: 1.5 }, itemStyle: { color: BULL } },
    ],
  }), [snapshot, amount, limitUp, ct])
  if (!amount && !limitUp) return null
  return <HistoryChart option={option} height={210} label="近10日成交额与涨停家数走势" />
}

function Mainlines({ snapshot, onOpenStock }: { snapshot: RecapSnapshot; onOpenStock?: (symbol: string) => void }) {
  const { mainlines, sectors } = snapshot
  return (
    <div className="min-w-0 space-y-2">
      {mainlines.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[11px] text-muted">
                <th className="py-1 pr-2 text-left font-normal">涨停主线</th>
                <th className="px-2 py-1 text-right font-normal">涨停</th>
                <th className="px-2 py-1 text-right font-normal">最高</th>
                <th className="px-2 py-1 text-left font-normal">龙头</th>
                <th className="py-1 pl-2 text-right font-normal">持续</th>
              </tr>
            </thead>
            <tbody>
              {mainlines.map(m => (
                <tr key={m.name} className="border-t border-border/40">
                  <td className="max-w-[10rem] truncate py-1.5 pr-2 text-foreground" title={m.name}>{m.name}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bull">{m.limit_up}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{m.max_boards}板</td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    {m.leader ? (
                      <button type="button" onClick={() => onOpenStock?.(m.leader!.symbol)} className="text-secondary hover:text-accent">
                        {m.leader.name}
                      </button>
                    ) : <span className="text-muted">—</span>}
                  </td>
                  <td className={cn('py-1.5 pl-2 text-right tabular-nums', m.streak >= 3 ? 'text-accent' : 'text-muted')}>
                    {m.streak === 1 ? '首日' : `${m.streak}日`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-[11px] text-muted">复盘日涨停主线尚未计算(盘后管道完成后可用)</div>
      )}
      <SectorChips label="涨幅居前" items={sectors.leading} />
      <SectorChips label="跌幅居前" items={sectors.lagging} />
    </div>
  )
}

function SectorChips({ label, items }: { label: string; items: RecapSnapshot['sectors']['leading'] }) {
  if (!items.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1 text-[11px]">
      <span className="mr-1 text-muted">{label}</span>
      {items.map(s => (
        <span key={s.name} className="rounded bg-elevated/60 px-1.5 py-0.5 text-secondary" title={s.leader ? `领涨: ${s.leader}` : undefined}>
          {s.name} <span className={cn('font-mono', priceColorClass(s.pct))}>{s.pct > 0 ? '+' : ''}{s.pct.toFixed(2)}%</span>
        </span>
      ))}
    </div>
  )
}

function Ladder({ snapshot, onOpenStock }: { snapshot: RecapSnapshot; onOpenStock?: (symbol: string) => void }) {
  const tiers = snapshot.ladder.filter(t => t.boards >= 2)
  const first = snapshot.ladder.find(t => t.boards === 1)
  const promo = snapshot.metrics.find(m => m.key === 'promo_rate')
  const perf = snapshot.prev_limit_up
  return (
    <div className="min-w-0 space-y-1.5 text-[12px]">
      <div className="text-[11px] text-muted">连板梯队</div>
      {tiers.length === 0 && <div className="text-[11px] text-muted">无连板</div>}
      {tiers.map(t => (
        <div key={t.boards} className="flex items-start gap-2">
          <span className="w-9 shrink-0 font-mono text-bull">{t.boards}板</span>
          <div className="flex min-w-0 flex-wrap gap-1">
            {t.stocks.map(s => (
              <button key={s.symbol} type="button" onClick={() => onOpenStock?.(s.symbol)}
                className="rounded bg-bull/10 px-1.5 py-0.5 text-[11px] text-foreground hover:bg-bull/20">
                {s.name || s.symbol}
              </button>
            ))}
            {t.count > t.stocks.length && <span className="px-1 py-0.5 text-[11px] text-muted">等{t.count}家</span>}
          </div>
        </div>
      ))}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 border-t border-border/40 pt-1.5 text-[11px] text-secondary">
        {first && <span>首板 <b className="font-mono text-foreground">{first.count}</b></span>}
        {promo?.today != null && <span>晋级率 <b className="font-mono text-foreground">{promo.today.toFixed(0)}%</b></span>}
        {perf && (
          <span>
            昨日涨停今日 <b className={cn('font-mono', priceColorClass(perf.avg_pct))}>{perf.avg_pct > 0 ? '+' : ''}{perf.avg_pct.toFixed(2)}%</b>
            {' '}· 红盘 <b className="font-mono text-foreground">{perf.up_ratio.toFixed(0)}%</b>
          </span>
        )}
      </div>
    </div>
  )
}

export function RecapSnapshotPanel({ snapshot, onOpenStock }: { snapshot: RecapSnapshot; onOpenStock?: (symbol: string) => void }) {
  const { phase } = snapshot
  return (
    <section aria-label="复盘数据" className="mb-4 space-y-3 border-b border-border/60 pb-4">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        {phase && (
          <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-accent">
            周期 · {phase.label} 第{phase.days}{phase.capped ? '+' : ''}天
            {phase.date !== snapshot.as_of && <span className="text-muted">(截至 {phase.date.slice(5)})</span>}
          </span>
        )}
        {phase?.state_label && <span className="rounded-full bg-elevated px-2 py-0.5 text-secondary">{phase.state_label}</span>}
        {snapshot.intraday && (
          <span className="rounded-full bg-warning/15 px-2 py-0.5 text-warning">盘中快照 {snapshot.generated_at.slice(11, 16)}</span>
        )}
        {snapshot.today_source === 'overview' && !snapshot.intraday && (
          <span className="text-muted" title="复盘日尚未写入环境时序, 今日列取实时总览, 与历史列口径略有差异">今日为总览口径</span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <MetricsTable metrics={snapshot.metrics} intraday={snapshot.intraday} />
        <TrendChart snapshot={snapshot} />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Mainlines snapshot={snapshot} onOpenStock={onOpenStock} />
        <Ladder snapshot={snapshot} onOpenStock={onOpenStock} />
      </div>
    </section>
  )
}
