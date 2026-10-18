import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type PositionTAccount, type PositionTConfig, type PositionTPosition, type PositionTPlan, type PositionTOrderEvent } from '@/lib/api'
import { Modal } from '@/components/Modal'
import { QK } from '@/lib/queryKeys'
import { beijingDate, beijingInputTime, buttonClass, directionLabel, Field, inputClass, newKey, Notice, numberValue, primaryClass } from './positionTShared'

function FormShell({ title, children, pending, error, onClose, onSubmit, submit = '保存核对结果' }: { title: string; children: ReactNode; pending: boolean; error: string; onClose: () => void; onSubmit: (event: FormEvent) => void; submit?: string }) {
  return <Modal onClose={() => !pending && onClose()} ariaLabel={title} closeOnBackdrop={!pending} panelClassName="flex max-h-[90dvh] w-[94vw] max-w-2xl flex-col rounded-xl border border-border bg-surface">
    <form className="flex min-h-0 flex-col" onSubmit={onSubmit}><header className="border-b border-border px-5 py-4"><h2 className="text-base font-semibold text-foreground">{title}</h2></header><div className="space-y-4 overflow-y-auto px-5 py-4">{children}{error && <Notice error>{error}</Notice>}</div><footer className="flex justify-end gap-2 border-t border-border px-5 py-3"><button type="button" onClick={onClose} disabled={pending} className={buttonClass}>取消</button><button disabled={pending} className={primaryClass}>{pending ? '正在保存…' : submit}</button></footer></form>
  </Modal>
}
const errorText = (error: unknown) => error instanceof Error ? error.message : '保存失败，请核对后重试'

export function PositionTAccountForm({ account, onClose, onSaved }: { account?: PositionTAccount; onClose: () => void; onSaved: (id: string) => void }) {
  const [name, setName] = useState(account?.name ?? '')
  const [mode, setMode] = useState<'manual' | 'simulation'>(account?.mode ?? 'manual')
  const [cash, setCash] = useState(account?.available_cash == null ? '' : String(account.available_cash))
  const [frozen, setFrozen] = useState(String(account?.broker_frozen_cash ?? 0))
  const [net, setNet] = useState(account?.cash_net_of_frozen ?? true)
  const [date, setDate] = useState(beijingDate())
  const [checked, setChecked] = useState(false)
  const [error, setError] = useState('')
  const save = useMutation({ mutationFn: async () => {
    const available_cash = numberValue(cash, '可用资金', { optional: true })
    if (!date) throw new Error('请选择核对交易日')
    if (account) {
      if (!checked) throw new Error('请核对资金与全部已登记成交后再保存')
      return api.positionTSnapshot(account.id, { available_cash, broker_frozen_cash: numberValue(frozen, '冻结资金')!, cash_net_of_frozen: net, trade_date: date, snapshot_at: new Date().toISOString(), reconciled: checked, included_event_seq: account.event_seq, expected_version: account.version })
    }
    if (!name.trim()) throw new Error('请填写账户名称')
    return api.positionTCreateAccount({ name: name.trim(), mode, available_cash, trade_date: date })
  }, onSuccess: result => onSaved(result.account.id), onError: e => setError(errorText(e)) })
  return <FormShell title={account ? '核对账户资金' : '添加做 T 账户'} pending={save.isPending} error={error} onClose={onClose} onSubmit={e => { e.preventDefault(); setError(''); save.mutate() }} submit={account ? '保存核对结果' : '创建账户'}>
    {!account && <div className="grid gap-4 sm:grid-cols-2"><Field label="账户名称"><input required value={name} onChange={e => setName(e.target.value)} className={inputClass} placeholder="如：我的现金账户" /></Field><Field label="账户类型"><select value={mode} onChange={e => setMode(e.target.value as typeof mode)} className={inputClass}><option value="manual">人工登记</option><option value="simulation">模拟账户</option></select></Field></div>}
    <Notice>{mode === 'manual' ? '人工登记仅按你核对的数据计算，券商可执行性待核对。请录入扣除券商冻结后的可用资金；平台预留会单独显示。' : '模拟账户与真实账户隔离；模拟记录不代表券商成交。'}</Notice>
    <div className="grid gap-4 sm:grid-cols-2"><Field label="可用资金（元，可留空）" hint="留空表示未知，不能用于验证新计划。"><input type="number" min="0" step="0.01" value={cash} onChange={e => setCash(e.target.value)} className={inputClass} /></Field><Field label="核对交易日（北京时间）"><input type="date" required value={date} onChange={e => setDate(e.target.value)} className={inputClass} /></Field></div>
    {account && <><Field label="券商冻结资金（元）"><input type="number" min="0" step="0.01" required value={frozen} onChange={e => setFrozen(e.target.value)} className={inputClass} /></Field><label className="flex items-start gap-2 text-xs text-secondary"><input type="checkbox" checked={net} onChange={e => setNet(e.target.checked)} className="mt-0.5" />上方可用资金已经扣除券商冻结（避免重复扣减）</label><label className="flex items-start gap-2 text-xs text-foreground"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} className="mt-0.5" />已核对所有已登记成交及平台外交易；此快照包含当前已登记的账户变动</label><p className="text-xs leading-relaxed text-secondary">有未决委托或未完成计划时，后台可能拒绝覆盖，以免错误释放占用。</p></>}
  </FormShell>
}

