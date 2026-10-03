import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Scale, Sparkles } from 'lucide-react'
import { api, type MarketGameCandidate, type MarketGameEvaluation, type MarketGameReport, type MarketGameRisk } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import type { NavItem } from '@/lib/listNav'

const button = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-btn border border-border px-3 py-2 text-sm text-foreground transition-colors hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
const inputClass = 'min-h-10 rounded-btn border border-border bg-surface px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-50'
const qualityLabels = { ready: '输入可用', limited: '数据有限', unavailable: '暂不可分析' }
const hypothesisLabels = { matched: '符合当前证据', watch: '等待确认', inactive: '暂不成立' }
const modeLabels: Record<string, string> = { panic_repair: '恐慌修复', trend_pullback: '趋势回撤', crowded_exit: '拥挤退潮' }
const percentFormatter = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 2 })
const pct = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : percentFormatter.format(value)
const price = (value: number | null) => value == null ? '—' : value.toFixed(2)
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。'

function evidenceValue(value: number | null, unit: string) {
  if (value == null || !Number.isFinite(value)) return '—'
  if (unit === 'ratio') return pct(value)
  const number = value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
  return unit === 'yuan' ? `${number} 元` : number
}

function beijingToday() {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
}

function time(value: string | null) {
  if (!value) return '未提供'
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}+08:00`
  const date = new Date(zoned)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function Lines({ items }: { items: string[] }) {
  return items.length ? <ul className="space-y-1 leading-relaxed">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <p>未提供</p>
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return <section aria-label={title} className="min-w-0 border-t border-border py-6">
    <h2 className="text-[16px] leading-6 font-semibold text-foreground">{title}</h2>
    {hint && <p className="mt-1 max-w-prose text-sm leading-relaxed text-secondary">{hint}</p>}
    <div className="mt-4">{children}</div>
  </section>
}

const riskFields = [
  { key: 'total_cap', label: '总仓位上限（%）', initial: '50', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'single_cap', label: '单股上限（%）', initial: '15', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'sector_cap', label: '板块上限（%）', initial: '25', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'risk_per_trade', label: '单笔风险预算（%）', initial: '0.5', min: .01, max: 2, step: .01, divisor: 100 },
  { key: 'min_amount', label: '最低日成交额（亿元）', initial: '1', min: .1, max: 1000, step: .1, divisor: 1e-8 },
  { key: 'max_candidates', label: '候选数量上限', initial: '5', min: 1, max: 5, step: 1, divisor: 1 },
] as const
type RiskDraft = Record<keyof MarketGameRisk, string>

function Candidate({ candidate, onOpen }: { candidate: MarketGameCandidate; onOpen: () => void }) {
  return <article className="min-w-0 py-5 first:pt-0">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button type="button" onClick={onOpen} className="min-h-10 rounded-sm text-[16px] leading-6 font-semibold text-foreground underline decoration-border underline-offset-4 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">{candidate.name}</button>
        <span className="font-mono text-xs text-secondary">{candidate.symbol}</span>
        <span className="text-sm text-secondary">{candidate.sector} · {candidate.role}</span>
      </div>
      <span className="text-sm text-secondary">{modeLabels[candidate.mode] ?? candidate.mode}</span>
    </div>
    <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-sm sm:grid-cols-4">
      <div><dt className="text-secondary">参考收盘价</dt><dd className="mt-1 font-mono">{price(candidate.reference_price)}</dd></div>
      <div><dt className="text-secondary">确认参考区间</dt><dd className="mt-1 font-mono">{price(candidate.trigger_low)} – {price(candidate.trigger_high)}</dd></div>
      <div><dt className="text-secondary">失效参考价</dt><dd className="mt-1 font-mono">{price(candidate.invalidation_price)}</dd></div>
      <div><dt className="text-secondary">单股仓位上限</dt><dd className="mt-1 font-mono">{pct(candidate.max_position)}</dd></div>
    </dl>
    <p className="mt-3 text-sm text-secondary">计划持有不超过 {candidate.holding_days} 个交易日 · 压力情景跌幅 {pct(candidate.stress_loss_pct)}</p>
    <details className="mt-3 text-sm">
      <summary className="min-h-10 cursor-pointer py-2 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">查看依据、触发与退出</summary>
      <div className="mt-2 grid gap-4 text-secondary md:grid-cols-2">
        {([['入选依据', candidate.evidence], ['触发条件', candidate.trigger], ['失效条件', candidate.invalidation], ['退出规则', candidate.exit_rules]] as const).map(([label, items]) => <div key={label}><h3 className="mb-1 font-medium text-foreground">{label}</h3><Lines items={items} /></div>)}
      </div>
    </details>
  </article>
}

function Observation({ data }: { data: MarketGameEvaluation }) {
  const stateLabels = { pending: '待观察', available: '已观察', missing: '行情缺失', unavailable: '无法核验' }
  return <>
    <p className="mb-3 text-sm text-secondary">更新于 {time(data.evaluated_at)}（北京时间）</p>
    {data.rows.length > 0 ? <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-left text-sm">
        <caption className="sr-only">报告候选后续收盘观察表现</caption>
        <thead className="border-b border-border text-secondary"><tr>{['个股', '观察序列', '实际行情日', '收盘观察收益', '状态'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{data.rows.map(row => <tr key={`${row.symbol}-${row.horizon}`} className="border-b border-border/60"><td className="py-3 pr-4">{row.name}<span className="block font-mono text-xs text-secondary">{row.symbol}</span></td><td className="pr-4">{row.label}</td><td className="pr-4 font-mono">{row.trade_date ?? '—'}</td><td className="pr-4 font-mono">{row.state === 'available' ? pct(row.close_return) : '—'}</td><td>{stateLabels[row.state]}</td></tr>)}</tbody>
      </table>
    </div> : <p className="text-sm text-secondary">暂没有可核验的候选后续行情。</p>}
    <div className="mt-3 text-sm text-secondary"><Lines items={data.limitations} /></div>
  </>
}

function ReportBody({ report, onOpen }: { report: MarketGameReport; onOpen: (candidate: MarketGameCandidate) => void }) {
  const { allocation, market_state: market } = report
  return <>
    <div className="rounded-card border border-border bg-surface p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="rounded bg-elevated px-2 py-1 font-medium">{qualityLabels[report.quality]}</span>
        {report.research_only && <span className="rounded border border-border px-2 py-1 font-medium">研究阶段计划</span>}
        <span className="text-secondary">行情归属 {report.as_of}</span>
      </div>
      <h2 className="mt-3 max-w-prose text-lg font-semibold leading-relaxed">{report.summary}</h2>
      <p className="mt-2 text-xs leading-relaxed text-secondary">数据截止 {time(report.cutoff)} · 生成 {time(report.created_at)}（北京时间）</p>
      <dl className="mt-5 flex flex-wrap gap-x-8 gap-y-3 border-t border-border pt-4 text-sm">
        {([['趋势', market.trend], ['周期', market.phase], ['情绪', market.emotion], ['拥挤程度', market.crowding]] as const).map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 font-medium">{value}</dd></div>)}
      </dl>
      <p className="mt-3 max-w-prose text-sm leading-relaxed text-secondary">{market.change}</p>
      <div className="mt-5 border-t border-border pt-4">
        <p className="text-[16px] leading-6 font-semibold">条件仓位 <span className="ml-2 font-mono">{pct(allocation.min)} – {pct(allocation.max)}</span></p>
        <p className="mt-2 max-w-prose text-sm leading-relaxed">{allocation.reason}</p>
        <p className="mt-2 text-xs leading-relaxed text-secondary">总仓上限 {pct(allocation.total_cap)} · 单股 {pct(allocation.single_cap)} · 板块 {pct(allocation.sector_cap)} · 单笔风险 {pct(allocation.risk_per_trade)}</p>
      </div>
      <p className="mt-4 text-xs leading-relaxed text-secondary">胜率与情景概率尚未校准；行为解释是可证伪的假设。仓位为条件上限，须结合已有持仓；此处不推断账户余额或执行交易。</p>
    </div>

    {report.limitations.length > 0 && <section aria-label="数据限制" className="py-5 text-sm">
      <h2 className="mb-2 font-semibold">本次分析的边界</h2>
      <div className="max-w-prose text-secondary"><Lines items={report.limitations} /></div>
      {report.quality === 'unavailable' && <a className="mt-2 inline-flex min-h-10 items-center underline underline-offset-4" href="/settings?tab=data-sources">检查数据源配置</a>}
    </section>}

    <Section title="未来 1–3 个交易日" hint="按条件逐日复核。先满足触发条件，再考虑对应仓位；情景未确认时不执行。">
      {report.scenarios.length ? <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left text-sm">
        <caption className="sr-only">未来交易日情景与仓位条件</caption>
        <thead className="border-b border-border text-secondary"><tr>{['窗口', '情景', '确认条件', '应对', '仓位上限'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{report.scenarios.map((scenario, index) => <tr key={`${scenario.horizon}-${index}`} className="border-b border-border/60 align-top"><td className="whitespace-nowrap py-3 pr-4">{scenario.label}</td><td className="py-3 pr-4">{scenario.scenario}</td><td className="max-w-xs py-3 pr-4 leading-relaxed">{scenario.condition}</td><td className="max-w-xs py-3 pr-4 leading-relaxed">{scenario.action}</td><td className="py-3 font-mono">{pct(scenario.max_position)}</td></tr>)}</tbody>
      </table></div> : <p className="text-sm text-secondary">数据不足，暂不形成情景计划。</p>}
    </Section>

    <Section title="行为与博弈假设" hint="把可观察事实、心理解释和另一种可能放在一起，避免把上涨或下跌自动解释为操纵。">
      <div className="divide-y divide-border">{report.hypotheses.map(hypothesis => <details key={hypothesis.id} open={hypothesis.status === 'matched'} className="py-3 first:pt-0">
        <summary className="min-h-10 cursor-pointer py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">{hypothesis.title}<span className="ml-3 font-normal text-secondary">{hypothesisLabels[hypothesis.status]}</span></summary>
        <div className="mt-2 grid gap-4 text-sm text-secondary md:grid-cols-2">
          <div><h3 className="mb-1 font-medium text-foreground">观察事实</h3><Lines items={hypothesis.facts} /></div>
          <div><h3 className="mb-1 font-medium text-foreground">行为解释</h3><p className="leading-relaxed">{hypothesis.interpretation}</p><h3 className="mb-1 mt-3 font-medium text-foreground">另一种解释</h3><p className="leading-relaxed">{hypothesis.alternative}</p></div>
          <div><h3 className="mb-1 font-medium text-foreground">确认条件</h3><Lines items={hypothesis.confirm} /></div>
          <div><h3 className="mb-1 font-medium text-foreground">推翻条件</h3><Lines items={hypothesis.invalidation} /></div>
        </div>
      </details>)}</div>
      {!report.hypotheses.length && <p className="text-sm text-secondary">暂无证据充分的行为假设。</p>}
    </Section>

    <Section title="板块观察">
      {report.sectors.length ? <div className="overflow-x-auto"><table className="w-full min-w-[560px] text-left text-sm">
        <caption className="sr-only">候选板块及其观察依据</caption>
        <thead className="border-b border-border text-secondary"><tr>{['板块', '平均涨跌幅', '上涨占比', '状态与依据'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{report.sectors.map(sector => <tr key={sector.name} className="border-b border-border/60 align-top"><td className="py-3 pr-4 font-medium">{sector.name}</td><td className="py-3 pr-4 font-mono">{pct(sector.avg_return)}</td><td className="py-3 pr-4 font-mono">{pct(sector.breadth)}</td><td className="max-w-md py-3 leading-relaxed">{sector.status} · {sector.reason}</td></tr>)}</tbody>
      </table></div> : <p className="text-sm text-secondary">当前数据未形成可关注的板块。</p>}
    </Section>

    <Section title="个股条件清单" hint="价格为报告生成时的参考值，须复核可交易性。个股详情展示当前数据，不是本报告时点的历史回放。">
      {report.candidates.length ? <div className="divide-y divide-border">{report.candidates.map(candidate => <Candidate key={candidate.symbol} candidate={candidate} onOpen={() => onOpen(candidate)} />)}</div> : <p className="text-sm text-secondary">本计划没有可列出的候选。可先观察市场条件，数据不足时不补猜股票。</p>}
    </Section>

    <details className="border-t border-border py-5">
      <summary className="min-h-10 cursor-pointer py-2 text-sm font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">原始证据与版本</summary>
      <p className="my-3 break-all text-xs text-secondary">输入版本 {report.input_version} · 规则版本 {report.rule_version}</p>
      <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-xs">
        <caption className="sr-only">数据证据来源和可获得时间</caption>
        <thead className="border-b border-border text-secondary"><tr>{['指标', '数值', '来源', '行情时点', '可获得时点'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{report.evidence.map(item => <tr key={item.id} className="border-b border-border/60"><td className="py-3 pr-4">{item.label}</td><td className="py-3 pr-4 font-mono">{evidenceValue(item.value, item.unit)}</td><td className="py-3 pr-4">{item.source}</td><td className="py-3 pr-4">{time(item.observed_at)}</td><td className="py-3">{time(item.available_at)}</td></tr>)}</tbody>
      </table></div>
    </details>
  </>
}

export function MarketGame() {
  const client = useQueryClient()
  const [asOf, setAsOf] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [risk, setRisk] = useState<RiskDraft>(() => Object.fromEntries(riskFields.map(field => [field.key, field.initial])) as RiskDraft)
  const [validation, setValidation] = useState('')
  const [preview, setPreview] = useState<NavItem | null>(null)
  const [observe, setObserve] = useState(false)
  const history = useQuery({ queryKey: QK.marketGameReports, queryFn: api.marketGameReports, retry: false })
  const reportQuery = useQuery({ queryKey: QK.marketGameReport(selectedId), queryFn: () => api.marketGameReport(selectedId), enabled: !!selectedId, staleTime: Infinity, retry: false })
  const report = reportQuery.data?.id === selectedId ? reportQuery.data : undefined
  const explanation = useQuery<{ content: string }>({ queryKey: QK.marketGameExplanation(selectedId), queryFn: skipToken })
  const evaluation = useQuery({ queryKey: QK.marketGameEvaluation(selectedId), queryFn: () => api.marketGameEvaluation(selectedId), enabled: !!selectedId && observe, retry: false, staleTime: 60_000 })
  const generate = useMutation({
    mutationFn: (body: { as_of?: string; risk?: MarketGameRisk }) => api.marketGameAnalyze(body),
    onSuccess: result => {
      client.setQueryData(QK.marketGameReport(result.id), result)
      setSelectedId(result.id)
      void client.invalidateQueries({ queryKey: QK.marketGameReports })
    },
  })
  const explain = useMutation({ mutationFn: (id: string) => api.marketGameExplain(id), onSuccess: (result, id) => client.setQueryData(QK.marketGameExplanation(id), result) })
  const explaining = explain.isPending && explain.variables === selectedId
  const explainError = explain.isError && explain.variables === selectedId
  useEffect(() => { setPreview(null); setObserve(false) }, [selectedId])

  function changeDate(value: string) {
    setAsOf(value)
    setSelectedId('')
    setPreview(null)
    generate.reset()
    setValidation('')
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    if (generate.isPending) return
    const invalid = riskFields.some(field => !risk[field.key].trim() || !Number.isFinite(Number(risk[field.key])) || Number(risk[field.key]) < field.min || Number(risk[field.key]) > field.max)
      || !Number.isInteger(Number(risk.max_candidates))
    if (invalid) { setValidation('请填写有效的风险参数；总仓位可设为 0，仅观察。'); return }
    if (asOf && (!/^\d{4}-\d{2}-\d{2}$/.test(asOf) || asOf > beijingToday())) { setValidation('请选择不晚于今天的盘后日期。'); return }
    setValidation('')
    const parameters = Object.fromEntries(riskFields.map(field => [field.key, Number(risk[field.key]) / field.divisor])) as unknown as MarketGameRisk
    generate.mutate({ ...(asOf ? { as_of: asOf } : {}), risk: parameters })
  }

  return <>
    <PageHeader title="超短线博弈" titleExtra={<Scale className="h-4 w-4 text-secondary" />} className="pl-14 md:pl-5" />
    <div className="mx-auto max-w-6xl space-y-5 px-4 py-5 sm:px-6">
      <p className="max-w-prose text-sm leading-relaxed text-secondary">从市场状态出发，检验情绪与行为假设，形成未来 1–3 个交易日的条件计划。</p>
      <form noValidate onSubmit={submit} className="space-y-4">
        <fieldset disabled={generate.isPending} className="space-y-4">
          <legend className="sr-only">盘后分析参数</legend>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1.5 text-xs text-secondary">盘后日期<input type="date" aria-label="盘后日期" value={asOf} max={beijingToday()} onChange={event => changeDate(event.target.value)} className={inputClass} disabled={generate.isPending} /></label>
            <button type="button" onClick={() => changeDate('')} className={button}>最新盘后</button>
            <button type="submit" disabled={generate.isPending} className={cn(button, 'border-accent bg-accent/10 font-medium hover:bg-accent/20')}><RefreshCw className={cn('h-4 w-4', generate.isPending && 'animate-spin motion-reduce:animate-none')} />{generate.isPending ? '生成中…' : '生成盘后计划'}</button>
          </div>
          <p className="text-xs leading-relaxed text-secondary">{asOf ? '指定历史日期仅按当时可核验的信息研究，缺少历史板块成员时不会生成个股候选。' : '日期留空时使用最近可用盘后行情，当前板块信息会按真实获取时点标注。'}</p>
          <details className="rounded-btn border border-border px-3">
            <summary className="min-h-10 cursor-pointer py-3 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">风险参数<span className="ml-3 text-xs font-normal text-secondary">控制新计划上限</span></summary>
            <div className="grid grid-cols-1 gap-3 pb-4 sm:grid-cols-2 lg:grid-cols-3">
              {riskFields.map(field => <label key={field.key} className="flex flex-col gap-1.5 text-xs text-secondary">{field.label}<input type="number" aria-label={field.label} min={field.min} max={field.max} step={field.step} value={risk[field.key]} onChange={event => { setRisk(current => ({ ...current, [field.key]: event.target.value })); setValidation('') }} className={inputClass} /></label>)}
            </div>
            <p className="pb-4 text-xs leading-relaxed text-secondary">比例按账户总资产计算。单股与板块实际额度同时受总仓上限约束；单笔预算用于压力情景估算，不保证最大损失。</p>
          </details>
        </fieldset>
        {validation && <p role="alert" className="text-sm text-foreground">{validation}</p>}
      </form>

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
        <label htmlFor="market-game-history" className="text-sm font-medium">历史计划</label>
        <select id="market-game-history" aria-label="历史计划" className={cn(inputClass, 'w-full min-w-0 sm:w-auto sm:max-w-lg')} value={selectedId} disabled={generate.isPending} onChange={event => {
          const id = event.target.value
          setSelectedId(id)
          setPreview(null)
          generate.reset()
        }}>
          <option value="">选择已归档计划</option>
          {report && !history.data?.reports.some(item => item.id === report.id) && <option value={report.id}>{report.as_of} · {time(report.created_at)}</option>}
          {history.data?.reports.map(item => <option key={item.id} value={item.id}>{item.as_of} · {time(item.created_at)} · {item.research_only ? '研究计划' : qualityLabels[item.quality]}</option>)}
        </select>
        {history.isLoading && <span role="status" className="text-xs text-secondary">正在读取历史…</span>}
        {history.isError && <div className="flex flex-wrap items-center gap-2"><p role="status" className="text-xs text-secondary">历史计划读取失败：{message(history.error)}</p><button type="button" className={button} onClick={() => void history.refetch()}>重试历史</button></div>}
      </div>
      {generate.isPending && <p role="status" className="text-sm text-secondary">正在核验盘后数据并生成条件计划。{report ? '下方保留上一份报告，完成后切换。' : '完成后将自动归档。'}</p>}
      {generate.isError && <p role="alert" className="rounded-btn border border-border p-3 text-sm">生成失败：{message(generate.error)}{report && ' 当前仍显示上一份已归档计划。'}</p>}
      {selectedId && reportQuery.isLoading && <div role="status" className="space-y-3 py-4"><p className="text-sm text-secondary">正在读取计划…</p><div aria-hidden className="h-24 animate-pulse rounded-card bg-elevated motion-reduce:animate-none" /></div>}
      {selectedId && reportQuery.isError && <div role="alert" className="space-y-2"><p className="text-sm">计划读取失败：{message(reportQuery.error)}</p><button type="button" className={button} onClick={() => void reportQuery.refetch()}>重试读取</button></div>}
      {!selectedId && !generate.isPending && <div className="border-y border-border py-12"><h2 className="text-[16px] leading-6 font-semibold">选择盘后日期，生成第一份条件计划</h2><p className="mt-2 max-w-prose text-sm leading-relaxed text-secondary">计划会同时记录依据、另一种解释和失效条件。也可选择历史计划，对照后续观察表现。</p></div>}

      {report && <div>
        <ReportBody key={report.id} report={report} onOpen={candidate => setPreview({ symbol: candidate.symbol, name: candidate.name })} />
        <Section title="AI 解读" hint="可选。解释已保存的证据和条件，不修改规则结论、候选或仓位。">
          <button type="button" className={button} disabled={explaining || report.quality === 'unavailable'} onClick={() => explain.mutate(report.id)}><Sparkles className="h-4 w-4" />{explaining ? '解读生成中…' : '生成 AI 解读'}</button>
          {report.quality === 'unavailable' && <p className="mt-2 text-sm text-secondary">输入不足，暂不生成 AI 解读。</p>}
          {explainError && <p role="alert" className="mt-3 text-sm">解读失败：{message(explain.error)}。规则计划仍可查看。</p>}
          {explanation.data?.content && <div className="mt-4 max-w-prose break-words"><MarkdownRenderer content={explanation.data.content} /></div>}
        </Section>
        <Section title="后续观察" hint="观察收益不等于触发或实盘收益。这里只对照候选后续收盘变化，不假设成交，也不计算策略胜率。">
          <button type="button" className={button} disabled={evaluation.isFetching} onClick={() => { if (observe) void evaluation.refetch(); else setObserve(true) }}>{evaluation.isFetching ? '读取中…' : observe ? '刷新后续表现' : '查看后续表现'}</button>
          {observe && evaluation.isError && <p role="alert" className="mt-3 text-sm">后续行情读取失败：{message(evaluation.error)}</p>}
          {observe && evaluation.data?.report_id === report.id && <div className="mt-4"><Observation data={evaluation.data} /></div>}
        </Section>
      </div>}
    </div>
    {preview && report && <StockPreviewDialog symbol={preview.symbol} name={preview.name} navList={report.candidates.map(candidate => ({ symbol: candidate.symbol, name: candidate.name }))}
      triggerInfo={{ message: `报告日期 ${report.as_of}；此详情为当前数据，不是历史时点回放。` }} onClose={() => setPreview(null)} onNavigate={(symbol, name) => setPreview({ symbol, name })} />}
  </>
}
