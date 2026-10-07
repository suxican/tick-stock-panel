import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, BrainCircuit, ChartNoAxesCombined, ClipboardList, RefreshCw, Scale, ShieldCheck, Sparkles, Timer } from 'lucide-react'
import { api, type MarketGameCandidate, type MarketGameEvaluation, type MarketGameReport, type MarketGameRisk } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import type { NavItem } from '@/lib/listNav'
import { PsychologyPanel } from './PsychologyPanel'
import { CapitalPanel } from './CapitalPanel'
import { OverseasPanel } from './OverseasPanel'
import { ModelPanel } from './ModelPanel'

const button = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-btn border border-border px-3 py-2 text-sm text-foreground transition-colors hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
const inputClass = 'min-h-10 rounded-btn border border-border bg-surface px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-50'
const qualityLabels = { ready: '输入可用', limited: '数据有限', unavailable: '暂不可分析' }
const hypothesisLabels = { matched: '符合当前证据', watch: '等待确认', inactive: '暂不成立' }
const modeLabels: Record<string, string> = { panic_repair: '恐慌修复', trend_pullback: '趋势回撤', crowded_exit: '拥挤退潮' }
const bullishConclusion = 'text-red-700 dark:text-bull'
const bearishConclusion = 'text-emerald-700 dark:text-bear'
const cautionConclusion = 'text-amber-700 dark:text-warning'
// 方向沿用 A 股红涨绿跌；过热和分歧单独提示，未知结论保持中性。
const conclusionColors: Record<string, string> = {
  上行: bullishConclusion, 启动: bullishConclusion, 主升: bullishConclusion,
  偏强: bullishConclusion, 改善: bullishConclusion,
  下行: bearishConclusion, 退潮: bearishConclusion, 冰点: bearishConclusion,
  偏弱: bearishConclusion, 转弱: bearishConclusion, 低迷: bearishConclusion,
  修复: 'text-blue-700 dark:text-accent',
  高潮: cautionConclusion, 偏热: cautionConclusion, 分歧: cautionConclusion, 偏高: cautionConclusion,
}
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
  return <section aria-label={title} className="min-w-0 rounded-card border border-border bg-surface/80 p-4">
    <h2 className="text-sm font-semibold text-foreground">{title}</h2>
    {hint && <p className="mt-1 max-w-prose text-xs leading-relaxed text-secondary">{hint}</p>}
    <div className="mt-4">{children}</div>
  </section>
}

const riskFields = [
  { key: 'total_cap', label: '本计划新增仓位上限（%）', initial: '50', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'single_cap', label: '单股上限（%）', initial: '15', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'sector_cap', label: '板块上限（%）', initial: '25', min: 0, max: 100, step: .1, divisor: 100 },
  { key: 'risk_per_trade', label: '单笔风险预算（%）', initial: '0.5', min: .01, max: 2, step: .01, divisor: 100 },
  { key: 'portfolio_risk_budget', label: '组合压力预算（%）', initial: '1.5', min: 0, max: 10, step: .01, divisor: 100 },
  { key: 'min_amount', label: '最低日成交额（亿元）', initial: '1', min: .1, max: 1000, step: .1, divisor: 1e-8 },
  { key: 'max_candidates', label: '候选数量上限', initial: '5', min: 1, max: 5, step: 1, divisor: 1 },
] as const
type RiskDraft = Record<keyof MarketGameRisk, string>
type View = 'overview' | 'psychology' | 'capital' | 'model' | 'evidence'
const views = [
  { id: 'overview', label: '博弈总览', icon: Activity },
  { id: 'psychology', label: '情绪与心理', icon: BrainCircuit },
  { id: 'capital', label: '资金行为与情绪博弈', icon: ChartNoAxesCombined },
  { id: 'model', label: '存量博弈模型', icon: Scale },
  { id: 'evidence', label: '证据与复盘', icon: ClipboardList },
] as const