export function PositionTPositionForm({ account, position, onClose, onSaved }: { account: PositionTAccount; position?: PositionTPosition; onClose: () => void; onSaved: () => void }) {
  const [symbol, setSymbol] = useState(position?.symbol ?? '')
  const [name, setName] = useState(position?.name ?? '')
  const [exchange, setExchange] = useState<PositionTPosition['exchange']>(position?.exchange ?? 'SH')
  const [board, setBoard] = useState<PositionTPosition['board']>(position?.board ?? 'main')
  const [date, setDate] = useState(beijingDate())
  const [costMethod, setCostMethod] = useState(position?.cost_method ?? 'manual_reference')
  const [benchmark, setBenchmark] = useState(position?.benchmark_symbol ?? '000001.SH')
  const [sector, setSector] = useState(position?.sector_id ?? '')
  const sectors = useQuery({ queryKey: QK.monitorRuleOptions, queryFn: api.monitorRuleOptions, staleTime: 300_000 })
  const sectorTargets = Object.values(sectors.data?.sector_targets ?? {}).flat()
  const [enabled, setEnabled] = useState(position?.enabled ?? true)
  const [net, setNet] = useState(position?.sellable_net_of_frozen ?? true)
  const [checked, setChecked] = useState(false)
  const [error, setError] = useState('')
  const numericFields = [
    ['qty', '总持仓（股）'], ['sellable_qty', '当日可卖（股，未知留空）'], ['today_bought', '当日新增（股）'], ['today_sold', '当日已卖（股）'],
    ['broker_frozen_qty', '券商冻结（股）'], ['external_sell_qty', '平台外有效卖单（股）'], ['core_qty', '核心持仓下限（股）'], ['target_qty', '日终目标持仓（股）'],
    ['max_t_qty', '单次做 T 上限（股）'], ['max_extra_qty', '临时增仓上限（股）'], ['cost_price', '持仓成本（元，可留空）'],
  ] as const
  const [nums, setNums] = useState<Record<string, string>>(() => Object.fromEntries(numericFields.map(([key]) => [key, position?.[key] == null ? (key === 'sellable_qty' || key === 'cost_price' ? '' : '0') : String(position[key])])) )
  const save = useMutation({ mutationFn: async () => {
    if (!checked) throw new Error('请核对持仓及可卖来源后勾选确认')
    const cleanSymbol = symbol.trim().toUpperCase()
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(cleanSymbol)) throw new Error('请填写完整 A 股代码，如 600000.SH')
    if (!cleanSymbol.endsWith(`.${exchange}`)) throw new Error('代码后缀与所选交易所不一致')
    const vals = Object.fromEntries(numericFields.map(([key, label]) => [key, numberValue(nums[key], label, { optional: ['sellable_qty', 'cost_price'].includes(key), integer: key !== 'cost_price' })]))
    // 已有持仓核对必须允许照录平台外交易后的真实数量，不能靠降低核心下限掩盖越界。
    if (!position && Number(vals.core_qty) > Number(vals.qty)) throw new Error('新增持仓的核心持仓下限不能超过当前总持仓')
    return api.positionTSavePosition(account.id, cleanSymbol, {
      symbol: cleanSymbol, name: name.trim(), exchange, board, security_type: 'stock',
      qty: vals.qty!, sellable_qty: vals.sellable_qty, today_bought: vals.today_bought!, today_sold: vals.today_sold!, broker_frozen_qty: vals.broker_frozen_qty!, external_sell_qty: vals.external_sell_qty!,
      core_qty: vals.core_qty!, target_qty: vals.target_qty!, max_t_qty: vals.max_t_qty!, max_extra_qty: vals.max_extra_qty!, cost_price: vals.cost_price,
      cost_method: costMethod, enabled, sellable_net_of_frozen: net, trade_date: date, snapshot_at: new Date().toISOString(), reconciled: checked,
      benchmark_symbol: benchmark, sector_id: sector.trim(), included_event_seq: account.event_seq, expected_version: position?.version,
    })
  }, onSuccess: onSaved, onError: e => setError(errorText(e)) })
  return <FormShell title={position ? `核对 ${position.name || position.symbol} 持仓` : '添加持仓监控'} pending={save.isPending} error={error} onClose={onClose} onSubmit={e => { e.preventDefault(); setError(''); save.mutate() }}>
    <Notice>当日可卖必须来自账户核对，不能从总持仓推算。当天买入、卖出后买回的股份都不会增加当天可卖额度。</Notice>
    {position && nums.qty.trim() && nums.core_qty.trim() && Number(nums.qty) < Number(nums.core_qty) && <Notice error>实际持仓已低于核心持仓下限。请如实登记，不必降低原核心下限；保存后账户将标为待核对，暂停新计划。</Notice>}
    <div className="grid gap-3 sm:grid-cols-2"><Field label="证券代码"><input required disabled={!!position} value={symbol} onChange={e => setSymbol(e.target.value)} placeholder="600000.SH" className={inputClass} /></Field><Field label="证券名称"><input value={name} onChange={e => setName(e.target.value)} className={inputClass} /></Field><Field label="交易所"><select value={exchange} onChange={e => setExchange(e.target.value as typeof exchange)} className={inputClass}><option value="SH">上海证券交易所</option><option value="SZ">深圳证券交易所</option><option value="BJ">北京证券交易所</option></select></Field><Field label="板块"><select value={board} onChange={e => setBoard(e.target.value as typeof board)} className={inputClass}><option value="main">主板</option><option value="chinext">创业板</option><option value="star">科创板</option><option value="bse">北交所</option></select></Field>
      {numericFields.map(([key, label]) => <Field key={key} label={label}><input type="number" min="0" step={key === 'cost_price' ? '0.001' : '1'} required={!['sellable_qty', 'cost_price'].includes(key)} value={nums[key]} onChange={e => setNums(s => ({ ...s, [key]: e.target.value }))} className={inputClass} /></Field>)}
      <Field label="成本口径"><select value={costMethod} onChange={e => setCostMethod(e.target.value)} className={inputClass}><option value="manual_reference">人工参考成本</option><option value="broker_reported">券商显示平均成本</option><option value="weighted_average">成交加权平均成本</option></select></Field><Field label="核对交易日"><input type="date" required value={date} onChange={e => setDate(e.target.value)} className={inputClass} /></Field><Field label="大盘参考"><select value={benchmark} onChange={e => setBenchmark(e.target.value)} className={inputClass}><option value="000001.SH">上证指数</option><option value="399001.SZ">深证成指</option><option value="399006.SZ">创业板指</option><option value="000680.SH">科创综指</option></select></Field><Field label="板块（可选择或填写已核验标识）" hint={sectors.isError ? '板块目录加载失败，可填写已核验的板块标识。' : '未接通对应板块行情时暂停判断。'}><input list="position-t-sectors" value={sector} onChange={e => setSector(e.target.value)} className={inputClass} /><datalist id="position-t-sectors">{sectorTargets.map(target => <option key={target.key} value={target.key}>{target.name}</option>)}</datalist></Field></div>
    <label className="flex gap-2 text-xs text-secondary"><input type="checkbox" checked={net} onChange={e => setNet(e.target.checked)} />当日可卖已经扣除券商冻结</label><label className="flex gap-2 text-xs text-secondary"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />纳入持仓机会监控</label><label className="flex items-start gap-2 text-xs text-foreground"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} className="mt-0.5" />已核对当前持仓、可卖和平台外委托，快照包含当前所有已登记变动；未知可卖已留空</label>
  </FormShell>
}

