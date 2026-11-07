import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeftRight, Check, Plus, RefreshCw, Settings2, X } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, type PositionTAccount, type PositionTConfig, type PositionTOverview, type PositionTPlan, type PositionTPosition, type PositionTSignal, type PositionTReview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Modal } from '@/components/Modal'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { PositionTAccountForm, PositionTConfigForm, PositionTExecutionForm, PositionTPositionForm } from './PositionTForms'
import { PositionTDailyReview } from './PositionTDailyReview'
import { beijingDate, buttonClass, directionLabel, Empty, Field, formatTime, inputClass, money, newKey, Notice, planDone, priceRange, primaryClass, signalCanPlan, snapshotValidity, Status, usePositionTClock } from './positionTShared'

type WorkspaceTab = 'monitor' | 'plans' | 'review' | 'config'
type Dialog = { kind: 'create-account' | 'snapshot' | 'config' } | { kind: 'position'; position?: PositionTPosition } | { kind: 'plan'; plan: PositionTPlan } | null
const numeric = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v : undefined
const displayValue = (v: unknown): string => typeof v === 'number' ? Number.isInteger(v) ? v.toLocaleString('zh-CN') : v.toFixed(4) : typeof v === 'boolean' ? v ? '是' : '否' : typeof v === 'string' ? v : '未提供'
const evidenceLabels: Record<string, string> = { market_return_5m: '大盘5分钟涨跌幅变化（小数）', sector_return_5m: '板块5分钟涨跌幅变化（小数）', last_complete_minute: '最新完整分钟', vwap_change_5m: '均价 5 分钟变化', vwap: '成交量加权均价', atr: '20 分钟平均波幅', band: '波动区间宽度', support: '已形成支撑', resistance: '已形成压力', volume_ratio: '成交量确认倍数', displacement: '10 分钟方向位移', market_return: '大盘5分钟涨跌幅变化（小数）', sector_return: '板块5分钟涨跌幅变化（小数）', close: '最新完整分钟收盘', trend: '趋势状态', strong_down: '强下跌', strong_up: '强上涨', confirmation: '价量确认', recovery: '止跌确认', rejection: '回落确认', atr20: '20 分钟平均波幅', volume_ratio20: '成交量确认倍数' }

