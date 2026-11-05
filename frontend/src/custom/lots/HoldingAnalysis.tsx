import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, RefreshCw } from 'lucide-react'
import { api, type HoldingAnalysisOverview, type HoldingAnalysisProfile, type HoldingAnalysisReport, type HoldingAnalysisRow, type HoldingAnalysisSavedPlan, type HoldingAnalysisSource } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/cn'
import { beijingDate, buttonClass, Empty, Field, formatTime, inputClass, Notice, primaryClass } from './positionTShared'

const ageLabels = { new: '刚建仓', holding: '持有中', overdue: '超过计划周期', unknown: '建仓时间未知' }
const pnlLabels = { deep_loss: '深度浮亏', loss: '浮亏', flat: '成本附近', profit: '浮盈', unknown: '盈亏未知' }
const trendLabels = { up: '趋势向上', range: '区间震荡', down: '趋势走弱', unknown: '趋势未知' }
const strengthLabels = { stronger: '强于板块', inline: '与板块相近', weaker: '弱于板块', unknown: '相对强弱未知' }
const price = (value: number | null) => value == null || !Number.isFinite(value) ? '未知' : fmtPrice(value)
const percent = (value: number | null) => value == null || !Number.isFinite(value) ? '未知' : fmtPct(value)
const defaultProfile = (opened_on: string | null): HoldingAnalysisProfile => ({ opened_on, horizon: 'short', thesis: '', invalidation: '', max_loss_pct: null, deep_loss_pct: 15, new_position_days: 3, max_holding_days: 5, primary_sector_id: '' })

export function HoldingAnalysis() {
  const [source, setSource] = useState<HoldingAnalysisSource>('lots')
  const [accountId, setAccountId] = useState('')
  const [today, setToday] = useState(beijingDate)
  const accountOptions = useRef<HoldingAnalysisOverview['accounts']>([])
  useEffect(() => { const timer = window.setInterval(() => setToday(beijingDate()), 60_000); return () => window.clearInterval(timer) }, [])
  const overview = useQuery({ queryKey: QK.holdingAnalysisOverview(source, accountId), queryFn: () => api.holdingAnalysisOverview(source, accountId), staleTime: 30_000 })
  const data = overview.data?.source === source && (overview.data.account_id ?? '') === accountId ? overview.data : undefined
  if (data && source === 'position_t') accountOptions.current = data.accounts

  return <section className="mx-auto max-w-6xl space-y-5 pb-6">
    <div className="space-y-2">
      <h2 className="text-base font-semibold text-foreground">持仓分析与计划</h2>
      <p className="max-w-3xl text-xs leading-relaxed text-secondary">结合持仓阶段、成本价差、个股走势与关联板块，制作下一步的条件计划。采纳计划只保存研究结论，不会下单，也不预留资金或库存。</p>
    </div>
    <div className="grid gap-3 sm:flex sm:flex-wrap sm:items-end">
      <Field label="分析来源"><select aria-label="分析来源" className={cn(inputClass, 'sm:w-48')} value={source} onChange={event => { setSource(event.target.value as HoldingAnalysisSource); setAccountId('') }}><option value="lots">批次参考</option><option value="position_t">账户持仓</option></select></Field>
      {source === 'position_t' && <Field label="分析账户"><select aria-label="分析账户" className={cn(inputClass, 'sm:w-48')} value={accountId} onChange={event => setAccountId(event.target.value)}><option value="">请选择账户</option>{accountOptions.current.map(account => <option key={account.id} value={account.id}>{account.name}</option>)}</select></Field>}
      <button className={buttonClass} disabled={overview.isFetching} onClick={() => void overview.refetch()}><RefreshCw size={14} />重新读取</button>
    </div>
    <Notice>{source === 'lots' ? '批次参考：数量与成本仅用于分析，不代表可卖库存或完整账户仓位；同股批次不与账户持仓重复相加。' : '账户持仓：使用选定账户的核对快照。可卖未知、核对过期或有未完成做 T 计划时，仅作研究并列出约束。'}</Notice>
    {overview.isLoading && <p role="status" className="py-8 text-center text-xs text-secondary">正在读取持仓与研究计划…</p>}
    {overview.isError && <Notice error>{overview.error.message}。可点击“重新读取”重试。</Notice>}
    {data && <HoldingWorkspace key={`${source}:${accountId}`} source={source} accountId={accountId} data={data} today={today} unavailable={overview.isError} />}
  </section>
}