export function PositionTConfigForm({ account, config, onClose, onSaved }: { account: PositionTAccount; config: PositionTConfig; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState(config)
  const [error, setError] = useState('')
  const fields: [keyof PositionTConfig, string][] = [['min_net_profit', '最低净价差（元）'], ['min_net_return', '最低净价差比例（0.002=0.2%）'], ['max_daily_plans', '账户每日计划上限'], ['max_symbol_daily_plans', '每股每日计划上限'], ['daily_quantity_ratio', '日累计数量比例（0.4=40%）'], ['cooldown_minutes', '冷却（交易分钟）'], ['single_risk_budget', '单计划风险预算（元）'], ['daily_risk_budget', '每日风险预算（元）'], ['max_extra_notional', '最大临时增仓金额（元）'], ['max_leg_minutes', '第二笔时间阈值（交易分钟）'], ['quote_max_age_seconds', '报价最大年龄（秒）'], ['position_max_age_seconds', '人工核对有效期（秒）'], ['commission_rate', '佣金比例（0.0003=万三）'], ['min_commission', '单委托最低佣金（元）'], ['stamp_tax_rate', '卖出印花税比例'], ['transfer_rate', '双边过户费比例'], ['slippage_per_share', '未计入参考价的每股执行偏移（元）']]
  const [nums, setNums] = useState(() => Object.fromEntries(fields.map(([k]) => [k, String(config[k])])) )
  const save = useMutation({ mutationFn: () => {
    const parsed = Object.fromEntries(fields.map(([key, label]) => [key, numberValue(nums[key], label)]))
    return api.positionTConfig(account.id, { ...draft, ...parsed, expected_version: account.version })
  }, onSuccess: onSaved, onError: e => setError(errorText(e)) })
  return <FormShell title="策略与费用配置" pending={save.isPending} error={error} onClose={onClose} onSubmit={e => { e.preventDefault(); setError(''); save.mutate() }} submit="保存配置">
    <Notice>这些参数是待验证的研发初值。费用按券商合同填写；佣金已包含的项目请勿再次叠加。参数匹配程度不是盈利概率。</Notice>
    <label className="flex gap-2 text-sm text-foreground"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft(d => ({ ...d, enabled: e.target.checked }))} />启用账户机会监控</label>
    <Field label="分钟行情时间戳含义" hint="请依据当前数据源文档确认。未确认时暂停策略；切换后重新校验。"><select value={draft.minute_timestamp_semantics ?? 'unknown'} onChange={e => setDraft(d => ({ ...d, minute_timestamp_semantics: e.target.value as PositionTConfig['minute_timestamp_semantics'] }))} className={inputClass}><option value="unknown">尚未确认</option><option value="start">表示该分钟开始时间</option><option value="end">表示该分钟结束时间</option></select></Field>
    <div className="grid gap-3 sm:grid-cols-2">{fields.map(([key, label]) => <Field key={key} label={label}><input required type="number" min="0" step="any" value={nums[key]} onChange={e => setNums(d => ({ ...d, [key]: e.target.value }))} className={inputClass} /></Field>)}<Field label="禁止新建计划的截止时点"><input type="time" required value={draft.new_plan_cutoff} onChange={e => setDraft(d => ({ ...d, new_plan_cutoff: e.target.value }))} className={inputClass} /></Field><Field label="费用生效日期"><input type="date" required value={draft.fee_effective_from} onChange={e => setDraft(d => ({ ...d, fee_effective_from: e.target.value }))} className={inputClass} /></Field></div>
    <label className="flex items-start gap-2 text-xs text-foreground"><input type="checkbox" checked={draft.fees_confirmed} onChange={e => setDraft(d => ({ ...d, fees_confirmed: e.target.checked }))} className="mt-0.5" />已按适用收费规则核对以上费用、最低收费和包含项目</label>
    <div className="grid gap-3 sm:grid-cols-2"><Field label="其他适用费用比例"><input type="number" min="0" step="any" value={draft.other_fee_rate ?? 0} onChange={e => setDraft(d => ({ ...d, other_fee_rate: Number(e.target.value) }))} className={inputClass} /></Field><Field label="费用失效日（可留空）"><input type="date" value={draft.fee_effective_to ?? ''} onChange={e => setDraft(d => ({ ...d, fee_effective_to: e.target.value || null }))} className={inputClass} /></Field></div>
    <label className="flex gap-2 text-xs text-secondary"><input type="checkbox" checked={draft.commission_includes_transfer ?? false} onChange={e => setDraft(d => ({ ...d, commission_includes_transfer: e.target.checked }))} />佣金已包含过户费，不再另计</label>
    <label className="flex gap-2 text-xs text-secondary"><input type="checkbox" checked={draft.commission_includes_other ?? true} onChange={e => setDraft(d => ({ ...d, commission_includes_other: e.target.checked }))} />佣金已包含其他适用费用，不再另计</label>
    <label className="flex gap-2 text-xs text-secondary"><input type="checkbox" checked={draft.reference_includes_slippage ?? false} onChange={e => setDraft(d => ({ ...d, reference_includes_slippage: e.target.checked }))} />参考价已经计入执行偏移，不再扣减相同成本</label>
  </FormShell>
}

