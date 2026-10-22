import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Scale } from 'lucide-react'
import { api, type MarketGameEcology, type MarketGameFeedbackHypothesis, type MarketGameFeedbackObservation, type MarketGameReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { ExecutionPanel } from './ExecutionPanel'

const panel = 'min-w-0 rounded-card border border-border bg-surface/80 p-4'
const button = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-btn border border-border px-3 py-2 text-xs font-medium transition-colors hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
const up = 'text-red-700 dark:text-bull'
const down = 'text-emerald-700 dark:text-bear'
const warning = 'text-amber-700 dark:text-warning'
const ecologyColor = { expanding: up, rotation: 'text-blue-700 dark:text-accent', concentrated: warning, contracting: down, unconfirmed: 'text-secondary' }
const directionLabels = { up: '上行', down: '下行', flat: '震荡', unknown: '方向待确认' }
const feedbackLabels = { reinforcing: '自我加强', corrective: '自我矫正', unclear: '机制待确认' }
const statusLabels = { supported: '获得支持', contradicted: '未获支持', pending: '待确认', unavailable: '无法核验', expired: '观察已到期' }
const percent = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`
const number = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const signed = (value: number | null) => value == null || value === 0 ? 'text-secondary' : value > 0 ? up : down
const errorText = (error: unknown) => error instanceof Error ? error.message : '请稍后重试。'

function time(value: string | null) {
  if (!value) return '未提供'
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}+08:00`)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

function Lines({ items }: { items: string[] }) {
  return <ul className="space-y-1 leading-relaxed">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return <section aria-label={title} className={panel}><h2 className="text-sm font-semibold">{title}</h2>{hint && <p className="mt-1 max-w-prose text-xs leading-relaxed text-secondary">{hint}</p>}<div className="mt-4">{children}</div></section>
}

function Limits({ items, label = '数据与判断边界' }: { items: string[]; label?: string }) {
  return items.length > 0 ? <details className="mt-3 border-t border-border pt-1 text-xs"><summary className="min-h-9 cursor-pointer py-2 font-medium">{label} · {items.length} 项</summary><div className="max-w-prose pb-2 text-secondary"><Lines items={items} /></div></details> : null
}

function metricValue(metric: MarketGameEcology['metrics'][number]) {
  if (metric.value == null || !Number.isFinite(metric.value)) return '—'
  if (metric.unit === 'ratio') return percent(metric.value)
  if (metric.unit === 'multiple') return `${number(metric.value)} 倍`
  if (metric.unit === 'yuan') return `${number(metric.value / 1e8)} 亿元`
  if (metric.unit === 'count') return number(metric.value, 0)
  return number(metric.value, 4)
}