function HoldingWorkspace({ source, accountId, data, today, unavailable }: { source: HoldingAnalysisSource; accountId: string; data: HoldingAnalysisOverview; today: string; unavailable: boolean }) {
  const qc = useQueryClient()
  const [localReport, setLocalReport] = useState<HoldingAnalysisReport | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [requiresRefresh, setRequiresRefresh] = useState(false)
  const [notice, setNotice] = useState('')
  const positionNodes = useRef<Record<string, HTMLDivElement | null>>({})
  const report = localReport ?? data.report
  const refresh = () => qc.invalidateQueries({ queryKey: QK.holdingAnalysisOverview(source, accountId) })
  const analyze = useMutation({
    mutationFn: () => api.holdingAnalysisAnalyze({ source, account_id: accountId }),
    onSuccess: result => { setLocalReport(result); setRequiresRefresh(false); setNotice(''); void refresh() },
    onError: () => setRequiresRefresh(true),
  })
  const adopt = useMutation({
    mutationFn: (rowKey: string) => api.holdingAnalysisAdopt({ report_id: report!.id, row_key: rowKey, expected_input_version: report!.input_version }),
    onSuccess: () => { setNotice('研究计划已采纳，可在下方查看和绑定价格提醒。'); void refresh() },
  })
  const currentReport = report && report.source === source && (report.account_id ?? '') === accountId ? report : null
  const stale = requiresRefresh || unavailable || (currentReport != null && currentReport.input_version !== data.input_version)

  return <div className="space-y-6">
    {!!data.warnings.length && <Notice>{data.warnings.join('；')}</Notice>}
    {source === 'position_t' && !accountId ? <Empty title="请选择账户">选择需要分析的账户后，再读取该账户的持仓。</Empty> : <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">分析范围 <span className="font-normal text-secondary">{data.positions.length} 只</span></h3>
        <button className={primaryClass} disabled={!data.positions.length || analyze.isPending || unavailable} onClick={() => analyze.mutate()}>{analyze.isPending ? '正在分析…' : currentReport ? '重新生成分析' : '生成分析'}</button>
      </div>
      {!data.positions.length ? <Empty title="暂无可分析记录">{source === 'lots' ? '请先在“批次提醒”登记个股和参考成本。' : '请先在“持仓做 T”中添加并核对该账户的持仓。'}</Empty> : <div className="divide-y divide-border rounded-lg border border-border">
        {data.positions.map(position => {
          const settings = data.profiles[position.key]
          return <div key={position.key} ref={node => { positionNodes.current[position.key] = node }} className="p-3 sm:p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0"><span className="break-words text-xs font-medium text-foreground">{position.name || position.symbol}</span><span className="ml-2 font-mono text-xs text-secondary">{position.symbol}</span><p className="mt-1 text-xs text-secondary">{source === 'lots' ? '参考数量' : '登记持仓'} {position.qty == null ? '未知' : `${position.qty} 股`} · 参考成本 {price(position.cost_price)} · 建仓日 {settings?.profile.opened_on ?? position.opened_on ?? '未知'}</p></div>
              <button className={buttonClass} onClick={() => setEditing(editing === position.key ? null : position.key)}>{editing === position.key ? '收起研究设置' : '编辑研究设置'}</button>
            </div>
            {editing === position.key && <ProfileEditor key={`${position.key}:${settings?.version ?? 0}`} rowKey={position.key} source={source} accountId={accountId} profile={settings?.profile ?? defaultProfile(position.opened_on)} version={settings?.version ?? 0} onSaved={() => { setEditing(null); setRequiresRefresh(true); setNotice('设置已保存，请重新生成分析。'); void refresh() }} />}
          </div>
        })}
      </div>}
      {analyze.isError && <Notice error>{analyze.error.message}。请核对数据后重新生成分析。</Notice>}
      {adopt.isError && <Notice error>{adopt.error.message}。请重新读取持仓并生成分析。</Notice>}
      {notice && <Notice>{notice}</Notice>}
      {currentReport && <div className="space-y-4">
        <div><h3 className="text-sm font-semibold text-foreground">分析结果</h3><p className="mt-1 text-xs text-secondary">分析基准 {currentReport.as_of_date} · 生成于 {formatTime(currentReport.generated_at)} · 计划采纳后保留当时依据</p></div>
        <InterpretationPanel key={currentReport.id} report={currentReport} disabled={stale || analyze.isPending} />
        {stale && <Notice>输入或研究设置已改变，或本次刷新未成功。以下保留上次分析，请重新生成后采纳。</Notice>}
        {!!currentReport.warnings.length && <Notice>{currentReport.warnings.join('；')}</Notice>}
        {!currentReport.rows.length ? <Empty title="本次没有可分析结果">请检查持仓来源与数据覆盖，完善设置后重新生成。</Empty> : <div className="divide-y divide-border rounded-lg border border-border">{currentReport.rows.map(row => <AnalysisRow key={row.key} row={row} expired={!!row.plan.expires_on && row.plan.expires_on < today} onEdit={() => { setEditing(row.key); positionNodes.current[row.key]?.scrollIntoView?.({ block: 'start' }) }} onAdopt={() => adopt.mutate(row.key)} adoptDisabled={stale || analyze.isPending || adopt.isPending || !row.plan.can_adopt || (!!row.plan.expires_on && row.plan.expires_on < today)} />)}</div>}
      </div>}
    </>}
    <div className="space-y-3">
      <div><h3 className="text-sm font-semibold text-foreground">已保存的研究计划</h3><p className="mt-1 text-xs leading-relaxed text-secondary">仅价格提醒：只检测所选价格阈值，板块与量价等完整条件仍需人工复核；不会下单，也不代表实际成交。价格提醒仅当日有效；跨日、持仓/设置变化或行情过期时暂停，重新分析后绑定。</p></div>
      {!data.plans.length ? <Empty title="尚未采纳研究计划">生成分析、核对依据后，可按个股采纳条件计划。</Empty> : <div className="divide-y divide-border rounded-lg border border-border">{data.plans.map(plan => <SavedPlan key={`${plan.id}:${plan.version}`} plan={plan} today={today} needsReview={requiresRefresh || plan.input_version !== data.input_version} unavailable={unavailable} onChanged={() => void refresh()} />)}</div>}
    </div>
  </div>
}

