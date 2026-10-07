import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { RefreshCw } from 'lucide-react'
import { api, type MarketGameCapitalEnvelope, type MarketGameCapitalMetric, type MarketGameCapitalObservation, type MarketGameCapitalRow, type MarketGameReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { useChartTheme } from '@/lib/theme'
import { OverseasPanel } from './OverseasPanel'

const button = 'inline-flex min-h-9 items-center justify-center gap-2 rounded-btn border border-border px-3 py-2 text-xs transition-colors hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
const panel = 'min-w-0 rounded-card border border-border bg-surface/80 p-4'
const up = 'text-red-700 dark:text-bull'
const down = 'text-emerald-700 dark:text-bear'
const warning = 'text-amber-700 dark:text-warning'
const statuses: Record<string, string> = { ready: '行为证据可用', limited: '行为证据有限', unavailable: '行为证据不足', stale: '行情已过时', historical: '历史研究观察' }
const linkLabels = { watch: '等待原条件确认', blocked: '额外否决', inactive: '原计划已停用', unverified: '条件待核验' }
const scenarioColors: Record<string, string> = { panic_absorption: up, panic_withdrawal: down, hot_distribution: warning, trend_feedback: up, divergence_repair: 'text-blue-700 dark:text-accent' }
const metrics = [
  { id: 'participation', label: '参与强度', color: '#3b82f6', description: '最近 30 分钟 / 此前 30 分钟成交量比' },
  { id: 'support', label: '承接强度', color: '#f04438', description: '收盘价在最近 30 分钟高低区间中的位置' },
  { id: 'distribution', label: '兑现压力', color: '#f79009', description: '当前价格距最近 30 分钟高点的回撤幅度' },
] as const
const score = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : value.toFixed(1)
const percent = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`
const signedClass = (value: number | null | undefined) => value == null || value === 0 ? 'text-secondary' : value > 0 ? up : down
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'
function time(value: string | null | undefined) {
  if (!value) return '未提供'
  if (/^\d{4}-\d{2}-\d{2}$/.test(value) || /^\d{2}:\d{2}$/.test(value)) return value
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}+08:00`
  const date = new Date(zoned)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
function money(value: number | null) {
  if (value == null) return '—'
  const absolute = Math.abs(value)
  return absolute >= 1e8 ? `${(value / 1e8).toFixed(2)} 亿` : absolute >= 1e4 ? `${(value / 1e4).toFixed(2)} 万` : `${value.toLocaleString('zh-CN')} 元`
}
function Lines({ items }: { items: string[] }) {
  return items.length ? <ul className="space-y-1 leading-relaxed">{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="text-secondary">未提供</p>
}
function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return <section aria-label={title} className={panel}><h2 className="text-sm font-semibold">{title}</h2>{hint && <p className="mt-1 max-w-prose text-xs leading-relaxed text-secondary">{hint}</p>}<div className="mt-3">{children}</div></section>
}
function Metric({ value, label }: { value: MarketGameCapitalMetric; label: string }) {
  return <span aria-label={`${label} ${value.score == null ? '无法评分' : `${score(value.score)} 分`}，历史样本 ${value.sample_size}`} className="whitespace-nowrap font-mono">{score(value.score)}<span className="ml-1 text-[10px] text-secondary">/{value.sample_size}日</span></span>
}

function StockEvidence({ row, observation }: { row: MarketGameCapitalRow; observation: MarketGameCapitalObservation }) {
  const linked = observation.plan_links.find(item => item.symbol === row.symbol)
  return <div className="mt-3 border-t border-border pt-3 text-xs">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">{row.name} · 证据拆解</h3><span className={cn('font-medium', scenarioColors[row.scenario.id] ?? 'text-secondary')}>{row.scenario.label}</span></div>
    <p className="mt-1 text-secondary">{row.reason} · 行情时点 {time(row.as_of)}</p>
    <dl className="mt-3 grid gap-x-6 gap-y-3 leading-relaxed md:grid-cols-2">
      <div><dt className="mb-1 font-medium">可观察事实</dt><dd className="text-secondary"><Lines items={row.scenario.evidence} /></dd></div>
      <div><dt className="mb-1 font-medium">另一种解释</dt><dd className="text-secondary">{row.scenario.alternative}</dd></div>
      <div><dt className="mb-1 font-medium">确认条件</dt><dd className="text-secondary">{row.scenario.confirmation}</dd></div>
      <div><dt className="mb-1 font-medium">推翻条件</dt><dd className="text-secondary">{row.scenario.invalidation}</dd></div>
    </dl>
    <details className="mt-2"><summary className="min-h-8 cursor-pointer py-2 font-medium">查看原始刻度与计算口径</summary><dl className="space-y-2 text-secondary">{metrics.map(metric => <div key={metric.id} className="flex flex-wrap justify-between gap-x-4 gap-y-1"><dt>{metric.description}</dt><dd className="font-mono">{metric.id === 'participation' ? `${score(row[metric.id].raw_value)} 倍` : percent(row[metric.id].raw_value)}</dd></div>)}<div className="flex flex-wrap justify-between gap-x-4 gap-y-1"><dt>分钟收盘价按量加权参考价（非成交 VWAP）</dt><dd className="font-mono">{row.reference_price?.toFixed(2) ?? '—'} · 偏离 {percent(row.price_bias)}</dd></div></dl></details>
    {linked && <div className="mt-3 border-t border-border pt-3"><h3 className={cn('font-semibold', linked.status === 'blocked' ? warning : 'text-foreground')}>记录时：{linkLabels[linked.status]}</h3><p className="mt-1 leading-relaxed text-secondary">{linked.reason}</p><p className="mt-2 font-mono">原计划单股上限 {percent(linked.original_max_position)} · 观察约束上限 {percent(linked.observation_cap)}</p>{linked.status === 'unverified' && linked.retained_cap != null && linked.retained_cap < linked.original_max_position && <p className="mt-2 leading-relaxed text-secondary">本日已保留收紧上限 {percent(linked.retained_cap)}，当前仍待核验。</p>}<p className="mt-2 leading-relaxed text-secondary">仅收紧原计划，不授权入场或增加仓位；此处为保存时的判断，不代表当前条件已满足。</p>{linked.unchecked_conditions.length > 0 && <details open className="mt-1"><summary className="min-h-8 cursor-pointer py-2 font-medium">仍待核验的原条件</summary><div className="text-secondary"><Lines items={linked.unchecked_conditions} /></div></details>}</div>}
    {!linked && <p className="mt-3 text-secondary">板块样本股，不属于原计划候选，不生成入场计划。</p>}
  </div>
}

function BehaviorChart({ report, observation }: { report: MarketGameReport; observation: MarketGameCapitalObservation }) {
  const theme = useChartTheme()
  const points = observation.behavior.series
  const option = useMemo<EChartsOption>(() => ({
    animation: false, color: metrics.map(metric => metric.color),
    tooltip: { trigger: 'axis', backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder, textStyle: { color: theme.tooltipText } },
    legend: { top: 5, textStyle: { color: theme.text, fontSize: 11 } },
    grid: { left: 35, right: 12, top: 42, bottom: 30 },
    xAxis: { type: 'category', boundaryGap: false, data: points.map(point => point.time), axisLabel: { color: theme.text, fontSize: 10 }, axisLine: { lineStyle: { color: theme.border } } },
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { lineStyle: { color: theme.grid } } },
    series: metrics.map(metric => ({ name: metric.label, type: 'line' as const, showSymbol: false, connectNulls: false, smooth: false, data: points.map(point => point[metric.id]) })),
  }), [points, theme])
  const hasPoints = points.some(point => metrics.some(metric => point[metric.id] != null))
  return <Section title="情绪背景与资金行为" hint="上方心理刻度冻结于报告日；下方为本次取得的分钟样本轨迹，不代表当时已经触发信号。">
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-border pb-3 text-xs"><span className="font-medium">情绪背景 {observation.background_date}</span>{report.psychology?.dimensions.map(dimension => <span key={dimension.id} className="text-secondary">{dimension.label} <span className="font-mono text-foreground">{score(dimension.score)}</span></span>)}</div>
    <p className={cn('mt-2 text-xs leading-relaxed', observation.background_usable ? 'text-secondary' : warning)}>{observation.background_reason}</p>
    {!report.psychology && <p className="mt-2 text-xs text-secondary">原报告没有心理评分，不补算历史情绪。</p>}
    {hasPoints ? <ReactECharts option={option} notMerge style={{ height: 240 }} opts={{ renderer: 'svg' }} /> : <p className="flex min-h-32 items-center justify-center text-xs text-secondary">历史同时间窗口样本不足，暂不绘制资金分位曲线。</p>}
    <p className="text-xs leading-relaxed text-secondary">0–100 为各维度历史分位，兑现压力高不代表机会大。分数来自量价代理，不识别机构或量化身份；同源量价信息不作为独立证据重复确认。</p>
    {points.length > 0 && <details className="mt-2 text-xs"><summary className="min-h-8 cursor-pointer py-2 text-secondary">查看轨迹数值与样本覆盖</summary><div className="max-h-48 overflow-auto"><table className="w-full min-w-[400px] text-left"><caption className="sr-only">本次取得的分钟分位轨迹</caption><thead><tr>{['时间', '有效样本股', ...metrics.map(metric => metric.label)].map(label => <th key={label} scope="col" className="py-2 font-medium">{label}</th>)}</tr></thead><tbody>{points.map(point => <tr key={point.time} className="border-t border-border"><td className="py-2 font-mono">{point.time}</td><td>{point.sample_size}</td>{metrics.map(metric => <td key={metric.id} className="font-mono">{score(point[metric.id])}</td>)}</tr>)}</tbody></table></div></details>}
  </Section>
}

