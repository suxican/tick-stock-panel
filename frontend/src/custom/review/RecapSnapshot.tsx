/**
 * AI 复盘数据面板 —— 用后端快照(meta.snapshot / 归档 report.snapshot)画表格与图表,
 * AI 正文只写判断与计划, 数字统一在这里呈现, 避免模型转抄出错与重复。
 *
 * 区块: 周期状态条 / 核心指标对比表(含近 10 日迷你走势) / 成交额与涨停走势图 /
 *       涨停主线表 + 板块涨幅榜(标注是否属于主线) / 连板梯队。
 */
import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import { Link } from 'react-router-dom'
import type { RecapHoldings, RecapPlanCheck, RecapPlanStatus, RecapSnapshot, RecapSnapshotMetric } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { useChartTheme } from '@/lib/theme'
import { HistoryChart } from '@/custom/market-game/HistoryChart'

/**
 * 去掉正文末尾给系统核验用的 ```plan 代码块(含流式中尚未闭合的部分)。
 * 归档时后端已剥离; 这里覆盖生成中的流式正文与复制/下载。
 */
export function stripPlanBlock(content: string): string {
  // 流式中围栏可能只到了一半(如 "```pl"), 末尾的未完成围栏一并隐藏
  const i = content.search(/\n?```plan|\n```p?l?a?$/)
  return i < 0 ? content : content.slice(0, i).trimEnd() + '\n'
}

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
  const live = snapshot.mainline_source === 'live'
  return (
    <div className="min-w-0 space-y-2">
      {mainlines.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[11px] text-muted">
                <th className="py-1 pr-2 text-left font-normal" title={live ? '复盘日主线尚未盘后入库, 按实时涨停数据同公式计算' : undefined}>
                  涨停主线{live && <span className="ml-1 text-[10px]">· 盘中</span>}
                </th>
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
        <div className="text-[11px] text-muted">复盘日无达标涨停主线</div>
      )}
      <SectorChips label="板块领涨" items={sectors.leading} />
      <SectorChips label="板块领跌" items={sectors.lagging} />
    </div>
  )
}