export function PositionTExecutionForm({ plan, mode, kind, onClose, onSaved }: { plan: PositionTPlan; mode: 'manual' | 'simulation'; kind: 'execution' | 'order'; onClose: () => void; onSaved: () => void }) {
  const [leg, setLeg] = useState<1 | 2>(plan.unmatched_qty > 0 ? 2 : 1)
  const [orderId, setOrderId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [price, setPrice] = useState('')
  const [fee, setFee] = useState('')
  const [when, setWhen] = useState(beijingInputTime())
  const [status, setStatus] = useState<PositionTOrderEvent['status']>('submitted')
  const [cumulative, setCumulative] = useState('0')
  const [cancelled, setCancelled] = useState('0')
  const [confirmed, setConfirmed] = useState(false)
  const [available, setAvailable] = useState(false)
  const [key] = useState(newKey)
  const [error, setError] = useState('')
  const side = (plan.direction === 'buy_sell') === (leg === 1) ? '买入' : '卖出'
  const save = useMutation({ mutationFn: async () => {
    if (!confirmed) throw new Error('请确认记录来自实际委托／成交事实')
    if (!orderId.trim()) throw new Error('请填写委托编号；同一委托的部分成交应使用相同编号')
    if (!when) throw new Error('请填写北京时间')
    const q = numberValue(quantity, kind === 'execution' ? '本次实际成交股数' : '原始委托股数', { integer: true, positive: true })!
    if (kind === 'execution') return api.positionTExecution({ plan_id: plan.id, leg, order_id: orderId.trim(), quantity: q, price: numberValue(price, '实际成交均价', { positive: true })!, executed_at: `${when}+08:00`, fee: numberValue(fee, '费用', { optional: true }), idempotency_key: key, proceeds_available: mode === 'simulation' || available })
    return api.positionTOrderEvent({ plan_id: plan.id, leg, order_id: orderId.trim(), event_id: key, status, quantity: q, cumulative_filled: numberValue(cumulative, '累计成交股数', { integer: true })!, cancelled_quantity: numberValue(cancelled, '已撤股数', { integer: true })!, price: numberValue(price, '委托价', { positive: true })!, occurred_at: `${when}+08:00` })
  }, onSuccess: onSaved, onError: e => setError(errorText(e)) })
  return <FormShell title={`${mode === 'simulation' ? '模拟 · ' : ''}${kind === 'execution' ? '登记实际成交' : '登记委托状态'}`} pending={save.isPending} error={error} onClose={onClose} onSubmit={e => { e.preventDefault(); setError(''); save.mutate() }} submit={kind === 'execution' ? '确认登记成交' : '保存委托状态'}>
    <Notice>{plan.symbol} · {directionLabel(plan.direction)}。{kind === 'execution' ? '仅登记本次真实成交数量，不填写累计成交量。价格触及、已读和委托提交都不是成交。' : '委托状态不会自动产生成交；部分成交还须单独登记。撤单未确认前继续占用资源。'}</Notice>
    <div className="grid gap-3 sm:grid-cols-2"><Field label="交易步骤"><select value={leg} onChange={e => { setLeg(Number(e.target.value) as 1 | 2); setAvailable(false) }} className={inputClass}><option value="1">第一笔</option><option value="2">第二笔</option></select></Field><Field label="委托编号"><input required value={orderId} onChange={e => setOrderId(e.target.value)} className={inputClass} placeholder="券商编号或唯一人工编号" /></Field><Field label={`${kind === 'execution' ? '本次实际成交' : '原始委托'}股数 · ${side}`}><input required type="number" min="1" step="1" value={quantity} onChange={e => setQuantity(e.target.value)} className={inputClass} /></Field><Field label={kind === 'execution' ? '实际成交均价（元）' : '委托价格（元）'}><input required type="number" min="0" step="any" value={price} onChange={e => setPrice(e.target.value)} className={inputClass} /></Field><Field label="发生时间（北京时间）"><input required type="datetime-local" step="1" value={when} onChange={e => setWhen(e.target.value)} className={inputClass} /></Field>
    {kind === 'execution' ? <Field label="本次费用（元，可留空暂估）"><input type="number" min="0" step="0.01" value={fee} onChange={e => setFee(e.target.value)} className={inputClass} /></Field> : <><Field label="委托状态"><select value={status} onChange={e => setStatus(e.target.value as typeof status)} className={inputClass}><option value="submitted">已提交</option><option value="partially_filled">部分成交</option><option value="filled">全部成交</option><option value="cancel_pending">撤单待确认</option><option value="cancelled">撤单已确认</option><option value="rejected">已拒绝</option></select></Field><Field label="累计成交股数"><input required type="number" min="0" step="1" value={cumulative} onChange={e => setCumulative(e.target.value)} className={inputClass} /></Field><Field label="已确认撤销股数"><input required type="number" min="0" step="1" value={cancelled} onChange={e => setCancelled(e.target.value)} className={inputClass} /></Field></>}</div>
    {kind === 'execution' && side === '卖出' && mode === 'manual' && <label className="flex items-start gap-2 text-xs text-foreground"><input type="checkbox" checked={available} onChange={e => setAvailable(e.target.checked)} className="mt-0.5" />卖出所得已在账户中确认可用（未勾选将保留资金待核对状态）</label>}
    <label className="flex items-start gap-2 text-xs text-foreground"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} className="mt-0.5" />{mode === 'simulation' ? '确认将这笔记录写入模拟账本' : '已核对券商记录，此处登记不向券商下单'}</label>
  </FormShell>
}