function Ecology({ data }: { data?: MarketGameEcology | null }) {
  const [visibleSectors, setVisibleSectors] = useState(20)
  return <>
    <Section title="市场竞争环境" hint="竞争状态描述成交活跃、集中与扩散特征，成交额不等于新增资金。">
      {!data ? <p className="text-sm leading-relaxed text-secondary">此报告未保存竞争环境。新生成的计划会冻结当时可核验的指标；旧报告不补写历史结论。</p> : <>
        <div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className={cn('text-lg font-semibold', ecologyColor[data.state])}>{data.label}</h3><span className="text-xs text-secondary">日线样本覆盖 {percent(data.coverage)} · 行情 {data.as_of}</span></div>
        <p className="mt-2 max-w-prose text-sm leading-relaxed">{data.interpretation}</p>
        <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4 border-y border-border py-4 lg:grid-cols-3">{data.metrics.map(metric => <div key={metric.id} className="min-w-0"><dt className="text-xs text-secondary">{metric.label}</dt><dd className="mt-1 font-mono text-[16px] leading-6">{metricValue(metric)}</dd><dd className="mt-1 text-xs leading-relaxed text-secondary">{metric.reason}<span className="block">有效样本 {metric.sample_size}</span></dd></div>)}</dl>
        <div className="mt-3 text-xs text-secondary"><Lines items={data.evidence} /></div>
        <p className="mt-2 text-xs text-secondary">冻结于 {time(data.cutoff)}（北京时间）</p>
        <Limits items={data.limitations} />
      </>}
    </Section>
    {data && <Section title="板块竞争" hint="重叠板块成交额分摊，上涨覆盖和平均涨跌按有效成分股等权；变化需要可比的历史成员和行情。">
      {data.sectors.length ? <div className="overflow-x-auto"><table className="w-full min-w-[800px] text-left text-xs"><caption className="sr-only">冻结的板块成交份额、参与广度与相对表现</caption><thead className="border-b border-border text-secondary"><tr>{['板块 / 有效成员', '成交份额', '份额变化', '上涨覆盖', '平均涨跌', '相对表现', '结构判断'].map(label => <th scope="col" key={label} className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead><tbody>{data.sectors.slice(0, visibleSectors).map(sector => <tr key={sector.name} className="border-b border-border/60"><td className="py-3 pr-4 font-medium">{sector.name}<span className="mt-1 block font-normal text-secondary">{sector.member_count} 只</span></td><td className="pr-4 font-mono">{percent(sector.amount_share)}</td><td className={cn('pr-4 font-mono', signed(sector.share_change))}>{sector.share_change == null ? '—' : `${number(sector.share_change * 100)} 个百分点`}</td><td className="pr-4 font-mono">{percent(sector.breadth)}</td><td className={cn('pr-4 font-mono', signed(sector.avg_return))}>{percent(sector.avg_return)}</td><td className={cn('pr-4 font-mono', signed(sector.relative_return))}>{percent(sector.relative_return)}</td><td className="max-w-xs py-3"><span className="font-medium">{sector.status}</span><div className="mt-1 text-secondary"><Lines items={sector.evidence} /></div></td></tr>)}</tbody></table></div> : <p className="text-sm text-secondary">没有可核验的板块成员，暂不推断板块竞争。</p>}
      {data.sectors.length > 20 && <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs"><span className="text-secondary">已显示 {Math.min(visibleSectors, data.sectors.length)} / {data.sectors.length} 个板块 · 按成交份额排序</span>{visibleSectors < data.sectors.length && <button type="button" className={button} onClick={() => setVisibleSectors(count => count + 20)}>再显示 20 个板块</button>}</div>}
      <p className="mt-3 text-xs leading-relaxed text-secondary">{data.comparison_date ? `份额对比日 ${data.comparison_date}` : '份额变化尚未核验，缺失数据以 — 显示。'} 板块成交份额不表示资金净流入。</p>
    </Section>}
  </>
}

function FrozenHypothesis({ data }: { data?: MarketGameFeedbackHypothesis | null }) {
  return <Section title="冻结的反馈假设" hint="先记录预期与否定条件，再观察后续响应。这里检验本模型的预期，不代表全市场共识。">
    {!data ? <p className="text-sm leading-relaxed text-secondary">此报告未冻结反馈假设，后续观察不会反向补造当时的预期。</p> : <>
      <div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className={cn('text-sm font-semibold', data.kind === 'unconfirmed' ? 'text-secondary' : warning)}>{data.label}</h3><span className="text-xs text-secondary">冻结于 {time(data.cutoff)}</span></div>
      <p className="mt-2 max-w-prose text-sm leading-relaxed">{data.interpretation}</p>
      <p className="mt-2 text-xs text-secondary">风险偏好 {number(data.risk_appetite, 1)} · 恐慌压力 {number(data.panic_pressure, 1)}（历史分位，不是投资者人数占比）</p>
      <div className="mt-4 grid gap-4 border-t border-border pt-4 md:grid-cols-2"><div><h4 className="mb-2 text-xs font-semibold">确认条件</h4><div className="text-xs text-secondary">{data.confirmation.length ? <Lines items={data.confirmation.map(item => item.description)} /> : <p>尚未形成可检验的确认条件。</p>}</div></div><div><h4 className="mb-2 text-xs font-semibold">否定条件</h4><div className="text-xs text-secondary">{data.invalidation.length ? <Lines items={data.invalidation.map(item => item.description)} /> : <p>尚未形成可检验的否定条件。</p>}</div></div></div>
      <p className="mt-3 max-w-prose text-xs leading-relaxed text-secondary">另一种解释：{data.alternative}</p>
      <p className="mt-2 text-xs leading-relaxed text-secondary">观察交易日：{data.observation_sessions.length ? data.observation_sessions.join(' / ') : '待核验'}{data.expires_at && ` · 到期 ${time(data.expires_at)}`}</p>
      <Limits items={data.limitations} />
    </>}
  </Section>
}

function FeedbackDetails({ observation }: { observation: MarketGameFeedbackObservation }) {
  return <>
      {observation.rows.length ? <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[700px] text-left text-xs"><caption className="sr-only">原假设与实际观察响应</caption><thead className="border-b border-border text-secondary"><tr>{['个股', '价格方向', '反馈机制', '假设检验', '响应与依据'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead><tbody>{observation.rows.map(row => <tr key={row.symbol} className="border-b border-border/60"><td className="py-3 pr-4">{row.name}<span className="block font-mono text-secondary">{row.symbol}</span></td><td className={cn('pr-4 font-medium', row.price_direction === 'up' ? up : row.price_direction === 'down' ? down : 'text-secondary')}>{directionLabels[row.price_direction]}</td><td className="pr-4">{feedbackLabels[row.feedback_type]}</td><td className={cn('pr-4', row.status === 'contradicted' || row.status === 'pending' ? warning : 'text-secondary')}>{statusLabels[row.status]}</td><td className="max-w-md py-3"><p className={cn('font-medium', row.stage === 'fragile' ? warning : 'text-foreground')}>{row.label}</p><p className="mt-1 text-secondary">{row.reason}</p><details className="mt-1"><summary className="min-h-8 cursor-pointer py-1.5 text-secondary">查看证据与时点</summary><div className="text-secondary"><Lines items={row.evidence} /><p className="mt-1">{time(row.observed_at)}</p></div></details></td></tr>)}</tbody></table></div> : <p className="mt-3 text-xs text-secondary">本次没有足够的有效分钟样本。</p>}
      <Limits items={observation.limitations} />
  </>
}

function FeedbackRecord({ observation, latest }: { observation: MarketGameFeedbackObservation; latest: boolean }) {
  const [expanded, setExpanded] = useState(false)
  if (latest) return <article className="pb-4"><div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className="text-sm font-medium">{observation.summary}</h3><span className="text-xs text-secondary">最近观察 {time(observation.observed_at)}</span></div><FeedbackDetails observation={observation} /></article>
  return <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)} className="border-t border-border py-2"><summary className="min-h-9 cursor-pointer py-2 text-xs font-medium"><span className="mr-3 font-normal text-secondary">{time(observation.observed_at)}</span>{observation.summary}</summary>{expanded && <div className="pb-2"><FeedbackDetails observation={observation} /></div>}</details>
}

function Feedback({ observations }: { observations: MarketGameFeedbackObservation[] }) {
  return <Section title="反馈兑现记录" hint="仅检查原计划固定样本。价格方向与反馈机制分别判断；下跌和恐慌卖出也可能形成自我加强。">
    {observations.length ? <div>{observations.slice(0, 60).map((observation, index) => <FeedbackRecord key={`${observation.hypothesis_id}-${observation.observed_at}-${index}`} observation={observation} latest={index === 0} />)}</div> : <p className="text-sm leading-relaxed text-secondary">尚无反馈兑现记录。先在“资金行为与情绪博弈”记录可用分钟观察，再更新模型验证。</p>}
  </Section>
}
export function ModelPanel({ report }: { report: MarketGameReport }) {
  const client = useQueryClient()
  const query = useQuery({ queryKey: QK.marketGameModel(report.id), queryFn: () => api.marketGameModel(report.id), retry: false, staleTime: 60_000, refetchOnWindowFocus: false })
  const envelope = query.data?.report_id === report.id ? query.data : undefined
  const refresh = useMutation({
    mutationKey: QK.marketGameEvaluateModel(report.id), mutationFn: (id: string) => api.marketGameEvaluateModel(id),
    onMutate: id => client.cancelQueries({ queryKey: QK.marketGameModel(id), exact: true }),
    onSuccess: (data, id) => { if (data.report_id === id) client.setQueryData(QK.marketGameModel(id), data) },
  })
  const refreshing = refresh.isPending && refresh.variables === report.id
  const refreshError = refresh.isError && refresh.variables === report.id
  const mismatched = !!query.data && query.data.report_id !== report.id
  const invalidRefresh = refresh.isSuccess && refresh.variables === report.id && refresh.data.report_id !== report.id
  return <>
    <section aria-label="模型验证操作" className={panel}>
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="min-w-0"><h2 className="flex items-center gap-2 text-sm font-semibold"><Scale className="h-4 w-4 text-accent" />存量博弈模型</h2><p className="mt-1 max-w-prose text-xs leading-relaxed text-secondary">竞争环境 → 反馈兑现 → 执行评估 → 策略适应性。当前并行观察，不授权交易，不自动调整原计划仓位。</p></div><button type="button" className={button} disabled={!envelope || refreshing} onClick={() => refresh.mutate(report.id)}><RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin motion-reduce:animate-none')} />{refreshing ? '正在验证…' : '更新模型验证'}</button></div>
      {query.isLoading && <div role="status" className="mt-3"><p className="text-xs text-secondary">正在读取模型验证…</p><div aria-hidden className="mt-2 h-12 animate-pulse rounded-btn bg-elevated motion-reduce:animate-none" /></div>}
      {(query.isError || mismatched) && <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 text-xs"><span>模型验证读取失败：{mismatched ? '返回的计划不匹配。' : errorText(query.error)}</span><button type="button" className={button} onClick={() => void query.refetch()}>重试读取模型验证</button></div>}
      {(refreshError || invalidRefresh) && <p role="alert" className="mt-3 text-xs leading-relaxed">模型验证更新失败：{invalidRefresh ? '返回的计划不匹配。' : errorText(refresh.error)} 保留上一份已保存结果。</p>}
      {envelope && <p className="mt-3 text-xs text-secondary">{envelope.evaluated_at ? `验证保存于 ${time(envelope.evaluated_at)}（北京时间）。` : '尚未保存模型验证。'} 更新将检查当前可用数据；冻结的环境与假设保持原样。</p>}
      {envelope && <Limits items={envelope.limitations} label="验证范围" />}
    </section>
    <Ecology data={report.ecology} />
    <FrozenHypothesis data={report.feedback_hypothesis} />
    {envelope && <>
      <Feedback observations={envelope.feedback} />
      <ExecutionPanel execution={envelope.execution?.report_id === report.id ? envelope.execution : null} adaptation={envelope.adaptation} />
    </>}
  </>
}