function ReportWindow({ report }: { report: MarketGameReport }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 60_000); return () => window.clearInterval(timer) }, [])
  const validity = report.validity
  const expired = !!validity?.expires_at && Number.isFinite(Date.parse(validity.expires_at)) && now > Date.parse(validity.expires_at)
  const entryEnded = !!validity?.entry_session && now > Date.parse(`${validity.entry_session}T15:00:00+08:00`)
  const entryPending = !!validity?.entry_session && now < Date.parse(`${validity.entry_session}T09:30:00+08:00`)
  const label = expired ? '观察计划已到期' : validity?.status === 'research_only' ? '历史研究窗口' : validity?.status === 'scheduled' ? (entryEnded ? '入场窗口已结束' : entryPending ? '等待入场观察日' : '入场观察窗口内') : '有效交易日待核验'
  return <div className="flex flex-wrap items-start gap-2 border-t border-border pt-2 text-xs leading-relaxed text-secondary">
    <Timer className="mt-0.5 h-3.5 w-3.5 shrink-0" /><div className="min-w-0 flex-1"><span className={cn('font-medium', expired ? 'text-warning' : 'text-foreground')}>{label}</span>
      {validity?.entry_session && <span className="ml-3">入场观察日 {validity.entry_session}</span>}
      {validity?.expires_at && <span className="ml-3">计划到期 {time(validity.expires_at)}</span>}
      {entryEnded && expired && <span className="ml-3">入场窗口已结束</span>}
      <p className="mt-1">{expired ? '此报告保留生成时的证据与计划。过期的入场条件不再作为当前操作依据。' : entryEnded ? '未触发的入场计划不再启用；已持有部分仍按原退出条件复核。' : validity?.reason ?? '这份旧报告没有冻结的有效交易日信息，不推定它仍可执行。'}</p>
    </div>
  </div>
}

