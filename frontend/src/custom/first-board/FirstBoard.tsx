import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, RefreshCw } from 'lucide-react'
import { api, type FirstBoardCandidate, type FirstBoardConfig, type FirstBoardEvent, type FirstBoardPattern, type FirstBoardRules } from '@/lib/api'
import type { ColumnConfig } from '@/lib/list-columns'
import { QK } from '@/lib/queryKeys'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { RulesPanel } from './RulesPanel'
import { ResearchPanel } from './ResearchPanel'
import { beijingDay, buttonClass, ErrorNotice, EventBadge, inputClass, LoadingRows, money, Notice, panelClass, PatternBadge, patternLabels, pct, stamp, StateBadge, stateLabels, universeLabels } from './shared'

const tabs = [{ id: 'live', label: '今日盯盘' }, { id: 'events', label: '信号记录' }, { id: 'research', label: '效果验证' }, { id: 'rules', label: '规则版本' }]
const statuses = { disabled: '监控未开启', waiting: '等待数据', ready: '盯盘中', stale: '数据已过期', closed: '非连续竞价时段', error: '暂不可用' }
const candidateColumns: ColumnConfig[] = [
  ['symbol', '股票 / 行业'], ['pattern', '形态'], ['state', '状态'], ['price', '现价'], ['change_pct', '涨幅'],
  ['distance_to_limit_pct', '距涨停'], ['turnover_rate', '换手率'], ['reasons', '入选与阻断原因'], ['quote_time', '报价时间'], ['actions', '模拟操作'],
].map(([id, label]) => ({ id, label, source: { type: 'builtin', key: id }, visible: true }))
const sortableColumns = new Set(['pattern', 'state', 'price', 'change_pct', 'distance_to_limit_pct'])
const categoryOrder = { pattern: Object.keys(patternLabels), state: Object.keys(stateLabels) }
const categorySortHints: Record<string, string> = {
  pattern: `升序：${Object.values(patternLabels).join(' → ')}`,
  state: `升序：${Object.values(stateLabels).join(' → ')}`,
}