function Disclosures({ data }: { data: MarketGameCapitalObservation['disclosures'] }) {
  return <Section title="已披露机构交易" hint="龙虎榜仅覆盖上榜交易，不代表机构全部持仓或全市场资金流；单日榜与三日榜分别展示，不重叠累加。">
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs"><span className={cn('font-medium', data.state === 'ok' ? 'text-foreground' : warning)}>{data.status}</span><span className="text-secondary">来源 {data.source_label}</span><span className="text-secondary">实际榜单 {data.trade_date ?? '未提供'} · 请求日期 {data.requested_date}</span></div>
    <p className="mt-2 text-xs leading-relaxed text-secondary">本次取得 {time(data.fetched_at)} · 历史可得时间 {data.available_at ? time(data.available_at) : '未知'}。取得时间不等于首次披露时间，不用于回填原计划。</p>
    {data.rows.length > 0 ? <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[660px] text-left text-xs"><caption className="sr-only">龙虎榜机构席位交易披露</caption><thead className="border-b border-border text-secondary"><tr>{['个股', '实际日期', '统计区间', '机构净额', '净额占比', '买入 / 卖出席位'].map(label => <th key={label} scope="col" className="py-2 pr-3 font-medium">{label}</th>)}</tr></thead><tbody>{data.rows.map(row => <tr key={`${row.symbol}-${row.trade_date}-${row.range_days}`} className="border-b border-border/60"><td className="py-3 pr-3">{row.name}<span className="ml-2 font-mono text-secondary">{row.symbol}</span></td><td className="pr-3 font-mono">{row.trade_date}</td><td className="pr-3">{row.range_days === 3 ? '三日榜' : '单日榜'}</td><td className={cn('pr-3 font-mono', signedClass(row.org_net_value))}>{money(row.org_net_value)}</td><td className={cn('pr-3 font-mono', signedClass(row.org_net_rate))}>{percent(row.org_net_rate)}</td><td className="font-mono">{row.org_buy_num ?? '—'} / {row.org_sell_num ?? '—'}</td></tr>)}</tbody></table></div> : <p className="py-4 text-sm text-secondary">暂无可核验的机构披露记录。</p>}
    {data.state === 'source_unavailable' && <a href="/settings?tab=data-sources" className="inline-flex min-h-9 items-center text-xs text-accent underline underline-offset-4">检查数据源配置</a>}
    {data.omitted_count > 0 && <p className="mt-2 text-xs text-secondary">另有 {data.omitted_count} 条记录未展示。</p>}
    {data.limitations.length > 0 && <details className="mt-2 text-xs"><summary className="min-h-8 cursor-pointer py-2 font-medium">披露范围与数据限制</summary><div className="text-secondary"><Lines items={data.limitations} /></div></details>}
  </Section>
}

export function CapitalPanel({ report }: { report: MarketGameReport }) {
  const client = useQueryClient()
  const [automatic, setAutomatic] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const query = useQuery({ queryKey: QK.marketGameCapital(report.id), queryFn: () => api.marketGameCapital(report.id), retry: false, staleTime: 60_000, refetchOnWindowFocus: false })
  const envelope = query.data?.report_id === report.id ? query.data : undefined
  const refresh = useMutation({
    mutationKey: QK.marketGameCapitalRefresh(report.id), mutationFn: (id: string) => api.marketGameRefreshCapital(id),
    onMutate: id => client.cancelQueries({ queryKey: QK.marketGameCapital(id), exact: true }),
    onSuccess: (result, id) => { if (result.report_id === id) client.setQueryData(QK.marketGameCapital(id), result); if (!result.auto_refresh_allowed) setAutomatic(false) },
    onError: () => setAutomatic(false),
  })
  useEffect(() => {
    if (!automatic || !envelope?.auto_refresh_allowed || !envelope.refresh_allowed || refresh.isPending) return
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') refresh.mutate(report.id) }, 60_000)
    return () => window.clearInterval(timer)
  }, [automatic, envelope?.auto_refresh_allowed, envelope?.refresh_allowed, refresh.isPending, refresh.mutate, report.id])
  const latest = envelope?.latest?.report_id === report.id ? envelope.latest : null
  const rows = latest?.behavior.rows ?? []
  const active = rows.find(row => row.symbol === selected) ?? rows[0]
  const canRefresh = !!envelope?.refresh_allowed && !refresh.isPending
  const canObserve = !!envelope?.refresh_allowed && envelope.auto_refresh_allowed
  return <>
    <section aria-label="资金观察操作" className={panel}>
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-sm font-semibold">资金行为与情绪博弈</h2><p className="mt-1 text-xs text-secondary">独立记录观察快照，保留原计划的证据与仓位。</p></div><div className="flex flex-wrap items-center gap-4"><label className="flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" aria-label="每 60 秒记录观察" checked={automatic && canObserve} disabled={!canObserve || refresh.isPending} onChange={event => setAutomatic(event.target.checked)} className="h-4 w-4 accent-accent disabled:opacity-50" />每 60 秒观察</label><button type="button" className={button} disabled={!canRefresh} onClick={() => refresh.mutate(report.id)}><RefreshCw className={cn('h-3.5 w-3.5', refresh.isPending && 'animate-spin motion-reduce:animate-none')} />{refresh.isPending ? '正在取得观察…' : '刷新并记录观察'}</button></div></div>
      {envelope && <p className="mt-2 text-xs leading-relaxed text-secondary">{envelope.refresh_reason}{automatic && envelope.auto_refresh_allowed ? ' 当前页每 60 秒记录一次；隐藏页面暂停，离开页签停止。' : ' 自动观察默认关闭。'}</p>}
      {query.isLoading && <div role="status" className="mt-3"><p className="text-xs text-secondary">正在读取已保存的资金观察…</p><div aria-hidden className="mt-2 h-12 animate-pulse rounded-btn bg-elevated motion-reduce:animate-none" /></div>}
      {query.isError && <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 text-xs"><span>观察读取失败：{errorText(query.error)}</span><button type="button" className={button} onClick={() => void query.refetch()}>重试读取观察</button></div>}
      {refresh.isError && <p role="alert" className="mt-3 text-xs leading-relaxed">观察刷新失败：{errorText(refresh.error)}。{latest ? '保留上一份有效观察。' : ''}自动观察已停止。</p>}
      {envelope && !latest && <p className="mt-4 text-sm leading-relaxed text-secondary">这份计划尚未记录资金观察。点击“刷新并记录观察”取得当前可用数据；不会补造过去的触发事件。</p>}
    </section>
    {latest && <>
      <OverseasPanel data={latest.overseas} mode="observation" />
      <section aria-label="资金观察结论" className={panel}><div className="flex flex-wrap justify-between gap-2"><h2 className="text-sm font-semibold leading-relaxed">{latest.summary}</h2><span className="text-xs text-secondary">记录于 {time(latest.created_at)}</span></div><dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs"><div><dt className="inline text-secondary">记录时行为证据 </dt><dd className={cn('inline font-medium', latest.behavior.status === 'ready' ? 'text-foreground' : warning)}>{statuses[latest.behavior.status]}</dd></div><div><dt className="inline text-secondary">行情日期 </dt><dd className="inline font-mono">{latest.behavior.data_date ?? '未提供'}</dd></div><div><dt className="inline text-secondary">最晚分钟时点 </dt><dd className="inline font-mono">{time(latest.behavior.observed_at)}</dd></div><div><dt className="inline text-secondary">观察范围 </dt><dd className="inline">{rows.length} 只候选与样本股</dd></div><div><dt className="inline text-secondary">量化身份 </dt><dd className="inline text-secondary">未验证</dd></div></dl><p className="mt-2 text-xs leading-relaxed text-secondary">{latest.behavior.reason}</p><p className="mt-1 text-xs text-secondary">已保存观察，不代表当前状态；各股最后一根分钟线的时点见证据详情。</p></section>
      <Section title="个股资金行为" hint="5 / 15 / 30 分钟为价格变化；三项刻度为历史同时间窗口分位 / 有效样本日数。点击个股查看证据和原计划约束。">
        {rows.length > 0 ? <><div className="overflow-x-auto"><table className="w-full min-w-[960px] text-left text-xs"><caption className="sr-only">候选与样本股资金行为及短时价格变化</caption><thead className="border-b border-border text-secondary"><tr>{['个股 / 板块', '近 5 分钟', '近 15 分钟', '近 30 分钟', ...metrics.map(metric => metric.label), '情绪关系'].map(label => <th key={label} scope="col" className="py-2 pr-3 font-medium">{label}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.symbol} className={cn('border-b border-border/60', active?.symbol === row.symbol && 'bg-accent/5')}><td className="py-2 pr-3"><button type="button" aria-pressed={active?.symbol === row.symbol} onClick={() => setSelected(row.symbol)} className="min-h-9 text-left font-medium underline decoration-border underline-offset-4 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">{row.name}<span className="ml-2 font-mono text-secondary">{row.symbol}</span></button><span className="block text-secondary">{row.sector ?? '未归属板块'} · {row.is_candidate ? '原计划候选' : '板块样本'}</span></td>{([5, 15, 30] as const).map(minutes => { const value = row.windows.find(window => window.minutes === minutes)?.return; return <td key={minutes} className={cn('pr-3 font-mono', signedClass(value))}>{percent(value)}</td> })}{metrics.map(metric => <td key={metric.id} className="pr-3"><Metric value={row[metric.id]} label={metric.label} /></td>)}<td className={cn('py-2', scenarioColors[row.scenario.id] ?? 'text-secondary')}>{row.scenario.label}</td></tr>)}</tbody></table></div>{active && <StockEvidence row={active} observation={latest} />}</> : <p className="py-3 text-sm text-secondary">当前没有可核验的分钟样本，暂不推断资金行为。</p>}
      </Section>
      <div className="grid min-w-0 items-start gap-3 xl:grid-cols-[1.2fr_1fr]">
        <BehaviorChart report={report} observation={latest} />
        <Section title="板块样本汇总" hint="仅汇总本次观察样本；站上参考价占比不是全板块上涨比例。不能推断板块整体资金净流入。">
          {latest.behavior.sectors.length > 0 ? <div className="divide-y divide-border">{latest.behavior.sectors.map(sector => <article key={sector.name} className="py-3 first:pt-0"><div className="flex flex-wrap justify-between gap-2 text-xs"><h3 className="font-semibold">{sector.name}</h3><span className="text-secondary">有效 {sector.valid_count}/{sector.sample_size} · 覆盖 {percent(sector.coverage)}</span></div><dl className="mt-2 flex flex-wrap gap-x-5 gap-y-2 text-xs">{metrics.map(metric => <div key={metric.id}><dt className="text-secondary">{metric.label}</dt><dd className="mt-1 font-mono">{score(sector[metric.id])}</dd></div>)}<div><dt className="text-secondary">站上参考价</dt><dd className="mt-1 font-mono">{percent(sector.breadth)}</dd></div></dl><p className="mt-2 text-xs leading-relaxed text-secondary">{sector.scenario}</p></article>)}</div> : <p className="py-4 text-xs text-secondary">暂无可核验的板块样本。</p>}
        </Section>
      </div>
      <Disclosures data={latest.disclosures} />
      {(latest.limitations.length > 0 || latest.behavior.limitations.length > 0) && <details className="rounded-card border border-border bg-surface/50 px-4 py-1 text-xs"><summary className="min-h-8 cursor-pointer py-2 font-medium">观察边界与数据限制</summary><div className="pb-3 text-secondary"><Lines items={[...new Set([...latest.limitations, ...latest.behavior.limitations])]} /></div></details>}
    </>}
    {envelope && <Timeline envelope={envelope} />}
  </>
}

function Timeline({ envelope }: { envelope: MarketGameCapitalEnvelope }) {
  return <Section title="实际观察时间线" hint={`保留最近 ${envelope.history_limit} 次实际记录；仅描述相邻快照的变化，不重建未观察时段的事件。`}>
    {envelope.observations.length > 0 ? <ol className="divide-y divide-border">{envelope.observations.map(event => <li key={event.id} className="grid gap-2 py-3 first:pt-0 sm:grid-cols-[140px_1fr]"><div className="font-mono text-xs text-secondary"><time dateTime={event.created_at}>{time(event.created_at)}</time><span className="mt-1 block">行情 {event.data_date ?? '未知'}</span></div><div className="min-w-0 text-xs"><p className="font-medium leading-relaxed">{event.summary}</p><div className="mt-1 text-secondary">{event.changes.length > 0 ? <Lines items={event.changes} /> : <p>本次未记录状态变化。</p>}</div></div></li>)}</ol> : <p className="text-xs text-secondary">尚无实际观察记录。</p>}
  </Section>
}