function Candidate({ candidate, onOpen }: { candidate: MarketGameCandidate; onOpen: () => void }) {
  const structured = !!candidate.conditions?.length
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
      <div><dt className="text-secondary">单股新增上限</dt><dd className="mt-1 font-mono">{pct(candidate.max_position)}</dd></div>
    </dl>
    <p className="mt-3 text-sm text-secondary">计划持有不超过 {candidate.holding_days} 个交易日 · 压力情景跌幅 {pct(candidate.stress_loss_pct)}</p>
    {(candidate.score != null || candidate.pressure_price != null || candidate.reward_risk_ratio != null) && <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary"><span>候选评分 {candidate.score == null ? '—' : candidate.score.toFixed(1)}</span><span>压力参考价 {price(candidate.pressure_price ?? null)}</span><span>参考盈亏比 {candidate.reward_risk_ratio == null ? '—' : candidate.reward_risk_ratio.toFixed(2)}</span></p>}
    <details className="mt-3 text-sm">
      <summary className="min-h-10 cursor-pointer py-2 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">查看依据、触发与退出</summary>
      <div className="mt-2 grid gap-4 text-secondary md:grid-cols-2">
        {([['入选依据', candidate.evidence], ...(!structured ? [['触发条件', candidate.trigger], ['失效条件', candidate.invalidation]] as const : []), ['退出规则', candidate.exit_rules]] as const).map(([label, items]) => <div key={label}><h3 className="mb-1 font-medium text-foreground">{label}</h3><Lines items={[...items]} /></div>)}
        {structured && <>{(['entry', 'cancel'] as const).map(phase => <div key={phase}><h3 className="mb-1 font-medium text-foreground">{phase === 'entry' ? '入场：全部满足' : '取消：任一成立'}</h3><Lines items={candidate.conditions!.filter(item => item.phase === phase).map(item => item.description)} /></div>)}<p className="text-xs md:col-span-2">以上是冻结的条件定义，尚未接入实时核验，不代表已经触发或成交。</p></>}
      </div>
    </details>
  </article>
}

function Observation({ data }: { data: MarketGameEvaluation }) {
  const stateLabels = { pending: '待观察', available: '已观察', missing: '行情缺失', unavailable: '无法核验' }
  const planLabels: Record<string, string> = { pending: '等待入场观察日', entry_window: '入场观察窗口内', observation_only: '入场已结束，仅观察持有期', expired: '观察计划已到期', unverified: '有效交易日待核验', research_only: '历史研究窗口' }
  return <>
    <p className="mb-3 text-xs leading-relaxed text-secondary">更新于 {time(data.evaluated_at)}（北京时间）{data.calendar_verified != null && <span className="ml-3">{data.calendar_verified ? '独立交易日历已核验 · 缺失行情不顺延' : '交易日历未核验 · 按已观测市场日排序'}</span>}</p>
    {data.plan_status && planLabels[data.plan_status] && <p className="mb-3 text-xs text-secondary">评估时的计划状态：{planLabels[data.plan_status]}</p>}
    {data.rows.length > 0 ? <div className="overflow-x-auto">
      <table className="w-full min-w-[940px] text-left text-xs">
        <caption className="sr-only">报告候选后续收盘观察表现</caption>
        <thead className="border-b border-border text-secondary"><tr>{['个股', '观察序列', '实际行情日', '收盘观察收益', '基准收益', '超额收益', '期间最高 / 最低', '状态'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{data.rows.map(row => <tr key={`${row.symbol}-${row.horizon}`} className="border-b border-border/60"><td className="py-3 pr-4">{row.name}<span className="block font-mono text-xs text-secondary">{row.symbol}</span></td><td className="pr-4">{row.label}</td><td className="pr-4 font-mono">{row.trade_date ?? '—'}</td><td className="pr-4 font-mono">{row.state === 'available' ? pct(row.close_return) : '—'}</td><td className="pr-4 font-mono">{pct(row.benchmark_return)}</td><td className="pr-4 font-mono">{row.state === 'available' ? pct(row.excess_return) : '—'}</td><td className="pr-4 font-mono">{pct(row.max_upside)} / {pct(row.max_drawdown)}</td><td>{stateLabels[row.state]}</td></tr>)}</tbody>
      </table>
    </div> : <p className="text-sm text-secondary">暂没有可核验的候选后续行情。</p>}
    <p className="mt-3 text-xs leading-relaxed text-secondary">期间最高 / 最低为相对报告收盘价的观察期价格幅度，不是入场后的收益或回撤；未核验数据以 — 显示。</p>
    <div className="mt-3 text-sm text-secondary"><Lines items={data.limitations} /></div>
  </>
}

function ReportBody({ report, view, onOpen }: { report: MarketGameReport; view: View; onOpen: (candidate: MarketGameCandidate) => void }) {
  const { allocation, market_state: market } = report
  return <>
    <div className="rounded-card border border-border bg-surface/80 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="rounded bg-elevated px-2 py-1 font-medium">{qualityLabels[report.quality]}</span>
        {report.research_only && <span className="rounded border border-border px-2 py-1 font-medium">研究阶段计划</span>}
        <span className="text-secondary">行情归属 {report.as_of}</span>
      </div>
      {view === 'overview' ? <><h2 className="mt-2 max-w-prose text-sm font-semibold leading-relaxed">{report.summary}</h2><p className="mt-1 max-w-prose text-xs leading-relaxed text-secondary">{market.change}</p></> : <details className="mt-1 text-xs"><summary className="min-h-7 cursor-pointer py-1 text-secondary">查看冻结的市场结论</summary><p className="mt-1 max-w-prose leading-relaxed">{report.summary}</p><p className="mt-1 max-w-prose leading-relaxed text-secondary">{market.change}</p></details>}
      <p className="mb-2 mt-1 text-xs leading-relaxed text-secondary">数据截止 {time(report.cutoff)} · 生成 {time(report.created_at)}（北京时间）</p>
      <ReportWindow report={report} />
    </div>
    <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {([['趋势', market.trend], ['周期', market.phase], ['情绪', market.emotion], ['拥挤程度', market.crowding]] as const).map(([label, value]) => <div key={label} className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1 rounded-card border border-border bg-surface/80 px-3 py-2"><dt className="text-xs text-secondary">{label}</dt><dd className={cn('break-words text-sm font-semibold', conclusionColors[value] ?? 'text-secondary')}>{value}</dd></div>)}
      </dl>

    {report.limitations.length > 0 && <section aria-label="数据限制" className="rounded-card border border-border bg-surface/50 px-4 py-1 text-xs">
      <details><summary className="min-h-8 cursor-pointer py-1.5 font-medium">本次分析的边界 · {report.limitations.length} 项</summary>
      <div className="mt-2 max-w-prose text-secondary"><Lines items={report.limitations} /></div></details>
      {report.quality === 'unavailable' && <a className="mt-2 inline-flex min-h-10 items-center underline underline-offset-4" href="/settings?tab=data-sources">检查数据源配置</a>}
    </section>}

    {view === 'overview' && <>
    <OverseasPanel data={report.overseas} mode="frozen" />
    <section aria-label="新增敞口与压力预算" className="grid gap-4 rounded-card border border-border bg-surface/80 p-4 lg:grid-cols-[1.2fr_1fr]">
      <div><h2 className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="h-4 w-4 text-accent" />本计划新增敞口</h2>
        <p className="mt-2 font-mono text-2xl font-semibold">{pct(allocation.min)} – {pct(allocation.max)}</p>
        <p className="mt-2 max-w-prose text-xs leading-relaxed text-secondary">{allocation.reason}</p>
        <p className="mt-2 text-xs leading-relaxed text-secondary">上限以账户总资产为分母，未扣除已有持仓，不代表账户目标总仓。</p>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
        {([['模型新增上限', allocation.total_cap], ['单股新增上限', allocation.single_cap], ['板块新增上限', allocation.sector_cap], ['单笔风险预算', allocation.risk_per_trade], ['组合压力预算', allocation.portfolio_risk_budget], ['估算压力损失', allocation.estimated_stress_loss]] as const).map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 font-mono text-sm">{pct(value)}</dd></div>)}
      </dl>
      <p className="text-xs leading-relaxed text-secondary lg:col-span-2">胜率与情景概率尚未校准；行为解释是可证伪的假设。压力预算不保证最大损失，此处不推断账户余额或执行交易。</p>
    </section>
    <Section title="未来 1–3 个交易日" hint="按条件逐日复核。先满足触发条件，再考虑对应仓位；情景未确认时不执行。">
      {report.scenarios.length ? <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left text-sm">
        <caption className="sr-only">未来交易日情景与仓位条件</caption>
        <thead className="border-b border-border text-secondary"><tr>{['窗口', '情景', '确认条件', '应对', '本计划仓位上限'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{report.scenarios.map((scenario, index) => <tr key={`${scenario.horizon}-${index}`} className="border-b border-border/60 align-top"><td className="whitespace-nowrap py-3 pr-4">{scenario.label}</td><td className="py-3 pr-4">{scenario.scenario}</td><td className="max-w-xs py-3 pr-4 leading-relaxed">{scenario.condition}</td><td className="max-w-xs py-3 pr-4 leading-relaxed">{scenario.action}</td><td className="py-3 font-mono">{pct(scenario.max_position)}</td></tr>)}</tbody>
      </table></div> : <p className="text-sm text-secondary">数据不足，暂不形成情景计划。</p>}
    </Section>
    </>}

    {view === 'psychology' && <>
    <PsychologyPanel key={report.id} psychology={report.psychology} />
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
    </>}

    {view === 'overview' && <>
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
    </>}

    {view === 'evidence' && <Section title="原始证据与版本" hint="这里保留报告生成时的依据，历史报告不会随着新行情改写。">
      <p className="my-3 break-all text-xs text-secondary">输入版本 {report.input_version} · 规则版本 {report.rule_version}</p>
      <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-xs">
        <caption className="sr-only">数据证据来源和可获得时间</caption>
        <thead className="border-b border-border text-secondary"><tr>{['指标', '数值', '来源', '行情时点', '可获得时点'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
        <tbody>{report.evidence.map(item => <tr key={item.id} className="border-b border-border/60"><td className="py-3 pr-4">{item.label}</td><td className="py-3 pr-4 font-mono">{evidenceValue(item.value, item.unit)}</td><td className="py-3 pr-4">{item.source}</td><td className="py-3 pr-4">{time(item.observed_at)}</td><td className="py-3">{time(item.available_at)}</td></tr>)}</tbody>
      </table></div>
    </Section>}
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
  const [view, setView] = useState<View>('overview')
  const [riskLoaded, setRiskLoaded] = useState(false)
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
  useEffect(() => { setPreview(null); setObserve(false); setRiskLoaded(false) }, [selectedId])

  function restoreRisk() {
    if (!report?.requested_risk) return
    const original = report.requested_risk
    setRisk(Object.fromEntries(riskFields.map(field => [field.key, original[field.key] == null ? field.initial : String(Number((original[field.key]! * field.divisor).toPrecision(12)))])) as RiskDraft)
    setRiskLoaded(true)
    setValidation('')
  }

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
    if (invalid) { setValidation('请填写有效的风险参数；新增仓位上限和组合压力预算可设为 0，仅观察。'); return }
    if (asOf && (!/^\d{4}-\d{2}-\d{2}$/.test(asOf) || asOf > beijingToday())) { setValidation('请选择不晚于今天的盘后日期。'); return }
    setValidation('')
    const parameters = Object.fromEntries(riskFields.map(field => [field.key, Number(risk[field.key]) / field.divisor])) as unknown as MarketGameRisk
    generate.mutate({ ...(asOf ? { as_of: asOf } : {}), risk: parameters })
  }

  return <>
    <PageHeader title="超短线博弈" titleExtra={<Scale className="h-4 w-4 text-accent" />} subtitle={<span className="hidden lg:inline">市场状态 · 行为假设 · 条件计划</span>} className="pl-14 md:pl-5" />
    <div className="mx-auto max-w-[1440px] space-y-3 px-4 py-3 sm:px-5">
      <details open className="rounded-card border border-border bg-surface/80 px-3">
        <summary className="min-h-9 cursor-pointer py-2 text-xs font-medium">生成设置<span className="ml-3 font-normal text-secondary">{asOf || '最新盘后'} · 新增仓位上限 {risk.total_cap}%</span></summary>
      <form noValidate onSubmit={submit} className="space-y-2 pb-2">
        <fieldset disabled={generate.isPending} className="space-y-2">
          <legend className="sr-only">盘后分析参数</legend>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-secondary">盘后日期<input type="date" aria-label="盘后日期" value={asOf} max={beijingToday()} onChange={event => changeDate(event.target.value)} className={cn(inputClass, 'min-h-9 py-1.5')} disabled={generate.isPending} /></label>
            <button type="button" onClick={() => changeDate('')} className={button}>最新盘后</button>
            <button type="submit" disabled={generate.isPending} className={cn(button, 'border-accent bg-accent/10 font-medium hover:bg-accent/20')}><RefreshCw className={cn('h-4 w-4', generate.isPending && 'animate-spin motion-reduce:animate-none')} />{generate.isPending ? '生成中…' : '生成盘后计划'}</button>
            <div className="flex w-full min-w-0 items-center gap-2 sm:ml-auto sm:w-auto">
              <label htmlFor="market-game-history" className="shrink-0 text-xs font-medium">历史计划</label>
              <select id="market-game-history" aria-label="历史计划" className={cn(inputClass, 'w-full min-w-0 sm:w-80')} value={selectedId} disabled={generate.isPending} onChange={event => {
                const id = event.target.value
                setSelectedId(id)
                setPreview(null)
                generate.reset()
              }}>
                <option value="">选择已归档计划</option>
                {report && !history.data?.reports.some(item => item.id === report.id) && <option value={report.id}>{report.as_of} · {time(report.created_at)}</option>}
                {history.data?.reports.map(item => <option key={item.id} value={item.id}>{item.as_of} · {time(item.created_at)} · {item.research_only ? '研究计划' : qualityLabels[item.quality]}</option>)}
              </select>
            </div>
          </div>
          <p className="text-xs leading-relaxed text-secondary">{asOf ? '指定历史日期仅按当时可核验的信息研究，缺少历史板块成员时不会生成个股候选。' : '日期留空时使用最近可用盘后行情，当前板块信息会按真实获取时点标注。'}</p>
          <details className="border-t border-border">
            <summary className="min-h-8 cursor-pointer py-2 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">风险参数<span className="ml-3 font-normal text-secondary">控制新计划上限</span></summary>
            <div className="grid grid-cols-1 gap-3 pb-4 sm:grid-cols-2 lg:grid-cols-3">
              {riskFields.map(field => <label key={field.key} className="flex flex-col gap-1.5 text-xs text-secondary">{field.label}<input type="number" aria-label={field.label} min={field.min} max={field.max} step={field.step} value={risk[field.key]} onChange={event => { setRisk(current => ({ ...current, [field.key]: event.target.value })); setValidation(''); setRiskLoaded(false) }} className={inputClass} /></label>)}
            </div>
            <p className="pb-3 text-xs leading-relaxed text-secondary">比例按账户总资产计算，仅约束本计划新增敞口，未扣除已有持仓。单股与板块额度同时受模型新增上限约束；压力预算不保证最大损失。</p>
            {report && <div className="flex flex-wrap items-center gap-2 pb-3"><button type="button" className={button} disabled={!report.requested_risk} onClick={restoreRisk}>载入此报告的风险参数</button><p role="status" className="text-xs text-secondary">{riskLoaded ? '已载入原参数；当前生成日期未改变。重新生成会使用所选日期的数据，不等于重放原快照。' : report.requested_risk ? '仅在点击后更新表单；归档报告保持不变。' : '这份旧报告未提供完整原始参数，无法恢复。'}</p></div>}
          </details>
        </fieldset>
        {validation && <p role="alert" className="text-sm text-foreground">{validation}</p>}
      </form>
      </details>

      {(history.isLoading || history.isError || report) && <div className="flex flex-wrap items-center gap-3">
        {history.isLoading && <span role="status" className="text-xs text-secondary">正在读取历史…</span>}
        {history.isError && <div className="flex flex-wrap items-center gap-2"><p role="status" className="text-xs text-secondary">历史计划读取失败：{message(history.error)}</p><button type="button" className={button} onClick={() => void history.refetch()}>重试历史</button></div>}
        {report && <span className="text-xs text-secondary">归档报告 · 证据与原参数冻结</span>}
      </div>}
      {generate.isPending && <p role="status" className="text-sm text-secondary">正在核验盘后数据并生成条件计划。{report ? '下方保留上一份报告，完成后切换。' : '完成后将自动归档。'}</p>}
      {generate.isError && <p role="alert" className="rounded-btn border border-border p-3 text-sm">生成失败：{message(generate.error)}{report && ' 当前仍显示上一份已归档计划。'}</p>}
      {selectedId && reportQuery.isLoading && <div role="status" className="space-y-3 py-4"><p className="text-sm text-secondary">正在读取计划…</p><div aria-hidden className="h-24 animate-pulse rounded-card bg-elevated motion-reduce:animate-none" /></div>}
      {selectedId && reportQuery.isError && <div role="alert" className="space-y-2"><p className="text-sm">计划读取失败：{message(reportQuery.error)}</p><button type="button" className={button} onClick={() => void reportQuery.refetch()}>重试读取</button></div>}
      {!selectedId && !generate.isPending && <div className="border-y border-border py-12"><h2 className="text-[16px] leading-6 font-semibold">选择盘后日期，生成第一份条件计划</h2><p className="mt-2 max-w-prose text-sm leading-relaxed text-secondary">计划会同时记录依据、另一种解释和失效条件。也可选择历史计划，对照后续观察表现。</p></div>}

      {report && <>
        <div role="tablist" aria-label="博弈分析视图" className="flex w-fit max-w-full gap-1 overflow-x-auto rounded-btn border border-border bg-base/60 p-1">
          {views.map(({ id, label, icon: Icon }, index) => <button key={id} type="button" role="tab" id={`game-tab-${id}`} aria-controls={`game-panel-${id}`} aria-selected={view === id} tabIndex={view === id ? 0 : -1} onClick={() => setView(id)} onKeyDown={event => {
            const next = event.key === 'ArrowRight' ? (index + 1) % views.length : event.key === 'ArrowLeft' ? (index + views.length - 1) % views.length : event.key === 'Home' ? 0 : event.key === 'End' ? views.length - 1 : null
            if (next == null) return
            event.preventDefault(); setView(views[next].id); document.getElementById(`game-tab-${views[next].id}`)?.focus()
          }} className={cn('inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-[5px] px-3 text-xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', view === id ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated hover:text-foreground')}><Icon className="h-3.5 w-3.5" />{label}</button>)}
        </div>
        <div role="tabpanel" id={`game-panel-${view}`} aria-labelledby={`game-tab-${view}`} className="space-y-3">
        <ReportBody report={report} view={view} onOpen={candidate => setPreview({ symbol: candidate.symbol, name: candidate.name })} />
        {view === 'capital' && <CapitalPanel key={report.id} report={report} />}
        {view === 'model' && <ModelPanel key={report.id} report={report} />}
        {view === 'psychology' && <>
        <Section title="AI 解读" hint="可选。解释已保存的证据和条件，不修改规则结论、候选或仓位。">
          <button type="button" className={button} disabled={explaining || report.quality === 'unavailable'} onClick={() => explain.mutate(report.id)}><Sparkles className="h-4 w-4" />{explaining ? '解读生成中…' : '生成 AI 解读'}</button>
          {report.quality === 'unavailable' && <p className="mt-2 text-sm text-secondary">输入不足，暂不生成 AI 解读。</p>}
          {explainError && <p role="alert" className="mt-3 text-sm">解读失败：{message(explain.error)}。规则计划仍可查看。</p>}
          {explanation.data?.content && <div className="mt-4 max-w-prose break-words"><MarkdownRenderer content={explanation.data.content} /></div>}
        </Section>
        </>}
        {view === 'evidence' && <>
        <Section title="后续观察" hint="观察收益不等于触发或实盘收益。这里只对照候选后续收盘变化，不假设成交，也不计算策略胜率。">
          <button type="button" className={button} disabled={evaluation.isFetching} onClick={() => { if (observe) void evaluation.refetch(); else setObserve(true) }}>{evaluation.isFetching ? '读取中…' : observe ? '刷新后续表现' : '查看后续表现'}</button>
          {observe && evaluation.isError && <p role="alert" className="mt-3 text-sm">后续行情读取失败：{message(evaluation.error)}</p>}
          {observe && evaluation.data?.report_id === report.id && <div className="mt-4"><Observation data={evaluation.data} /></div>}
        </Section>
        </>}
      </div></>}
    </div>
    {preview && report && <StockPreviewDialog symbol={preview.symbol} name={preview.name} navList={report.candidates.map(candidate => ({ symbol: candidate.symbol, name: candidate.name }))}
      triggerInfo={{ message: `报告日期 ${report.as_of}；此详情为当前数据，不是历史时点回放。` }} onClose={() => setPreview(null)} onNavigate={(symbol, name) => setPreview({ symbol, name })} />}
  </>
}