function candidateSortValue(row: FirstBoardCandidate, column: ColumnConfig) {
  if (column.id === 'pattern' || column.id === 'state') {
    // 分类沿用界面已有的定义顺序，未知分类与缺失数值一样置底。
    const index = categoryOrder[column.id].indexOf(row[column.id])
    return index < 0 ? null : index
  }
  const value = row[column.id as keyof FirstBoardCandidate]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function FirstBoard() {
  const [params, setParams] = useSearchParams()
  const tab = tabs.some(item => item.id === params.get('tab')) ? params.get('tab')! : 'live'
  const client = useQueryClient()
  const config = useQuery({ queryKey: QK.firstBoardConfig, queryFn: api.firstBoardConfig })
  const snapshot = useQuery({ queryKey: QK.firstBoardSnapshot, queryFn: api.firstBoardSnapshot, enabled: !!config.data, refetchInterval: 5000, retry: 1 })
  const matrix = useCapabilityMatrix()
  const versions = useQuery({ queryKey: QK.firstBoardVersions, queryFn: api.firstBoardVersions, enabled: tab === 'rules' || tab === 'research' })
  const accounts = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts, enabled: tab === 'rules' })
  const [eventDay, setEventDay] = useState(beijingDay)
  const events = useQuery({ queryKey: QK.firstBoardEvents(eventDay), queryFn: () => api.firstBoardEvents(eventDay), enabled: tab === 'events', refetchInterval: eventDay === beijingDay() && config.data?.enabled ? 5000 : false })
  const [draft, setDraft] = useState<FirstBoardConfig | null>(null)
  const [filter, setFilter] = useState<FirstBoardPattern | 'all'>('all')
  const [onlyActionable, setOnlyActionable] = useState(false)
  const { sort, toggle: toggleSort, sortRows } = useTableSort<FirstBoardCandidate>(candidateSortValue)
  const [stock, setStock] = useState<{ symbol: string; name: string } | null>(null)
  const [feedback, setFeedback] = useState('')
  useEffect(() => { if (config.data && !draft) setDraft(config.data) }, [config.data, draft])
  const save = useMutation({
    mutationFn: (value: FirstBoardConfig) => api.firstBoardSaveConfig(value),
    onSuccess: value => {
      client.setQueryData(QK.firstBoardConfig, value)
      setDraft(value)
      setFeedback(`规则已保存为版本 ${value.revision}。`)
      void client.invalidateQueries({ queryKey: QK.firstBoard })
    },
    onError: () => { void config.refetch() },
  })
  const refresh = useMutation({ mutationFn: api.firstBoardRefresh, onSuccess: () => {
    void client.invalidateQueries({ queryKey: QK.firstBoardSnapshot })
  } })
  const order = useMutation({ mutationFn: api.firstBoardPaperOrder, onSuccess: value => {
    setFeedback(value.order.status === 'filled' ? '模拟委托已成交，可在模拟盘查看费用与记录。' : '模拟委托已提交，请在模拟盘查看实际撮合结果。')
    void client.invalidateQueries({ queryKey: QK.firstBoard })
    void client.invalidateQueries({ queryKey: QK.paperAll })
  } })
  const data = snapshot.data
  const rows = useMemo(() => sortRows(
    (data?.rows ?? []).filter(row => (filter === 'all' || row.pattern === filter) && (!onlyActionable || row.can_buy)),
    candidateColumns,
  ), [data?.rows, filter, onlyActionable, sortRows])
  const capabilities = matrix.data?.capabilities ?? []
  const liveUsable = !snapshot.isError && data?.status === 'ready' && !!config.data?.enabled
    && !matrix.isError && ['daily', 'realtime'].every(id => capabilities.some(item => item.id === id && item.usable))
  const missing = capabilities.filter(item => ['daily', 'realtime'].includes(item.id) && !item.usable)
  const openStock = (symbol: string, name: string) => setStock({ symbol, name })
  const selectTab = (id: string) => { const next = new URLSearchParams(params); next.set('tab', id); setParams(next) }
  const loadRules = (rules: FirstBoardRules) => { if (config.data) setDraft({ ...(draft ?? config.data), rules }); selectTab('rules'); setFeedback('实验规则已载入草稿。请审阅参数后保存为新版本。') }
  const sellEvent = (event: FirstBoardEvent) => {
    const qty = event.evidence.qty
    if (typeof qty === 'number' && qty > 0) order.mutate({ event_id: event.id, side: 'sell', qty })
  }
  const navRows = tab === 'live' ? rows : data?.rows
  const navList = useMemo(() => navRows?.filter((row, index, all) => all.findIndex(other => other.symbol === row.symbol) === index).map(row => ({ symbol: row.symbol, name: row.name })), [navRows])

  return <div className="min-w-0">
    <PageHeader title="首板模式" className="flex-wrap pl-14 lg:pl-5" right={<div className="flex flex-wrap items-center gap-2"><span className="text-xs text-secondary">{data ? statuses[data.status] : '正在连接'}</span><button className={buttonClass} disabled={snapshot.isFetching || refresh.isPending || !config.data} onClick={() => refresh.mutate()}><RefreshCw className={cn('h-3.5 w-3.5', snapshot.isFetching && 'motion-safe:animate-spin')} />刷新</button></div>} />
    <div role="tablist" aria-label="首板工作台" className="flex overflow-x-auto border-b border-border px-4">{tabs.map((item, index) => <button key={item.id} id={`first-board-tab-${item.id}`} type="button" role="tab" tabIndex={tab === item.id ? 0 : -1} aria-selected={tab === item.id} aria-controls={`first-board-panel-${item.id}`} onClick={() => selectTab(item.id)} onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; selectTab(tabs[next].id); document.getElementById(`first-board-tab-${tabs[next].id}`)?.focus() } }} className={cn('shrink-0 border-b-2 px-4 py-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', tab === item.id ? 'border-accent font-medium text-foreground' : 'border-transparent text-secondary hover:text-foreground')}>{item.label}</button>)}</div>
    <div className="space-y-4 p-4 sm:p-5">
      <ErrorNotice error={config.error} retry={() => void config.refetch()} />
      {config.isLoading && <LoadingRows />}
      {feedback && <Notice><div className="flex items-center justify-between gap-3"><span>{feedback}</span><button className={buttonClass} onClick={() => setFeedback('')}>关闭提示</button></div></Notice>}
      {config.data && <div role="tabpanel" id={`first-board-panel-${tab}`} aria-labelledby={`first-board-tab-${tab}`} className="space-y-4">
        {tab === 'live' && <>
          <Notice>近 {config.data.rules.lookback_days} 个交易日无收盘涨停 · {universeLabels[config.data.rules.universe]}。临近涨停是买入候选提示，不能证明扫板、排板能够成交。涨停价观察也不能证明封单稳定。</Notice>
          <ErrorNotice error={snapshot.error ?? refresh.error} retry={() => refresh.mutate()} />
          {matrix.isError && <Notice error>行情能力状态读取失败。<a href="/settings?tab=data-sources" className="ml-2 underline">检查数据源配置</a></Notice>}
          {!!missing.length && <Notice>当前数据源缺少可用的{missing.map(item => item.label).join('、')}。<a href="/settings?tab=data-sources" className="ml-2 underline">配置数据源</a></Notice>}
          {data && <section className={`${panelClass} space-y-3`}><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">{statuses[data.status]}</h2><span className="text-xs text-secondary">交易日期 {data.as_of ?? '—'} · 快照 {stamp(data.observed_at)} · 规则版本 {data.revision}</span></div><p className="text-sm leading-relaxed">{data.message}</p><div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-secondary"><span>市场环境：{data.environment.label || '待确认'}{data.environment.date ? `（${data.environment.date}）` : ''}</span><span>{data.environment.allowed ? '环境允许提示' : '环境暂不允许提示'}</span><span>候选形态 {data.coverage.candidate_count} · 股票 {data.coverage.symbol_count}</span><span>报价覆盖 {data.coverage.quote_count} · 新鲜报价 {data.coverage.fresh_count}</span></div>{data.environment.reason && <p className="text-xs leading-relaxed text-secondary">{data.environment.reason}</p>}{!config.data.enabled && <button className={buttonClass} onClick={() => selectTab('rules')}>前往规则版本开启盯盘</button>}</section>}
          <section className={panelClass}><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">今日候选 <span className="font-normal text-secondary">{rows.length} 条</span></h2><div className="flex flex-wrap items-center gap-4 text-xs"><label className="flex shrink-0 items-center gap-2 whitespace-nowrap">形态<select className={inputClass} value={filter} onChange={event => setFilter(event.target.value as FirstBoardPattern | 'all')}><option value="all">全部形态</option>{Object.entries(patternLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="flex items-center gap-2"><input type="checkbox" checked={onlyActionable} onChange={event => setOnlyActionable(event.target.checked)} />只看可提示候选</label></div></div>
            {snapshot.isLoading ? <LoadingRows /> : rows.length ? <div className="overflow-x-auto"><table aria-label="首板实时候选" className="w-full min-w-[1100px] text-left text-xs"><thead><tr className="text-secondary">{candidateColumns.map(column => {
              const sortable = sortableColumns.has(column.id)
              const active = sort?.key === column.id
              const Icon = active ? sort.dir === 'asc' ? ArrowUp : ArrowDown : ArrowUpDown
              const action = active ? sort.dir === 'asc' ? '当前升序，点击降序' : '当前降序，点击恢复默认顺序' : '点击升序排列'
              return <th key={column.id} scope="col" aria-sort={sortable ? active ? sort.dir === 'asc' ? 'ascending' : 'descending' : 'none' : undefined} className="whitespace-nowrap px-3 pb-3 font-normal first:pl-0">
                {sortable ? <button type="button" aria-label={`按${column.label}排序`} title={[action, categorySortHints[column.id]].filter(Boolean).join('；')} className={cn('inline-flex min-h-9 items-center gap-1 rounded hover:text-foreground focus-visible:outline focus-visible:outline-accent', active && 'font-medium text-foreground')} onClick={() => toggleSort(column.id)}>{column.label}<Icon className={cn('h-3 w-3', active && 'text-accent')} aria-hidden="true" /></button> : column.label}
              </th>
            })}</tr></thead><tbody>{rows.map(row => { const board = boardTag(row.symbol); return <tr key={`${row.symbol}-${row.pattern}`} className="border-t border-border"><td className="py-3 pr-3 align-top"><button className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => openStock(row.symbol, row.name)}>{row.name}{board && <span className={`ml-1 shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>{board.label}</span>}<span className="block font-normal text-secondary">{row.symbol}</span></button><span className="mt-1 block text-secondary">{row.sector || '行业未确认'}</span></td><td className="px-3 py-3 align-top"><PatternBadge pattern={row.pattern} label={row.pattern_label} /></td><td className="px-3 py-3 align-top"><StateBadge state={row.state} /></td><td className={cn("px-3 py-3 align-top tabular-nums", priceColorClass(row.price ? row.change_pct : null))}>{fmtPrice(row.price)}</td><td className={cn("px-3 py-3 align-top tabular-nums", priceColorClass(row.change_pct))}>{fmtPct(row.change_pct)}</td><td className="px-3 py-3 align-top tabular-nums">{pct(row.distance_to_limit_pct)}</td><td className="px-3 py-3 align-top tabular-nums">{pct(row.turnover_rate, 1)}</td><td className="min-w-56 max-w-80 px-3 py-3 align-top leading-relaxed"><p>{row.blocked_reasons.length ? row.blocked_reasons.join('；') : row.can_buy ? '达到实验提示条件，仍需确认可成交性。' : '继续观察'}</p><details className="mt-2 text-secondary"><summary className="cursor-pointer">入选依据</summary><ul className="mt-1 space-y-1">{row.reasons.map(reason => <li key={reason}>{reason}</li>)}{row.sector_confirmation?.reason && <li>{row.sector_confirmation.reason}</li>}<li>形态参考日期：{String(row.evidence.reference_date ?? '—')}</li>{typeof row.evidence.breakout_price_raw === 'number' && <li>原价突破参考：{money(row.evidence.breakout_price_raw)} 元</li>}</ul></details></td><td className="whitespace-nowrap px-3 py-3 align-top tabular-nums">{stamp(row.quote_time)}</td><td className="px-3 py-3 align-top"><button className={buttonClass} disabled={!liveUsable || !row.can_buy || !row.event_id || !row.suggested_amount || order.isPending} onClick={() => row.event_id && row.suggested_amount && order.mutate({ event_id: row.event_id, side: 'buy', amount: row.suggested_amount })}>模拟买入</button><span className="mt-1 block text-secondary">{row.suggested_amount ? `${money(row.suggested_amount)} 元` : '需关联账户及可用额度'}</span></td></tr> })}</tbody></table></div> : <p className="py-6 text-sm leading-relaxed text-secondary">{onlyActionable ? '当前没有通过全部确认条件的候选，可取消筛选查看阻断原因。' : '暂无匹配的首板候选。系统需要完成日线建立观察池，并等待有效盘中报价；没有候选时不会生成买入提示。'}</p>}
          </section>
          <ErrorNotice error={order.error} />
          {data?.paper && <section className={panelClass}><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">关联模拟账户</h2><a href="/paper" className="text-xs underline">查看模拟盘</a></div><p className="mt-2 text-xs leading-relaxed text-secondary">{data.paper.note || (data.paper.initialized ? '账户统计包含该账户全部交易，不能解释为首板策略独立胜率。' : '尚未关联可用账户。可先记录信号，在规则版本中选择模拟账户。')}</p>{data.paper.initialized && data.paper.stats && <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-3 text-xs"><div><dt className="text-secondary">账户已完成轮次</dt><dd className="mt-1 tabular-nums">{data.paper.stats.rounds}</dd></div><div><dt className="text-secondary">账户胜率</dt><dd className="mt-1 tabular-nums">{pct(data.paper.stats.win_rate, 1)}</dd></div><div><dt className="text-secondary">账户已实现盈亏（元）</dt><dd className="mt-1 tabular-nums">{money(data.paper.stats.realized_pnl)}</dd></div></dl>}{data.paper.holdings?.length ? <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[440px] text-left text-xs" aria-label="关联账户持仓"><thead><tr className="text-secondary"><th className="py-2 font-normal">股票</th><th className="py-2 font-normal">持仓股数</th><th className="py-2 font-normal">成本价</th><th className="py-2 font-normal">持仓盈亏（元）</th></tr></thead><tbody>{data.paper.holdings.map(holding => <tr key={holding.symbol} className="border-t border-border"><td className="py-2"><button onClick={() => openStock(holding.symbol, holding.symbol)} className="hover:underline">{holding.symbol}</button></td><td className="py-2 tabular-nums">{holding.qty}</td><td className="py-2 tabular-nums">{money(holding.avg_cost)}</td><td className="py-2 tabular-nums">{money(holding.pnl)}</td></tr>)}</tbody></table></div> : null}</section>}
          {!!data?.limitations.length && <details className="text-xs text-secondary"><summary className="cursor-pointer">当前实现的边界</summary><ul className="ml-4 mt-2 list-disc space-y-2 leading-relaxed">{data.limitations.map(item => <li key={item}>{item}</li>)}</ul></details>}
        </>}
        {tab === 'events' && <section className={`${panelClass} space-y-4`}><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">信号记录 <span className="font-normal text-secondary">{events.data?.total ?? 0} 条</span></h2><label className="flex items-center gap-2 text-xs">交易日期<input className={inputClass} type="date" max={beijingDay()} value={eventDay} onChange={event => { if (event.target.value) setEventDay(event.target.value) }} /></label></div><p className="text-xs leading-relaxed text-secondary">记录状态变化时的价格、规则版本与阻断原因。历史提示不代表当前仍可买入，模拟委托会在提交时重新核验行情。</p><ErrorNotice error={events.error} retry={() => void events.refetch()} /><ErrorNotice error={order.error} />{events.isLoading ? <LoadingRows /> : events.data?.events.length ? <ul className="divide-y divide-border">{events.data.events.map(event => <li key={event.id} className="space-y-2 py-4"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-3"><button className="text-sm font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => openStock(event.symbol, event.name)}>{event.name} <span className="font-normal text-secondary">{event.symbol}</span></button><EventBadge type={event.event_type} /><PatternBadge pattern={event.pattern} label={event.pattern_label} /></div><span className="text-xs text-secondary">{stamp(event.ts)} · 版本 {event.revision}</span></div><p className="text-sm leading-relaxed">{event.message}</p><div className="flex flex-wrap items-center justify-between gap-3 text-xs text-secondary"><span>信号价格 {money(event.price)} 元 · 行情时间 {stamp(event.quote_time)}</span>{event.event_type === 'exit_candidate' && <button className={buttonClass} disabled={!liveUsable || event.date !== beijingDay() || typeof event.evidence.qty !== 'number' || event.evidence.qty <= 0 || order.isPending} onClick={() => sellEvent(event)}>模拟卖出</button>}</div>{event.blocked_reasons.length > 0 && <p className="text-xs text-secondary">{event.blocked_reasons.join('；')}</p>}</li>)}</ul> : <p className="py-5 text-sm text-secondary">该日暂无首板信号。开启盯盘后，符合条件的状态变化会记录在这里。</p>}</section>}
        {tab === 'research' && <><ErrorNotice error={versions.error} retry={() => void versions.refetch()} /><ResearchPanel config={config.data} versions={versions.data?.versions ?? []} onOpenStock={openStock} onLoadRules={loadRules} /></>}
        {tab === 'rules' && draft && <><ErrorNotice error={versions.error ?? accounts.error} retry={() => { void versions.refetch(); void accounts.refetch() }} /><RulesPanel config={config.data} draft={draft} onDraft={setDraft} onSave={() => save.mutate(draft)} versions={versions.data?.versions ?? []} accounts={accounts.data?.accounts ?? []} saving={save.isPending} error={save.error} reload={() => { if (config.data) setDraft(config.data); save.reset() }} /></>}
      </div>}
    </div>
    <StockPreviewDialog symbol={stock?.symbol ?? null} name={stock?.name} onClose={() => setStock(null)} navList={navList?.some(item => item.symbol === stock?.symbol) ? navList : undefined} onNavigate={(symbol, name) => setStock({ symbol, name: name ?? symbol })} />
  </div>
}