function SectorChips({ label, items }: { label: string; items: RecapSnapshot['sectors']['leading'] }) {
  if (!items.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1 text-[11px]">
      <span className="mr-1 text-muted">{label}</span>
      {items.map(s => (
        <span
          key={s.name}
          className={cn('rounded px-1.5 py-0.5 text-secondary', s.mainline ? 'bg-accent/10 ring-1 ring-accent/30' : 'bg-elevated/60')}
          title={[s.mainline ? '属于涨停主线' : '涨幅异动, 未进入涨停主线', s.leader ? `领涨: ${s.leader}` : ''].filter(Boolean).join(' · ')}
        >
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

const STATUS_TEXT: Record<RecapPlanStatus, string> = { hit: '✅ 命中', miss: '❌ 落空', pending: '➖ 待定' }
const STATUS_CLS: Record<RecapPlanStatus, string> = { hit: 'text-bull', miss: 'text-bear', pending: 'text-muted' }

function PlanCheck({ check, stats }: { check: RecapPlanCheck | null | undefined; stats: RecapSnapshot['plan_stats'] }) {
  if (!check && !stats) return null
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px]">
        <span className="text-muted">
          昨日剧本核验(系统按实际数据判定{check ? ` · 来自 ${check.from_as_of.slice(5)}` : ''})
        </span>
        {stats && stats.primary_total > 0 && (
          <span className="text-secondary">主判断命中 <b className="font-mono text-foreground">{stats.primary_hits}/{stats.primary_total}</b></span>
        )}
        {stats && stats.covered_total > 0 && (
          <span className="text-secondary" title="至少一个情景全部条件成立的天数占比">
            剧本覆盖 <b className="font-mono text-foreground">{stats.covered}/{stats.covered_total}</b>
          </span>
        )}
      </div>
      {check ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[11px] text-muted">
                <th className="py-1 pr-2 text-left font-normal">情景</th>
                <th className="px-2 py-1 text-left font-normal">触发条件 → 实际</th>
                <th className="py-1 pl-2 text-right font-normal">结果</th>
              </tr>
            </thead>
            <tbody>
              {check.scenarios.map(sc => (
                <tr key={sc.name} className={cn('border-t border-border/40', sc.primary && 'bg-accent/5')}>
                  <td className="whitespace-nowrap py-1.5 pr-2 align-top text-foreground">
                    {sc.name}{sc.primary && <span className="ml-1 text-[10px] text-accent">主判断</span>}
                  </td>
                  <td className="px-2 py-1.5">
                    {sc.conditions.map((c, i) => (
                      <div key={i} className="flex flex-wrap gap-x-1.5">
                        {i > 0 && sc.match === 'any' && <span className="text-muted">或</span>}
                        <span className="text-secondary">{c.label}</span>
                        <span className={cn('font-mono', STATUS_CLS[c.status])}>→ {c.actual_text}</span>
                      </div>
                    ))}
                  </td>
                  <td className={cn('whitespace-nowrap py-1.5 pl-2 text-right align-top', STATUS_CLS[sc.status])}>
                    {STATUS_TEXT[sc.status]}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-[11px] text-muted">上一交易日没有可核验的结构化剧本</div>
      )}
    </div>
  )
}

const PNL_TEXT = { deep_loss: '深套', loss: '浮亏', flat: '持平', profit: '浮盈', unknown: '' } as const
const REL_TEXT = { stronger: '跑赢', weaker: '跑输', inline: '同步', unknown: '' } as const

function HoldingsLinkage({ holdings, onOpenStock }: { holdings: RecapHoldings; onOpenStock?: (symbol: string) => void }) {
  // 多只持仓共有的数据原因(如日线未定版)只在表下提示一次
  const notes = Array.from(new Set([...holdings.warnings, ...holdings.rows.flatMap(r => r.warnings)])).slice(0, 3)
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="flex flex-wrap items-baseline gap-x-3 text-[11px]">
        <span className="text-muted">持仓联动(批次提醒 · 条件由程序给出)</span>
        {holdings.market_only > 0 && (
          <span className="text-secondary">
            {holdings.market_only} 只缺建仓日或失效条件, 只看市场联动 ·{' '}
            <Link to="/lots" className="text-accent hover:underline">去补全</Link>
          </span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-[11px] text-muted">
              <th className="py-1 pr-2 text-left font-normal">持仓</th>
              <th className="px-2 py-1 text-right font-normal">今日</th>
              <th className="px-2 py-1 text-left font-normal">联动</th>
              <th className="px-2 py-1 text-right font-normal">距成本</th>
              <th className="py-1 pl-2 text-left font-normal">条件</th>
            </tr>
          </thead>
          <tbody>
            {holdings.rows.map(r => (
              <tr key={r.symbol} className="border-t border-border/40 align-top">
                <td className="whitespace-nowrap py-1.5 pr-2">
                  <button type="button" onClick={() => onOpenStock?.(r.symbol)} className="text-foreground hover:text-accent">{r.name}</button>
                  {r.mode === 'market' && (
                    <span className="ml-1 rounded bg-elevated px-1 text-[10px] text-muted" title={`缺${r.missing.join('、')}`}>只看联动</span>
                  )}
                </td>
                <td className={cn('px-2 py-1.5 text-right font-mono tabular-nums', priceColorClass(r.stock_pct))}>
                  {r.stock_pct == null ? '—' : `${r.stock_pct > 0 ? '+' : ''}${r.stock_pct.toFixed(2)}%`}
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex flex-wrap gap-1">
                    {r.mainline && (
                      <span className="rounded bg-bull/10 px-1.5 py-0.5 text-[11px] text-bull">
                        主线 {r.mainline.name} 第{r.mainline.rank} · {r.mainline.streak}日
                      </span>
                    )}
                    {r.hot.map(h => (
                      <span key={h.name} className="rounded bg-elevated/60 px-1.5 py-0.5 text-[11px] text-secondary">
                        {h.name} <span className={cn('font-mono', priceColorClass(h.pct))}>{h.pct > 0 ? '+' : ''}{h.pct.toFixed(2)}%</span>
                      </span>
                    ))}
                    {r.sector && (
                      <span className="px-0.5 py-0.5 text-[11px] text-muted">
                        {r.sector.name} {r.sector.pct > 0 ? '+' : ''}{r.sector.pct.toFixed(2)}%{REL_TEXT[r.relative] && ` · ${REL_TEXT[r.relative]}`}
                      </span>
                    )}
                    {!r.mainline && !r.hot.length && !r.sector && <span className="text-[11px] text-muted">未进入今日热点</span>}
                  </div>
                </td>
                <td className={cn('whitespace-nowrap px-2 py-1.5 text-right font-mono tabular-nums', priceColorClass(r.pnl_pct))}>
                  {r.pnl_pct == null ? '—' : `${r.pnl_pct > 0 ? '+' : ''}${r.pnl_pct.toFixed(1)}%`}
                  {PNL_TEXT[r.pnl_state] && <span className="ml-1 font-sans text-[10px] text-muted">{PNL_TEXT[r.pnl_state]}</span>}
                </td>
                <td className="py-1.5 pl-2 text-[11px]">
                  {r.conditions.length ? r.conditions.map((c, i) => (
                    <div key={i}><span className="font-mono text-foreground">{c.label}</span> <span className="text-secondary">→ {c.meaning}</span></div>
                  )) : <span className="text-muted">技术位不可用</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {notes.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-muted">
          {notes.map(n => <li key={n}>· {n}</li>)}
        </ul>
      )}
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
      <PlanCheck check={snapshot.plan_check} stats={snapshot.plan_stats} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <MetricsTable metrics={snapshot.metrics} intraday={snapshot.intraday} />
        <TrendChart snapshot={snapshot} />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Mainlines snapshot={snapshot} onOpenStock={onOpenStock} />
        <Ladder snapshot={snapshot} onOpenStock={onOpenStock} />
      </div>
      {snapshot.holdings && snapshot.holdings.rows.length > 0 && (
        <HoldingsLinkage holdings={snapshot.holdings} onOpenStock={onOpenStock} />
      )}
    </section>
  )
}