export function PositionTMonitor() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const linkedAccount = params.get('account_id') ?? ''
  const linkedPlan = params.get('plan_id') ?? ''
  const linkedSymbol = params.get('symbol') ?? ''
  const handledLink = useRef('')
  const now = usePositionTClock()
  const [accountId, setAccountId] = useState(linkedAccount)
  const [tab, setTab] = useState<WorkspaceTab>('monitor')
  const [dialog, setDialog] = useState<Dialog>(null)
  const [symbol, setSymbol] = useState('')
  const overview = useQuery({ queryKey: QK.positionTOverview(accountId), queryFn: () => api.positionTOverview(accountId), refetchInterval: 5000, refetchIntervalInBackground: false })
  const data = overview.data
  const account = data?.account
  useEffect(() => {
    if (linkedAccount) { setAccountId(linkedAccount); setDialog(null) }
    setSymbol(linkedSymbol)
    if (linkedPlan) setTab('plans')
  }, [linkedAccount, linkedPlan, linkedSymbol])
  useEffect(() => {
    const key = `${linkedAccount}:${linkedPlan}`
    if (!linkedAccount || !linkedPlan || data?.account?.id !== linkedAccount || handledLink.current === key) return
    const plan = data.plans.find(p => p.id === linkedPlan && p.account_id === linkedAccount)
    if (plan) { setDialog({ kind: 'plan', plan }); handledLink.current = key }
  }, [linkedAccount, linkedPlan, data])
  useEffect(() => { if (!accountId && account?.id) setAccountId(account.id) }, [accountId, account?.id])
  const refresh = () => Promise.all([
    qc.invalidateQueries({ queryKey: QK.positionTAll }),
    qc.invalidateQueries({ queryKey: QK.holdingAnalysisAll }),
  ])
  const saved = () => { setDialog(null); void refresh() }
  const changeAccount = (id: string) => { setAccountId(id); setSymbol(''); setDialog(null); handledLink.current = ''; setParams({}, { replace: true }) }
  const unfinished = (data?.plans ?? []).filter(p => !planDone(p.state))
  const selectedPosition = data?.positions.find(p => p.symbol === symbol)
  const signals = (data?.signals ?? []).filter(s => !symbol || s.symbol === symbol)
  const chartSignal = signals.find(s => s.first_price && s.second_price)
  const lastRun = Date.parse(data?.health.last_run ?? '')
  const serviceAge = Date.now() - lastRun
  const serviceFresh = !overview.isError && Number.isFinite(lastRun) && serviceAge >= 0 && serviceAge <= 15000
  const closePlan = () => { setDialog(null); handledLink.current = ''; const next = new URLSearchParams(params); next.delete('plan_id'); setParams(next, { replace: true }) }

  return <div className="mx-auto w-full max-w-7xl space-y-4">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2"><label className="sr-only" htmlFor="position-t-account">做 T 账户</label><select id="position-t-account" value={accountId} onChange={e => changeAccount(e.target.value)} className={cn(inputClass, 'w-52')} disabled={!data?.accounts.length}><option value="">选择账户</option>{data?.accounts.map(a => <option key={a.id} value={a.id}>{a.name} · {a.mode === 'simulation' ? '模拟' : '人工登记'}</option>)}</select><button className={buttonClass} onClick={() => setDialog({ kind: 'create-account' })}><Plus size={14} />添加账户</button></div>
      <button onClick={() => overview.refetch()} disabled={overview.isFetching} className={buttonClass}><RefreshCw size={14} />{overview.isFetching ? '更新中…' : '刷新'}</button>
    </div>
    {overview.isError && <Notice error>监控数据读取失败：{overview.error.message}。当前展示的数据不可用于新计划，请刷新后重新核对。</Notice>}
    {overview.isLoading && <div role="status" aria-label="正在读取监控账户" className="space-y-3 py-3"><div className="h-12 rounded-lg bg-elevated" /><div className="h-44 rounded-lg bg-elevated" /><span className="text-xs text-secondary">正在读取监控账户…</span></div>}
    {!overview.isLoading && !account && !overview.isError && <Empty title="从核对真实持仓开始"><p>添加人工或模拟账户，分别录入资金、总持仓和当日可卖额度。旧批次提醒中的参考数量不会自动作为可卖库存。</p><button className={cn(primaryClass, 'mt-4')} onClick={() => setDialog({ kind: 'create-account' })}>添加第一个账户</button></Empty>}
    {account && data && <>
      <section aria-label="账户与服务状态" className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium text-foreground">{account.name}</span><span className="rounded bg-elevated px-2 py-1 text-xs text-foreground">{account.mode === 'simulation' ? '模拟账户' : '人工登记 · 券商可执行性待核对'}</span>{(!account.reconciled || account.needs_reconciliation) && <Status state="paused" />}</div><button className={buttonClass} onClick={() => setDialog({ kind: 'snapshot' })}><Check size={14} />核对资金</button></div>
        <dl className="mt-3 flex flex-wrap gap-x-8 gap-y-2 text-xs"><div><dt className="inline text-secondary">可用资金 </dt><dd className="inline font-mono text-foreground">{money(account.available_cash)}</dd></div><div><dt className="inline text-secondary">平台预留 </dt><dd className="inline font-mono text-foreground">{money(account.reserved_cash ?? 0)}</dd></div><div><dt className="inline text-secondary">剩余可规划资金 </dt><dd className="inline font-mono text-foreground">{money(account.effective_cash)}</dd></div><div><dt className="inline text-secondary">资金核对 </dt><dd className="inline text-foreground">{formatTime(account.snapshot_at)} · 资金{snapshotValidity(account.snapshot_at, data.config?.position_max_age_seconds, now)}</dd></div></dl>
        <p className="mt-3 text-xs leading-relaxed text-secondary">{data.health.running && serviceFresh ? '后台服务运行中' : '后台检查已延迟，请刷新核对'} · 最近检查 {formatTime(data.health.last_run)}。页面关闭后由后台服务继续监控；服务停止时监控停止。</p>
        {!!data.health.reasons?.length && <p className="mt-2 text-xs leading-relaxed text-secondary">{data.health.reasons.join('；')}</p>}
        {account.cash_estimated && <p role="alert" className="mt-3 text-xs font-medium leading-relaxed text-foreground">账户资金含未核对费用：请先更正成交费用并核对资金，当前现金不能作为已核对结果。</p>}
        {account.mode === 'simulation' && <AutomaticSimulation key={account.id} account={account} config={data.config} simulation={data.simulation} unavailable={overview.isError} onSaved={refresh} onConfigure={() => setTab('config')} />}
      </section>
      <nav className="flex overflow-x-auto border-b border-border" aria-label="持仓做 T 工作区">{([['monitor', '盘中做 T'], ['plans', `未完成计划${unfinished.length ? ` (${unfinished.length})` : ''}`], ['review', '盘后复盘'], ['config', '策略与费用']] as const).map(([key, label]) => <button key={key} aria-current={tab === key ? 'page' : undefined} className={cn('shrink-0 border-b-2 px-4 py-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent', tab === key ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground')} onClick={() => setTab(key)}>{label}</button>)}</nav>
      {tab === 'monitor' && <>
        {unfinished.length > 0 && <Notice><div className="flex flex-wrap items-center justify-between gap-2"><span>有 {unfinished.length} 个未完成计划。先跟踪第二笔；同一股票不会重复占用资源。</span><button className="font-medium text-accent underline underline-offset-4" onClick={() => setTab('plans')}>查看待处理计划</button></div></Notice>}
        <PositionTHoldingsToolbar key={account.id} account={account} count={data.positions.length} unavailable={overview.isError} onAdd={() => setDialog({ kind: 'position' })} />
        {!data.positions.length ? <Empty title="尚未添加持仓">录入总持仓、当日可卖、核心下限与日终目标后开始观察。可卖未知时留空，系统会暂停执行判断。</Empty> : <div className="overflow-x-auto rounded-lg border border-border"><table className="w-full min-w-[780px] text-left text-xs"><thead className="border-b border-border bg-elevated/50 text-secondary"><tr>{['股票', '总持仓', '可卖 / 已预留', '核心下限 / 目标', '持仓核对', '结果', '操作'].map(h => <th key={h} className="px-3 py-2.5 font-medium">{h}</th>)}</tr></thead><tbody>{data.positions.map(p => { const directions = data.signals.filter(s => s.symbol === p.symbol); return <tr key={p.symbol} className={cn('border-b border-border last:border-0', symbol === p.symbol ? 'bg-accent/5' : 'hover:bg-elevated/40')}><td className="px-3 py-3"><button onClick={() => setSymbol(symbol === p.symbol ? '' : p.symbol)} className="text-left text-foreground hover:text-accent"><span className="block font-medium">{p.name || p.symbol}</span><span className="font-mono text-secondary">{p.symbol}</span></button>{p.source === "lots" && <span className="mt-1 block text-secondary">批次提醒同步{!p.reconciled && " · 待核对"}</span>}</td><td className="px-3 py-3 font-mono">{p.qty}</td><td className="px-3 py-3 font-mono">{p.sellable_qty == null ? '未知' : p.effective_sellable ?? p.sellable_qty} / {p.reserved_qty ?? 0}</td><td className="px-3 py-3 font-mono">{p.core_qty} / {p.target_qty}</td><td className="px-3 py-3 text-secondary">{formatTime(p.snapshot_at)}<span className="block">持仓{snapshotValidity(p.snapshot_at, data.config?.position_max_age_seconds, now)}</span>{!p.reconciled && <span className="block">待核对</span>}</td><td className="px-3 py-3"><div className="space-y-1">{(['buy_sell', 'sell_buy'] as const).map(direction => { const candidate = directions.find(s => s.direction === direction); const state = candidate?.state === 'triggered' && !signalCanPlan(candidate.state, candidate.expires_at, candidate.quantity, now) ? 'expired' : candidate?.state ?? 'paused'; return <div key={direction} className="flex flex-wrap items-center gap-1.5"><span className="text-secondary">{directionLabel(direction)}</span><Status state={state} /></div> })}</div></td><td className="px-3 py-3"><button className={buttonClass} onClick={() => setDialog({ kind: 'position', position: p })}>核对</button></td></tr> })}</tbody></table></div>}
        <div className="flex flex-wrap items-center justify-between gap-2 pt-2"><h2 className="text-sm font-semibold text-foreground">{symbol ? `${selectedPosition?.name || symbol} · 机会判断` : '机会判断'}</h2>{symbol && <button className={buttonClass} onClick={() => setSymbol('')}>显示全部持仓</button>}</div>
        <p className="text-xs leading-relaxed text-secondary">{account.mode === 'simulation' && data.config?.auto_simulation_enabled ? '自动模拟已启用，候选由后台按约束建立模拟计划，手工建立入口已暂停。' : '候选只测算；建立计划时才预留资金与旧库存。'}允许全天无机会，条件匹配不代表盈利概率。</p>
        {!signals.length ? <Empty title="等待后台评估">服务会在交易日和配置时段内，取得完整分钟数据后更新。缺少报价、板块环境或可卖数据时将明确暂停。</Empty> : <div className="space-y-3">{signals.map(signal => <Opportunity key={`${signal.account_id}:${signal.symbol}:${signal.direction}`} signal={signal} position={data.positions.find(p => p.symbol === signal.symbol)} unavailable={overview.isError || (account.mode === 'simulation' && data.config?.auto_simulation_enabled === true)} onCreated={() => { void refresh(); setTab('plans') }} />)}</div>}
        {selectedPosition && <section className="rounded-lg border border-border bg-surface p-3"><h3 className="mb-2 text-sm font-medium text-foreground">{selectedPosition.name || selectedPosition.symbol} · 分时与参考价位</h3><p className="mb-2 text-xs text-secondary">图表仅辅助观察，触及参考价不代表成交。</p><StockIntradayChart key={`${account.id}:${symbol}`} symbol={symbol} date={account.trade_date} height={320} refetchIntervalMs={30000} priceLines={[...(chartSignal?.first_price ?? []).map((value, index) => ({ value, label: `第一笔${index ? '上界' : '下界'}`, color: '#3b82f6' })), ...(chartSignal?.second_price ?? []).map((value, index) => ({ value, label: `第二笔${index ? '上界' : '下界'}`, color: '#888888' }))]} /></section>}
        <p className="text-xs text-secondary">数据能力缺失时，请在 <Link className="text-accent underline underline-offset-4" to="/settings">数据源设置</Link> 核对实时、分钟和报价能力，并在策略配置中确认分钟时间戳含义。</p>
      </>}
      {tab === 'plans' && <><div className="flex items-center gap-2 text-xs text-secondary"><ArrowLeftRight size={15} />第二笔按第一笔实际成交量跟踪；信号过期不会抹去成交。</div>{!unfinished.length ? <Empty title="没有未完成计划">{account.mode === 'simulation' ? '自动模拟启用后在此跟踪模拟计划与撮合结果；未启用时可手工登记模拟成交。' : '建立计划后将在此跟踪两笔交易。成交须手工登记。'}关注和价格触及都不会直接写入成交。</Empty> : <div className="space-y-3">{unfinished.map(plan => <PlanRow key={plan.id} plan={plan} now={now} onOpen={() => setDialog({ kind: 'plan', plan })} />)}</div>}</>}
      {tab === 'review' && <PositionTReviewPanel key={account.id} account={account} positions={data.positions} />}
      {tab === 'config' && <div className="space-y-4"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold text-foreground">策略与账户约束</h2><button disabled={!data.config} className={primaryClass} onClick={() => setDialog({ kind: 'config' })}><Settings2 size={14} />编辑配置</button></div><Notice>首版使用分钟级 VWAP 回落恢复／上冲回落规则。资金、库存、费用、市场环境和数据质量均为硬过滤。AI 不参与是否触发，也不会代替实际成交。</Notice>{data.config && <dl className="grid gap-x-8 gap-y-0 rounded-lg border border-border px-4 sm:grid-cols-2">{[['账户监控', data.config.enabled ? '已启用' : '未启用'], ['费用口径', data.config.fees_confirmed ? '用户已核对' : '待核对'], ['分钟时间戳', ({ unknown: '尚未确认', start: '分钟开始时间', end: '分钟结束时间' } as const)[data.config.minute_timestamp_semantics ?? 'unknown']], ['最低净价差', `${money(data.config.min_net_profit)} 且 ${(data.config.min_net_return * 100).toFixed(2)}%`], ['单次／每日风险预算', `${money(data.config.single_risk_budget)} / ${money(data.config.daily_risk_budget)}`], ['账户／每股每日次数', `${data.config.max_daily_plans} / ${data.config.max_symbol_daily_plans}`], ['新计划截止', data.config.new_plan_cutoff], ['报价／持仓有效期', `${data.config.quote_max_age_seconds} 秒 / ${data.config.position_max_age_seconds} 秒`], ['第二笔时间阈值', `${data.config.max_leg_minutes} 个交易分钟`], ['费用生效日', data.config.fee_effective_from]].map(([label, value]) => <div key={label} className="flex flex-wrap justify-between gap-2 border-b border-border/60 py-3 text-xs"><dt className="text-secondary">{label}</dt><dd className="text-foreground">{value}</dd></div>)}</dl>}<p className="max-w-3xl text-xs leading-relaxed text-secondary">参数为待验证初值，不承诺收益、完成率或恢复股数。风险阈值触发也不能保证成交。先买后卖只能用预留的旧可卖库存卖出；先卖后买的买回不会恢复当日已消耗可卖。</p></div>}
    </>}
    {dialog?.kind === 'create-account' && <PositionTAccountForm onClose={() => setDialog(null)} onSaved={id => { changeAccount(id); void refresh() }} />}
    {account && dialog?.kind === 'snapshot' && <PositionTAccountForm key={account.id} account={account} onClose={() => setDialog(null)} onSaved={() => saved()} />}
    {account && dialog?.kind === 'position' && <PositionTPositionForm key={`${account.id}:${dialog.position?.symbol ?? 'new'}`} account={account} position={dialog.position} onClose={() => setDialog(null)} onSaved={saved} />}
    {account && data?.config && dialog?.kind === 'config' && <PositionTConfigForm account={account} config={data.config} onClose={() => setDialog(null)} onSaved={saved} />}
    {account && dialog?.kind === 'plan' && <PositionTPlanDetail id={dialog.plan.id} account={account} onClose={closePlan} onSaved={() => void refresh()} />}
  </div>
}

function AutomaticSimulation({ account, config, simulation, unavailable, onSaved, onConfigure }: { account: PositionTAccount; config: PositionTConfig | null; simulation: PositionTOverview['simulation']; unavailable: boolean; onSaved: () => Promise<unknown>; onConfigure: () => void }) {
  const enabled = config?.auto_simulation_enabled === true
  const save = useMutation({ mutationFn: () => api.positionTConfig(account.id, { ...config!, auto_simulation_enabled: !enabled, expected_version: account.version }), onSuccess: () => onSaved() })
  const labels: Record<string, string> = { disabled: '自动模拟已关闭', waiting: '等待可执行机会', running: '自动模拟运行中', paused: '自动模拟已暂停', idle: '休市，等待下一交易时段' }
  const state = !enabled ? labels.disabled : !simulation || simulation.state === 'disabled' ? '等待后台确认' : labels[simulation.state] ?? '等待后台确认'
  return <div className="mt-3 border-t border-border pt-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-sm font-medium text-foreground">{state}</span><button className={enabled ? buttonClass : primaryClass} disabled={!config || (!enabled && unavailable) || save.isPending} onClick={() => save.mutate()}>{save.isPending ? '正在保存…' : enabled ? '停止自动模拟' : '启用自动模拟'}</button></div>
    <p className="mt-2 text-xs leading-relaxed text-secondary">按完整分钟保守撮合并记录模拟成交，非真实交易。停止后取消未成交模拟委托，已成交及未完成敞口保留。{simulation?.last_run && ` 最近处理 ${formatTime(simulation.last_run)}。`}</p>
    {enabled && !config?.enabled && <p className="mt-2 text-xs text-secondary">账户机会监控尚未启用。<button className="ml-1 text-accent underline underline-offset-4" onClick={onConfigure}>查看策略与费用</button></p>}
    {enabled && !!simulation?.reasons.length && <p className="mt-2 text-xs leading-relaxed text-secondary">{simulation.reasons.join('；')}</p>}
    {save.isError && <div className="mt-2"><Notice error>{save.error.message}</Notice></div>}
  </div>
}

function PositionTHoldingsToolbar({ account, count, unavailable, onAdd }: { account: PositionTAccount; count: number; unavailable: boolean; onAdd: () => void }) {
  const qc = useQueryClient()
  const sync = useMutation({
    mutationFn: () => api.positionTSyncLots(account.id, { expected_version: account.version }),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: QK.positionTAll }),
      qc.invalidateQueries({ queryKey: QK.holdingAnalysisAll }),
    ]),
  })
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-sm font-semibold text-foreground">持仓监控 <span className="font-normal text-secondary">{count} 只</span></h2>
      <div className="flex flex-wrap gap-2">
        <button className={count ? buttonClass : primaryClass} disabled={unavailable || sync.isPending} onClick={() => sync.mutate()}><RefreshCw size={14} />{sync.isPending ? '正在同步…' : '从批次提醒同步'}</button>
        <button className={count ? primaryClass : buttonClass} onClick={onAdd}><Plus size={14} />添加／核对持仓</button>
      </div>
    </div>
    <p className="text-xs leading-relaxed text-secondary">可从批次提醒同步股票、合计股数与加权参考成本。同步后需核对可卖数量和做 T 限额，再启用监控；已有持仓保留。</p>
    {sync.isPending && <p role="status" className="text-xs text-secondary">正在读取批次提醒并同步到 {account.name}…</p>}
    {sync.isError && <Notice error>同步失败：{sync.error.message}</Notice>}
    {sync.isSuccess && <Notice>
      <p>{sync.data.created.length ? `已同步 ${sync.data.created.length} 只持仓，尚未启用监控。请逐只核对可卖数量、持仓边界和做 T 限额。` : sync.data.skipped.length ? '本次未新增持仓。' : '批次提醒中没有可同步的持仓。'}</p>
      {sync.data.created.length > 0 && <ul className="mt-2 space-y-1">{sync.data.created.map(p => <li key={p.symbol}>{p.name || p.symbol} · {p.qty} 股</li>)}</ul>}
      {sync.data.skipped.length > 0 && <details className="mt-2"><summary className="cursor-pointer">已跳过 {sync.data.skipped.length} 只，查看原因</summary><ul className="mt-2 space-y-1">{sync.data.skipped.map((p, index) => <li key={`${p.symbol}:${index}`}>{p.symbol}：{p.reason}</li>)}</ul></details>}
    </Notice>}
  </div>
}