function InterpretationPanel({ report, disabled }: { report: HoldingAnalysisReport; disabled: boolean }) {
  const interpretation = useMutation({ mutationFn: async () => {
    const result = await api.holdingAnalysisInterpret(report.id)
    if (result.report_id !== report.id) throw new Error('解读结果与当前报告不一致，请重新解读')
    return result
  } })
  const result = !disabled && interpretation.data?.report_id === report.id ? interpretation.data : null

  return <div className="space-y-3 border-y border-border py-3">
    <div className="flex flex-wrap items-center gap-3"><button className={buttonClass} disabled={disabled || interpretation.isPending} onClick={() => interpretation.mutate()}>{interpretation.isPending ? '正在解读…' : interpretation.data ? '重新解读依据' : 'AI 解读依据'}</button><p className="max-w-2xl text-xs leading-relaxed text-secondary">调用已配置模型，仅解释程序证据与计划，不改变价格和条件。</p></div>
    {interpretation.isError && <Notice error>模型解读未完成：{interpretation.error.message}。程序分析与计划仍可使用。</Notice>}
    {result && <section aria-label="AI 依据解读" className="min-w-0 space-y-3 break-words text-xs leading-relaxed">
      <div><h4 className="font-medium text-foreground">AI 依据解读</h4><p className="mt-1 text-secondary">生成于 {formatTime(result.generated_at)} · 仅本次会话展示</p></div>
      <p className="whitespace-pre-wrap break-words text-foreground">{result.interpretation.summary}</p>
      {result.interpretation.observations.map((observation, index) => {
        const row = report.rows.find(item => item.key === observation.key)
        if (!row) return null
        const references = [...new Set(observation.evidence_indices)].filter(value => Number.isInteger(value) && value >= 0 && value < row.evidence.length)
        if (!references.length) return null
        return <div key={`${observation.key}:${index}`} className="space-y-2 border-t border-border/60 pt-3"><h5 className="font-medium text-foreground">{row.name || row.symbol}</h5><p className="whitespace-pre-wrap break-words text-foreground">{observation.text}</p><p className="text-secondary">引用的程序证据：</p><ul className="list-disc space-y-1 pl-4 text-secondary">{references.map(reference => <li key={reference}>{row.evidence[reference]}</li>)}</ul></div>
      })}
      {!!result.interpretation.limitations.length && <ul className="list-disc space-y-1 border-t border-border/60 pt-3 pl-4 text-secondary">{result.interpretation.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
    </section>}
  </div>
}

function AnalysisRow({ row, expired, adoptDisabled, onAdopt, onEdit }: { row: HoldingAnalysisRow; expired: boolean; adoptDisabled: boolean; onAdopt: () => void; onEdit: () => void }) {
  return <article className="min-w-0 space-y-3 p-3 sm:p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0"><h4 className="break-words text-sm font-semibold text-foreground">{row.name || row.symbol} <span className="font-mono text-xs font-normal text-secondary">{row.symbol}</span></h4><div className="mt-2 flex flex-wrap gap-1.5">{[ageLabels[row.age_state], pnlLabels[row.pnl_state], trendLabels[row.trend], strengthLabels[row.relative_strength]].map(label => <span key={label} className="rounded border border-border bg-elevated px-2 py-0.5 text-xs text-foreground">{label}</span>)}</div></div>
      <div className="text-xs text-secondary"><span className="block">参考成本价差</span><span className="mt-1 block font-mono text-sm text-foreground">{percent(row.pnl_ratio)}</span></div>
    </div>
    <p className="text-xs leading-relaxed text-foreground">{row.plan.summary}</p>
    <p className="text-xs leading-relaxed text-secondary">{row.age_days == null ? '持仓交易日数未知' : `持仓 ${row.age_days} 个交易日`} · {row.constraints.sellable_qty == null ? '可卖数量未知' : `登记可卖 ${row.constraints.sellable_qty} 股`}{row.constraints.active_t_plan && ' · 有未完成做 T 计划'}。本页不计算可执行股数。</p>
    {!!row.warnings.length && <Notice>{row.warnings.join('；')}</Notice>}
    {expired && <Notice>研究计划已过期，请重新生成分析后采纳。</Notice>}
    {!row.plan.can_adopt && <p className="text-xs leading-relaxed text-secondary">暂不能采纳。请核对上方数据缺口，并完善建仓日、参考成本与研究失效条件后重新分析。</p>}
    <div className="flex flex-wrap gap-2"><button className={primaryClass} disabled={adoptDisabled} onClick={onAdopt}>采纳研究计划</button><button className={buttonClass} onClick={onEdit}>完善研究设置</button></div>
    <details className="group text-xs"><summary className="cursor-pointer rounded py-1 text-accent underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">查看行情、板块与三种情景</summary><div className="mt-4 space-y-5"><AnalysisEvidence row={row} /><Scenarios row={row} /></div></details>
  </article>
}

function AnalysisEvidence({ row }: { row: HoldingAnalysisRow }) {
  const sourceName = row.price.source === 'quote' ? '行情快照' : row.price.source === 'daily' ? '日线参考' : '来源未知'
  return <>
    <div><h5 className="font-medium text-foreground">本次研究设置</h5><div className="mt-2 space-y-2 leading-relaxed text-secondary"><p>操作周期 {row.profile.horizon === 'short' ? '短线' : '波段'} · 预计最长 {row.profile.max_holding_days} 个交易日 · 最大亏损阈值 {row.profile.max_loss_pct == null ? '未设定' : `${row.profile.max_loss_pct}%`}</p><p className="whitespace-pre-wrap break-words">买入理由：{row.profile.thesis || '未填写'}</p><p className="whitespace-pre-wrap break-words">研究失效条件：{row.profile.invalidation || '未填写'}</p></div></div>
    <div><h5 className="font-medium text-foreground">价格与技术依据</h5><p className="mt-2 leading-relaxed text-secondary">参考价 {price(row.price.value)} · 行情日期 {row.price.date ?? '未知'} · {sourceName}{!row.price.fresh && '（非新鲜盘中报价）'} · 市场时间 {formatTime(row.price.market_at)}</p><dl className="mt-2 grid grid-cols-2 gap-x-5 gap-y-2 sm:grid-cols-4">{[['支撑', row.technicals.support], ['压力', row.technicals.resistance], ['20日均线', row.technicals.ma20], ['60日均线', row.technicals.ma60]].map(([label, value]) => <div key={String(label)} className="flex justify-between gap-2 border-b border-border/60 py-2"><dt className="text-secondary">{label}</dt><dd className="font-mono text-foreground">{price(value as number | null)}</dd></div>)}</dl></div>
    <div><h5 className="font-medium text-foreground">关联板块与覆盖</h5>{!row.sectors.length ? <p className="mt-2 text-secondary">板块关联未知，无法判断相对强弱。</p> : <ul className="mt-2 divide-y divide-border/60">{row.sectors.map(sector => <li key={sector.key} className="space-y-1 py-2"><div className="flex flex-wrap justify-between gap-2"><span className="text-foreground">{sector.name || sector.key}</span><span className="font-mono text-foreground">{percent(sector.change_ratio)}</span></div><p className="break-words leading-relaxed text-secondary">有效成分 {sector.valid_count} / {sector.member_count} · 覆盖 {Number.isFinite(sector.coverage_ratio) ? `${(sector.coverage_ratio * 100).toFixed(1)}%` : '未知'} · {sector.valid ? '可用于本次比较' : '覆盖不足，不参与强弱判断'} · 来源 {sector.source_id || '未知'}</p></li>)}</ul>}</div>
    {!!row.evidence.length && <div><h5 className="font-medium text-foreground">判断依据</h5><ul className="mt-2 list-disc space-y-1.5 pl-4 leading-relaxed text-secondary">{row.evidence.map((fact, index) => <li key={index}>{fact}</li>)}</ul></div>}
  </>
}

function Scenarios({ row }: { row: HoldingAnalysisRow }) {
  return <div><h5 className="text-xs font-medium text-foreground">条件操作计划</h5><div className="mt-2 divide-y divide-border/60">{row.plan.scenarios.map(scenario => <section key={scenario.key} className="space-y-2 py-3 text-xs"><h6 className="font-medium text-foreground">{scenario.title} · {scenario.action}</h6><ul className="list-disc space-y-1 pl-4 leading-relaxed text-secondary">{scenario.conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul>{scenario.price_trigger && <p className="text-secondary">价格参考：{scenario.price_trigger.op === '>=' ? '达到或高于' : '达到或低于'} <span className="font-mono text-foreground">{price(scenario.price_trigger.value)}</span></p>}<p className="leading-relaxed text-secondary">情景失效：{scenario.invalidation || '需补充'}</p></section>)}</div><p className="mt-2 text-xs text-secondary">研究计划有效至 {row.plan.expires_on ?? '未知'}；价格触及后仍需复核完整条件。</p></div>
}

function ProfileEditor({ rowKey, source, accountId, profile, version, onSaved }: { rowKey: string; source: HoldingAnalysisSource; accountId: string; profile: HoldingAnalysisProfile; version: number; onSaved: () => void }) {
  const [draft, setDraft] = useState(profile)
  const [maxLoss, setMaxLoss] = useState(profile.max_loss_pct == null ? '' : String(profile.max_loss_pct))
  const [error, setError] = useState('')
  const save = useMutation({ mutationFn: (value: HoldingAnalysisProfile) => api.holdingAnalysisProfileSave(rowKey, { source, account_id: accountId, expected_version: version, profile: value }), onSuccess: onSaved })
  const set = <K extends keyof HoldingAnalysisProfile>(key: K, value: HoldingAnalysisProfile[K]) => setDraft(previous => ({ ...previous, [key]: value }))
  return <form className="mt-4 space-y-4 border-t border-border pt-4" onSubmit={event => {
    event.preventDefault(); setError('')
    const loss = maxLoss.trim() ? Number(maxLoss) : null
    if ((loss != null && (!Number.isFinite(loss) || loss <= 0 || loss >= 100)) || !Number.isFinite(draft.deep_loss_pct) || draft.deep_loss_pct <= 0 || draft.deep_loss_pct >= 100) { setError('亏损比例请填写大于 0 且小于 100 的百分数。'); return }
    if (![draft.new_position_days, draft.max_holding_days].every(value => Number.isInteger(value) && value >= 1)) { setError('交易日天数请填写正整数。'); return }
    save.mutate({ ...draft, max_loss_pct: loss })
  }}>
    <p className="text-xs leading-relaxed text-secondary">研究设置用于解释持仓与生成条件计划。亏损阈值采用百分数，例如 15 表示 15%；未知风险预算可留空。</p>
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <Field label="实际建仓日"><input aria-label="实际建仓日" className={inputClass} type="date" value={draft.opened_on ?? ''} max={beijingDate()} onChange={event => set('opened_on', event.target.value || null)} /></Field>
      <Field label="操作周期"><select aria-label="操作周期" className={inputClass} value={draft.horizon} onChange={event => { const horizon = event.target.value as 'short' | 'swing'; setDraft(previous => ({ ...previous, horizon, max_holding_days: horizon === 'short' ? 5 : 20 })) }}><option value="short">短线</option><option value="swing">波段</option></select></Field>
      <Field label="最大亏损阈值（%，可留空）"><input aria-label="最大亏损阈值" className={inputClass} type="number" min="0.01" max="99.99" step="0.01" value={maxLoss} onChange={event => setMaxLoss(event.target.value)} /></Field>
      <Field label="深度浮亏阈值（%）"><input aria-label="深度浮亏阈值" className={inputClass} required type="number" min="0.01" max="99.99" step="0.01" value={draft.deep_loss_pct} onChange={event => set('deep_loss_pct', Number(event.target.value))} /></Field>
      <Field label="新建仓窗口（交易日）"><input aria-label="新建仓窗口" className={inputClass} required type="number" min="1" step="1" value={draft.new_position_days} onChange={event => set('new_position_days', Number(event.target.value))} /></Field>
      <Field label="预计最长持有（交易日）"><input aria-label="预计最长持有" className={inputClass} required type="number" min="1" step="1" value={draft.max_holding_days} onChange={event => set('max_holding_days', Number(event.target.value))} /></Field>
    </div>
    <div className="grid gap-3 sm:grid-cols-2"><Field label="买入理由"><textarea aria-label="买入理由" className={cn(inputClass, 'h-20 resize-y py-2')} maxLength={2000} value={draft.thesis} onChange={event => set('thesis', event.target.value)} /></Field><Field label="研究失效条件" hint="采纳前需补充，例如买入逻辑不再成立的具体条件。"><textarea aria-label="研究失效条件" className={cn(inputClass, 'h-20 resize-y py-2')} maxLength={2000} value={draft.invalidation} onChange={event => set('invalidation', event.target.value)} /></Field></div>
    <Field label="主要驱动板块标识（可选）" hint="填写已核验的板块标识；系统仍显示其他关联板块和数据覆盖。"><input aria-label="主要驱动板块标识" className={inputClass} value={draft.primary_sector_id} maxLength={200} onChange={event => set('primary_sector_id', event.target.value)} /></Field>
    {(error || save.isError) && <Notice error>{error || save.error?.message}</Notice>}
    <button className={primaryClass} disabled={save.isPending}>{save.isPending ? '正在保存…' : '保存研究设置'}</button>
  </form>
}

function SavedPlan({ plan, today, needsReview, unavailable, onChanged }: { plan: HoldingAnalysisSavedPlan; today: string; needsReview: boolean; unavailable: boolean; onChanged: () => void }) {
  const [message, setMessage] = useState('')
  const expired = !!plan.expires_on && plan.expires_on < today
  const reviewRequired = needsReview || plan.needs_review
  const canAlert = plan.status === 'adopted' && !reviewRequired && !expired && !unavailable
  const ruleIds = [...new Set(plan.alert_rule_ids)]
  const activeCount = ruleIds.filter(id => plan.alert_bindings?.[id] === 'active').length
  const hasPending = ruleIds.some(id => plan.alert_bindings?.[id] === 'pending')
  const hasUnverified = ruleIds.some(id => !plan.alert_bindings?.[id])
  const change = useMutation({ mutationFn: () => api.holdingAnalysisInvalidate(plan.id, { expected_version: plan.version, status: 'invalidated' }), onSuccess: () => { setMessage('研究计划已作废。'); onChanged() } })
  const alert = useMutation({ mutationFn: (scenario_key: 'bull' | 'bear') => api.holdingAnalysisAlert(plan.id, { expected_version: plan.version, scenario_key }), onSuccess: result => { const status = result.plan.alert_bindings?.[result.rule_id]; setMessage(status === 'active' ? '价格提醒已确认绑定，可在监控中心查看。' : status === 'pending' ? '有提醒待重新绑定，请重新读取计划后重试。' : '提醒状态待核验，请重新读取计划后重新绑定。'); onChanged() } })
  return <article className="min-w-0 space-y-3 p-3 sm:p-4">
    <div className="flex flex-wrap items-start justify-between gap-2"><h4 className="break-words text-sm font-medium text-foreground">{plan.row.name || plan.row.symbol} <span className="font-mono text-xs text-secondary">{plan.row.symbol}</span></h4><span className="rounded border border-border px-2 py-0.5 text-xs text-secondary">{plan.status === 'invalidated' ? '已作废' : expired ? '已过期' : reviewRequired ? '输入改变，待复核' : '已采纳'}</span></div>
    <p className="text-xs leading-relaxed text-foreground">{plan.row.plan.summary}</p>
    <p className="text-xs text-secondary">采纳于 {formatTime(plan.created_at)} · 有效至 {plan.expires_on ?? '未知'} · 已绑定 {activeCount} 条价格提醒</p>
    {hasPending && <Notice>有提醒待重新绑定。请核对当前数据后，重新绑定对应的价格提醒。</Notice>}
    {hasUnverified && <Notice>有提醒状态待核验。旧记录不视为已启用，请重新读取计划并重新绑定。</Notice>}
    <details className="text-xs"><summary className="cursor-pointer rounded py-1 text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">查看采纳时的依据与情景</summary><div className="mt-3 space-y-5"><AnalysisEvidence row={plan.row} /><Scenarios row={plan.row} /></div></details>
    <div className="flex flex-wrap gap-2">{(['bull', 'bear'] as const).map(key => <button key={key} className={buttonClass} disabled={!canAlert || alert.isPending || change.isPending || !plan.row.plan.scenarios.find(scenario => scenario.key === key)?.price_trigger} onClick={() => alert.mutate(key)}><Bell size={14} />{key === 'bull' ? '绑定转强价格提醒' : '绑定转弱价格提醒'}</button>)}<button className={buttonClass} disabled={plan.status !== 'adopted' || change.isPending || alert.isPending || unavailable} onClick={() => change.mutate()}>作废研究计划</button></div>
    {!canAlert && plan.status !== 'invalidated' && <p className="text-xs text-secondary">{expired ? '计划已过期，请重新分析并采纳。' : '数据或输入需复核，请重新读取并生成分析。'}</p>}
    {message && <Notice>{message}</Notice>}
    {(change.isError || alert.isError) && <Notice error>{change.error?.message || alert.error?.message}。请重新读取计划后重试。</Notice>}
  </article>
}