export function Opportunity({ signal, position, unavailable, onCreated }: { signal: PositionTSignal; position?: PositionTPosition; unavailable: boolean; onCreated: () => void }) {
  const [quantity, setQuantity] = useState(String(signal.quantity))
  const [key, setKey] = useState(newKey)
  useEffect(() => { setKey(newKey()); setQuantity(current => Number(current) > 0 && Number(current) <= signal.quantity ? current : String(signal.quantity)) }, [signal.id, signal.quantity])
  const [expired, setExpired] = useState(!signalCanPlan(signal.state, signal.expires_at, signal.quantity))
  useEffect(() => { const update = () => setExpired(!signalCanPlan(signal.state, signal.expires_at, signal.quantity)); update(); const timer = window.setInterval(update, 1000); return () => clearInterval(timer) }, [signal.state, signal.expires_at, signal.quantity])
  const create = useMutation({ mutationFn: () => {
    const q = Number(quantity)
    if (!Number.isInteger(q) || q <= 0 || q > signal.quantity) throw new Error('请输入不超过可规划上限的正整数股数')
    return api.positionTCreatePlan({ signal_id: signal.id, quantity: q, expected_account_version: signal.account_version, expected_position_version: signal.position_version, idempotency_key: key })
  }, onSuccess: onCreated })
  const state = signal.state === 'triggered' && expired ? 'expired' : signal.state
  const evidence = Object.entries(signal.evidence).filter(([key, value]) => evidenceLabels[key] && ['number', 'string', 'boolean'].includes(typeof value))
  const rawQuoteAge = numeric(signal.market.quote_age_seconds)
  const conditions = signal.evidence.conditions && typeof signal.evidence.conditions === 'object' ? Object.entries(signal.evidence.conditions as Record<string, unknown>).filter(([, value]) => typeof value === 'boolean') : []
  return <article className="rounded-lg border border-border bg-surface p-4">
    <div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="text-sm font-semibold text-foreground">{signal.source_mode === 'simulation' && '模拟 · '}{position?.name || signal.symbol} <span className="ml-2 font-normal text-secondary">{directionLabel(signal.direction)}</span></h3><p className="mt-1 text-xs text-secondary">{signal.symbol} · 判断 {formatTime(signal.created_at)} · {String(signal.market.source ?? signal.market.data_source ?? '来源待核对')}{rawQuoteAge == null ? ' · 报价时效待核对' : ` · 报价 ${rawQuoteAge.toFixed(0)} 秒前`}</p></div><Status state={state} /></div>
    {!!signal.reasons.length && <ul className="mt-3 space-y-1 text-xs leading-relaxed text-secondary">{signal.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>}
    <dl className="mt-4 grid gap-4 border-y border-border py-3 sm:grid-cols-3"><div><dt className="text-xs text-secondary">第一笔 · {signal.direction === 'buy_sell' ? '买入' : '卖出'}</dt><dd className="mt-1 font-mono text-sm text-foreground">{priceRange(signal.first_price)}</dd></div><div><dt className="text-xs text-secondary">第二笔 · {signal.direction === 'buy_sell' ? '卖出旧库存' : '买回'}</dt><dd className="mt-1 font-mono text-sm text-foreground">{priceRange(signal.second_price)}</dd></div><div><dt className="text-xs text-secondary">条件净价差 · {signal.quantity} 股</dt><dd className="mt-1 font-mono text-sm text-foreground">{signal.net_min == null ? '数据不足' : `${money(signal.net_min)} — ${money(signal.net_max)}`}</dd></div></dl>
    {!!evidence.length && <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs">{evidence.map(([key, value]) => <div key={key}><dt className="inline text-secondary">{evidenceLabels[key]} </dt><dd className="inline font-mono text-foreground">{displayValue(value)}</dd></div>)}</dl>}
    {!!conditions.length && <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-secondary">{conditions.map(([label, passed]) => <li key={label}>{passed ? '已满足' : '待满足'} · {label}</li>)}</ul>}
    <p className="mt-3 text-xs leading-relaxed text-secondary">失效参考价：{signal.stop_price == null ? '未形成' : signal.stop_price.toFixed(2)}。{signal.cover_guard != null && `压力回补上界：${signal.cover_guard.toFixed(2)}。`}数据过期、账户变化或触发条件消失时重新校验。第一笔成交后若第二笔未完成，相对计划基准将{signal.direction === 'buy_sell' ? '多持有' : '少持有'}尚未匹配的股数，可能隔夜。</p>
    <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><p className="text-xs text-secondary">有效至 {formatTime(signal.expires_at)} · 策略 {signal.strategy_version}<br />价差为条件情景测算，盈利概率未知。</p>{state === 'triggered' && <div className="flex flex-wrap items-center gap-2"><label className="text-xs text-secondary">计划股数<input aria-label={`${signal.symbol} ${directionLabel(signal.direction)}计划股数`} type="number" min="1" max={signal.quantity} step="1" className={cn(inputClass, 'ml-2 w-24')} value={quantity} onChange={e => setQuantity(e.target.value)} /></label><button disabled={unavailable || expired || create.isPending} onClick={() => create.mutate()} className={primaryClass}>{create.isPending ? '正在重新校验…' : '建立计划并预留'}</button></div>}</div>
    <details className="mt-3 text-xs text-secondary"><summary className="cursor-pointer">数据时间与费用明细</summary><dl className="mt-2 grid gap-2 sm:grid-cols-2"><div><dt className="inline">行情时间 </dt><dd className="inline">{formatTime(typeof signal.market.market_ts === 'string' ? signal.market.market_ts : null)}</dd></div><div><dt className="inline">接收时间 </dt><dd className="inline">{formatTime(typeof signal.market.received_ts === 'string' ? signal.market.received_ts : null)}</dd></div><div><dt className="inline">计算时间 </dt><dd className="inline">{formatTime(typeof signal.market.calculated_at === 'string' ? signal.market.calculated_at : signal.created_at)}</dd></div>{([['buy_fee', '买入费用'], ['sell_fee', '卖出费用'], ['slippage', '另计执行偏移'], ['gross', '费用前价差']] as const).map(([key, label]) => <div key={key}><dt className="inline">{label} </dt><dd className="inline font-mono">{money(numeric(signal.fees[key]))}</dd></div>)}</dl></details>
    {create.isError && <div className="mt-3"><Notice error>{create.error.message}</Notice></div>}
  </article>
}

export function PlanRow({ plan, onOpen, now = Date.now() }: { plan: PositionTPlan; onOpen: () => void; now?: number }) {
  const monitor = plan.monitor
  const expires = Date.parse(monitor?.expires_at ?? '')
  const computed = Date.parse(monitor?.computed_at ?? '')
  const fresh = Number.isFinite(expires) && Number.isFinite(computed) && computed <= now && expires > now
  const ready = !planDone(plan.state) && fresh && monitor?.state === 'ready' && plan.unmatched_qty > 0
  const monitorLabel = planDone(plan.state) ? '计划已结束' : !fresh ? '跟踪数据已过期，请刷新核对' : ready ? '第二笔条件满足' : monitor?.state === 'risk' ? '第二笔风险待处理' : monitor?.state === 'paused' ? '第二笔判断暂停' : monitor?.state === 'waiting_first' ? '等待第一笔实际成交' : '等待第二笔条件'
  return <article className="rounded-lg border border-border bg-surface p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-medium text-foreground">{plan.symbol} · {directionLabel(plan.direction)}{plan.execution_mode === 'automatic_simulation' && <span className="ml-2 text-xs font-normal text-secondary">自动模拟</span>}</h3><Status state={plan.state} /></div>
    <dl className="mt-3 flex flex-wrap gap-x-8 gap-y-2 text-xs">
      <div><dt className="inline text-secondary">第一笔已成交 </dt><dd className="inline font-mono">{plan.leg1_filled} 股</dd></div>
      <div><dt className="inline text-secondary">已匹配 </dt><dd className="inline font-mono">{plan.matched_qty} 股</dd></div>
      <div><dt className="inline text-secondary">第二笔待处理 </dt><dd className="inline font-mono font-semibold">{plan.unmatched_qty} 股</dd></div>
      <div><dt className="inline text-secondary">匹配净收益 </dt><dd className="inline font-mono">{money(plan.matched_pnl)}</dd></div>
    </dl>
    {!planDone(plan.state) && <div className="mt-3 border-t border-border pt-3">
      <p className={cn('text-sm font-medium', ready ? 'text-accent' : 'text-foreground')}>{monitorLabel}</p>
      {ready && <p className="mt-1 text-xs leading-relaxed text-foreground">{plan.direction === 'buy_sell' ? '卖出旧库存' : '买回'} {plan.unmatched_qty} 股 · {plan.direction === 'buy_sell' ? '买一' : '卖一'}参考 {numeric(monitor?.actionable_price)?.toFixed(2) ?? '待核对'} · 按当前报价测算净收益 {money(monitor?.net_profit)}。{plan.execution_mode === 'automatic_simulation' ? '等待后续完整分钟保守撮合，非真实交易。' : '请核对委托，价格触及不代表成交。'}</p>}
      {fresh && monitor?.second_leg_condition && ['waiting', 'waiting_first'].includes(monitor.state ?? '') && <p className="mt-1 text-xs text-secondary">{monitor.second_leg_condition}</p>}
      <p className="mt-1 text-xs text-secondary">最近判断 {formatTime(monitor?.computed_at)}{fresh && ` · 有效至 ${formatTime(monitor?.expires_at)}`}</p>
      {!!monitor?.reasons?.length && <p className="mt-1 text-xs leading-relaxed text-secondary">{monitor.reasons.join('；')}</p>}
    </div>}
    {plan.unmatched_qty > 0 && <p className="mt-3 text-xs leading-relaxed text-foreground">当前相对计划基准{plan.direction === 'buy_sell' ? '多出' : '缺少'} {plan.unmatched_qty} 股。第二笔未完成，持仓变化继续保留。</p>}
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-secondary">资金占用 {money(plan.reserved_cash)} · 库存占用 {plan.reserved_qty} 股</p><button className={primaryClass} onClick={onOpen}>跟踪／登记</button></div>
    {!!plan.reasons.length && <p className="mt-2 text-xs text-secondary">{plan.reasons.join('；')}</p>}
    {plan.matched_pnl == null && <p className="mt-2 text-xs font-medium text-foreground">费用未核对，匹配净收益暂不计算；实际成交与未完成敞口继续保留。</p>}
  </article>
}

function PositionTPlanDetail({ id, account, onClose, onSaved }: { id: string; account: PositionTAccount; onClose: () => void; onSaved: () => void }) {
  const qc = useQueryClient()
  const now = usePositionTClock()
  const [entry, setEntry] = useState<'execution' | 'order' | null>(null)
  const [feeEdit, setFeeEdit] = useState<{ id: string; value: string; reason: string; key: string } | null>(null)
  const query = useQuery({ queryKey: QK.positionTPlan(id), queryFn: () => api.positionTPlan(id), refetchInterval: 5000 })
  const plan = query.data?.plan
  const stop = useMutation({ mutationFn: () => api.positionTStopFirstLeg(id, plan!.version), onSuccess: () => { void qc.invalidateQueries({ queryKey: QK.positionTPlan(id) }); onSaved() } })
  const saved = () => { setEntry(null); void qc.invalidateQueries({ queryKey: QK.positionTPlan(id) }); onSaved() }
  const reviseFee = useMutation({ mutationFn: () => {
    if (!feeEdit?.value.trim() || !Number.isFinite(Number(feeEdit.value)) || Number(feeEdit.value) < 0) throw new Error('请输入非负的实际费用')
    if (!feeEdit.reason.trim()) throw new Error('请填写费用更正原因')
    return api.positionTExecutionFee(feeEdit.id, { fee: Number(feeEdit.value), reason: feeEdit.reason.trim(), idempotency_key: feeEdit.key })
  }, onSuccess: () => { setFeeEdit(null); saved() } })
  if (entry && plan) return <PositionTExecutionForm plan={plan} mode={account.mode} kind={entry} onClose={() => setEntry(null)} onSaved={saved} />
  return <Modal onClose={onClose} ariaLabel="两笔计划跟踪" panelClassName="flex max-h-[90dvh] w-[94vw] max-w-3xl flex-col rounded-xl border border-border bg-surface"><header className="flex items-center justify-between border-b border-border p-4"><h2 className="text-base font-semibold text-foreground">{account.mode === 'simulation' ? '模拟 · ' : ''}两笔计划跟踪</h2><button aria-label="关闭计划详情" className={buttonClass} onClick={onClose}><X size={16} /></button></header><div className="space-y-4 overflow-y-auto p-4">
    {query.isLoading && <p role="status" className="text-sm text-secondary">正在读取成交与计划…</p>}{query.isError && <Notice error>{query.error.message}</Notice>}
    {plan && <><PlanRow plan={plan} now={now} onOpen={() => setEntry('execution')} /><dl className="grid gap-3 sm:grid-cols-2"><div><dt className="text-xs text-secondary">第一笔参考区间</dt><dd className="font-mono text-sm">{priceRange(plan.first_price)}</dd></div><div><dt className="text-xs text-secondary">第二笔参考区间</dt><dd className="font-mono text-sm">{priceRange(plan.second_price)}</dd></div></dl><Notice>{account.mode === 'simulation' ? '模拟成交与委托分别记录，非真实交易。停止第一笔后仍需核对未决委托；已成交股数继续保留，手工登记会影响模拟样本。' : '登记委托和登记成交分别处理。停止第一笔仅停止继续规划，不替你撤销券商委托；撤单待确认时资源继续占用。已成交股数不会被取消。'}</Notice><div className="flex flex-wrap gap-2"><button className={primaryClass} onClick={() => setEntry('execution')}>{account.mode === 'simulation' ? '登记模拟成交' : '登记实际成交'}</button><button className={buttonClass} onClick={() => setEntry('order')}>登记委托状态</button><button disabled={planDone(plan.state) || stop.isPending} className={buttonClass} onClick={() => stop.mutate()}>{stop.isPending ? '正在处理…' : '停止继续第一笔'}</button></div>{stop.isError && <Notice error>{stop.error.message}</Notice>}
    <section><h3 className="mb-2 text-sm font-medium">{account.mode === 'simulation' ? '模拟成交记录' : '实际成交记录'}</h3>{!query.data?.executions.length ? <p className="text-xs text-secondary">{account.mode === 'simulation' ? '尚无模拟成交。' : '尚无实际成交。'}提醒和价格触及不等于成交。</p> : <div className="overflow-x-auto"><table className="w-full min-w-[530px] text-left text-xs"><thead className="text-secondary"><tr>{['北京时间', '步骤', '股数', '均价', '费用', '操作'].map(x => <th key={x} className="py-2 font-normal">{x}</th>)}</tr></thead><tbody>{query.data.executions.map((execution, i) => <tr key={execution.id ?? i} className="border-t border-border"><td className="py-2">{formatTime(execution.executed_at)}</td><td>第 {execution.leg} 笔</td><td className="font-mono">{execution.quantity}</td><td className="font-mono">{execution.price.toFixed(2)}</td><td>{execution.fee_unverified ? '费用待核对（合同不适用）' : execution.fee == null ? `暂估 ${money(execution.allocated_fee)}` : money(execution.fee)}</td><td>{execution.id && <button className={buttonClass} onClick={() => { reviseFee.reset(); setFeeEdit({ id: execution.id!, value: String(execution.fee ?? execution.allocated_fee ?? ''), reason: '', key: newKey() }) }}>更正费用</button>}</td></tr>)}</tbody></table></div>}</section>
    {feeEdit && <form className="space-y-3 rounded-lg border border-border p-3" onSubmit={e => { e.preventDefault(); reviseFee.mutate() }}><p className="text-xs text-secondary">仅更正此笔成交费用并保留审计记录。数量或价格错误请暂停新计划、核对账户，不删除成交事实。</p><div className="grid gap-3 sm:grid-cols-2"><Field label="此笔实际费用（元）"><input required type="number" min="0" step="0.01" value={feeEdit.value} onChange={e => setFeeEdit({ ...feeEdit, value: e.target.value })} className={inputClass} /></Field><Field label="更正原因"><input required value={feeEdit.reason} onChange={e => setFeeEdit({ ...feeEdit, reason: e.target.value })} className={inputClass} /></Field></div>{reviseFee.isError && <Notice error>{reviseFee.error.message}</Notice>}<div className="flex gap-2"><button disabled={reviseFee.isPending} className={primaryClass}>保存费用更正</button><button type="button" disabled={reviseFee.isPending} className={buttonClass} onClick={() => setFeeEdit(null)}>取消更正</button></div></form>}
    <details className="text-xs"><summary className="cursor-pointer text-secondary">委托状态记录（{query.data?.events.length ?? 0}）</summary><ul className="mt-2 space-y-2">{query.data?.events.map((event, i) => <li key={event.event_id ?? i} className="flex flex-wrap items-center gap-2 border-t border-border py-2"><span>{formatTime(event.occurred_at)}</span><span>第 {event.leg} 笔</span><Status state={event.status} /><span>累计成交 {event.cumulative_filled} / 委托 {event.quantity} 股；已撤 {event.cancelled_quantity} 股</span></li>)}</ul></details><p className="text-xs text-secondary">策略 {plan.strategy_version} · 更新 {formatTime(plan.updated_at)}</p></>}
  </div></Modal>
}

function ReviewNumbers({ review }: { review: PositionTReview }) {
  return <><dl className="grid gap-x-6 rounded-lg border border-border px-4 sm:grid-cols-2 lg:grid-cols-3">{[['已匹配做 T 净收益', money(review.matched_pnl)], ['未匹配股数', review.unmatched_qty == null ? '待核对' : `${review.unmatched_qty} 股`], ['未匹配敞口价值', money(review.exposure_value)], ['整体账户权益', money(review.total_equity)], ['持续持有对照权益', money(review.hold_equity)], ['净增量权益', money(review.incremental_equity)], ['全部费用', money(review.fees)], ['计划完成率', review.completion_rate == null ? '样本不足' : `${(review.completion_rate * 100).toFixed(1)}%`], ['计划样本数', String(review.plan_count ?? '待核对')]].map(([label, value]) => <div key={label} className="border-b border-border/60 py-3 text-xs"><dt className="text-secondary">{label}</dt><dd className="mt-1 font-mono text-sm text-foreground">{value}</dd></div>)}</dl>{!!review.reasons?.length && <Notice>{review.reasons.join('；')}</Notice>}{!!review.limitations?.length && <Notice>{review.limitations.join('；')}</Notice>}{review.valuation_scope && <p className="text-xs text-secondary">{review.valuation_scope}</p>}{review.benchmark_valid === false && <Notice>持续持有基准尚未核对，当前无法据此判断净增量收益。</Notice>}</>
}

function PositionTReviewPanel({ account, positions }: { account: PositionTAccount; positions: PositionTPosition[] }) {
  const [date, setDate] = useState(beijingDate())
  const [symbol, setSymbol] = useState(positions[0]?.symbol ?? '')
  const [replayDate, setReplayDate] = useState('')
  const review = useQuery({ queryKey: QK.positionTDailyReview(account.id, date), queryFn: () => api.positionTDailyReview(account.id, date), enabled: !!date, staleTime: 30000, refetchInterval: query => query.state.data?.report.status === 'final' ? false : 30000, refetchIntervalInBackground: false })
  const replay = useMutation({ mutationFn: () => api.positionTReplay({ account_id: account.id, symbol, start_date: replayDate, end_date: replayDate }) })
  return <div className="space-y-4"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">当日做 T 复盘</h2><div className="flex flex-wrap items-center gap-2"><label className="flex items-center gap-2 text-xs text-secondary">交易日<input type="date" value={date} max={beijingDate()} onChange={e => setDate(e.target.value)} className={cn(inputClass, 'w-40')} /></label><button disabled={!date || review.isFetching} className={buttonClass} onClick={() => review.refetch()}>{review.isFetching ? '读取中…' : '更新复盘'}</button></div></div>{!date && <Notice>请选择需要复盘的交易日。</Notice>}{review.isLoading && <p role="status" className="text-xs text-secondary">正在读取复盘…</p>}{review.isError && <Notice error>复盘读取失败：{review.error.message}。请更新后再核对结果。</Notice>}{review.data && !review.isError && <PositionTDailyReview report={review.data.report} />}
    <details className="rounded-lg border border-border p-4"><summary className="cursor-pointer text-sm font-medium">单日保守模拟</summary><form className="mt-4 space-y-3" onSubmit={e => { e.preventDefault(); replay.mutate() }}><p className="text-xs leading-relaxed text-secondary">以首个已核对快照为起点的单日保守模拟，非真实账户历史收益。仅在当时存档的分钟数据、交易规则和市场环境完整时运行；下一分钟才能模拟执行，同一分钟不会按有利顺序完成两笔。</p><div className="grid gap-3 sm:grid-cols-2"><Field label="持仓股票"><select required value={symbol} onChange={e => setSymbol(e.target.value)} className={inputClass}><option value="">请选择</option>{positions.map(p => <option key={p.symbol} value={p.symbol}>{p.name || p.symbol} · {p.symbol}</option>)}</select></Field><Field label="模拟交易日"><input required type="date" value={replayDate} onChange={e => setReplayDate(e.target.value)} className={inputClass} /></Field></div><button disabled={replay.isPending || !positions.length} className={primaryClass}>{replay.isPending ? '正在保守回放…' : '运行单日模拟'}</button></form>{replay.isError && <div className="mt-3"><Notice error>{replay.error.message}</Notice></div>}{replay.data && <div className="mt-4 space-y-3"><h3 className="text-sm font-medium">模拟结果 · 非实盘成交</h3><ReviewNumbers review={replay.data.replay} /><dl className="flex flex-wrap gap-x-8 gap-y-2 text-xs"><div><dt className="text-secondary">收盘不利变化</dt><dd className="font-mono">{money(replay.data.replay.max_adverse_excursion_close)}</dd></div><div><dt className="text-secondary">分钟内风险上界</dt><dd className="font-mono">{money(replay.data.replay.max_adverse_excursion_intraminute_bound)}</dd></div></dl><p className="text-xs leading-relaxed text-secondary">收盘口径可能遗漏分钟内波动；风险上界按分钟内最不利价格测算，不能视为已发生亏损。</p><Notice>{replay.data.replay.assumptions?.join('；') || '策略参数尚未完成样本外验证，不承诺收益。'}</Notice></div>}</details>

  </div>
}
