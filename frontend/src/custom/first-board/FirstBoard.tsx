import { useEffect, useId, useMemo, useState, type KeyboardEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ChevronDown, ChevronUp, RefreshCw } from 'lucide-react'
import { api, type FirstBoardConfig, type FirstBoardEvent, type FirstBoardEventFilters, type FirstBoardRules, type FirstBoardSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { useStockMarks } from '@/lib/useStockMarks'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { RulesPanel } from './RulesPanel'
import { ResearchPanel } from './ResearchPanel'
import { YesterdayFeedback } from './YesterdayFeedback'
import { OpeningDistribution } from './OpeningDistribution'
import { DataStatus } from './DataStatus'
import { CandidateTable, useCandidateView } from './CandidateTable'
import { EventsPanel } from './EventsPanel'
import { beijingDay, buttonClass, ErrorNotice, LoadingRows, money, Notice, panelClass, pct, stamp } from './shared'

const tabs = [{ id: 'live', label: '今日盯盘' }, { id: 'events', label: '信号记录' }, { id: 'research', label: '效果验证' }, { id: 'rules', label: '规则版本' }]
const statuses = { disabled: '监控未开启', waiting: '等待数据', ready: '盯盘中', stale: '数据已过期', closed: '非连续竞价时段', error: '暂不可用' }
// 盯盘与当日信号需要及时的可成交状态；其他标签页只刷新页头状态。
const LIVE_POLL_MS = 5000
const IDLE_POLL_MS = 30000

function PaperAccount({ paper, onOpen }: { paper: FirstBoardSnapshot['paper']; onOpen: (symbol: string, name: string) => void }) {
  return <section className={panelClass}>
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">关联模拟账户</h2><a href="/paper" className="text-xs underline">查看模拟盘</a></div>
    <p className="mt-2 text-xs leading-relaxed text-secondary">{paper.note || (paper.initialized ? '账户统计包含该账户全部交易，不能解释为首板策略独立胜率。' : '尚未关联可用账户。可先记录信号，在规则版本中选择模拟账户。')}</p>
    {paper.initialized && paper.stats && <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-3 text-xs">
      <div><dt className="text-secondary">账户已完成轮次</dt><dd className="mt-1 tabular-nums">{paper.stats.rounds}</dd></div>
      <div><dt className="text-secondary">账户胜率</dt><dd className="mt-1 tabular-nums">{pct(paper.stats.win_rate, 1)}</dd></div>
      <div><dt className="text-secondary">账户已实现盈亏（元）</dt><dd className="mt-1 tabular-nums">{money(paper.stats.realized_pnl)}</dd></div>
    </dl>}
    {paper.holdings?.length ? <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[440px] text-left text-xs" aria-label="关联账户持仓">
      <thead><tr className="text-secondary"><th className="py-2 font-normal">股票</th><th className="py-2 font-normal">持仓股数</th><th className="py-2 font-normal">成本价</th><th className="py-2 font-normal">持仓盈亏（元）</th></tr></thead>
      <tbody>{paper.holdings.map(holding => <tr key={holding.symbol} className="border-t border-border"><td className="py-2"><button onClick={() => onOpen(holding.symbol, holding.symbol)} className="hover:underline">{holding.symbol}</button></td><td className="py-2 tabular-nums">{holding.qty}</td><td className="py-2 tabular-nums">{money(holding.avg_cost)}</td><td className="py-2 tabular-nums">{money(holding.pnl)}</td></tr>)}</tbody>
    </table></div> : null}
  </section>
}

export function FirstBoard() {
  const [params, setParams] = useSearchParams()
  const tab = tabs.some(item => item.id === params.get('tab')) ? params.get('tab')! : 'live'
  const client = useQueryClient()
  const config = useQuery({ queryKey: QK.firstBoardConfig, queryFn: api.firstBoardConfig })
  const [eventDay, setEventDay] = useState(beijingDay)
  const [eventFilters, setEventFilters] = useState<FirstBoardEventFilters>({})
  const pollSignals = eventDay === beijingDay() && !!config.data?.enabled
  const snapshotPoll = tab === 'live' || (tab === 'events' && pollSignals) ? LIVE_POLL_MS : IDLE_POLL_MS
  const snapshot = useQuery({ queryKey: QK.firstBoardSnapshot, queryFn: api.firstBoardSnapshot, enabled: !!config.data, refetchInterval: snapshotPoll, retry: 1 })
  const matrix = useCapabilityMatrix()
  const versions = useQuery({ queryKey: QK.firstBoardVersions, queryFn: api.firstBoardVersions, enabled: tab === 'rules' || tab === 'research' })
  const accounts = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts, enabled: tab === 'rules' })
  const events = useQuery({ queryKey: QK.firstBoardEvents(eventDay, eventFilters), queryFn: () => api.firstBoardEvents(eventDay, eventFilters), enabled: tab === 'events', refetchInterval: pollSignals ? LIVE_POLL_MS : false })
  const [draft, setDraft] = useState<FirstBoardConfig | null>(null)
  const [monitorExpanded, setMonitorExpanded] = useState(true)
  const monitorDetailsId = useId()
  const stockMarks = useStockMarks(storage.firstBoardStockMarks)
  const attentionCurrent = !snapshot.isError && snapshot.data?.status === 'ready'
  const view = useCandidateView(snapshot.data?.rows, attentionCurrent, stockMarks.marks)
  const { rows } = view
  const [stock, setStock] = useState<{ symbol: string; name: string; navList?: { symbol: string; name: string }[] } | null>(null)
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
  function refreshPage() {
    if (tab !== 'events') { refresh.mutate(); return }
    void events.refetch()
    void client.invalidateQueries({ queryKey: QK.firstBoardRankingsDay(eventDay) })
  }
  const order = useMutation({ mutationFn: api.firstBoardPaperOrder, onSuccess: value => {
    setFeedback(value.order.status === 'filled' ? '模拟委托已成交，可在模拟盘查看费用与记录。' : '模拟委托已提交，请在模拟盘查看实际撮合结果。')
    void client.invalidateQueries({ queryKey: QK.firstBoard })
    void client.invalidateQueries({ queryKey: QK.paperAll })
  } })
  const data = snapshot.data
  const capabilities = matrix.data?.capabilities ?? []
  const liveUsable = !snapshot.isError && data?.status === 'ready' && !!config.data?.enabled
    && !matrix.isError && ['daily', 'realtime'].every(id => capabilities.some(item => item.id === id && item.usable))
  const missing = capabilities.filter(item => ['daily', 'realtime'].includes(item.id) && !item.usable)
  const openStock = (symbol: string, name: string) => setStock({ symbol, name })
  const openList = (symbol: string, name: string, navList: { symbol: string; name: string }[]) => setStock({ symbol, name, navList })
  const selectTab = (id: string) => { const next = new URLSearchParams(params); next.set('tab', id); setParams(next) }
  const loadRules = (rules: FirstBoardRules) => { if (config.data) setDraft({ ...(draft ?? config.data), rules }); selectTab('rules'); setFeedback('实验规则已载入草稿。请审阅参数后保存为新版本。') }
  const sellEvent = (event: FirstBoardEvent) => {
    const qty = event.evidence.qty
    if (typeof qty === 'number' && qty > 0) order.mutate({ event_id: event.id, side: 'sell', qty })
  }
  const navRows = tab === 'live' ? rows : data?.rows
  const navList = useMemo(() => navRows?.filter((row, index, all) => all.findIndex(other => other.symbol === row.symbol) === index).map(row => ({ symbol: row.symbol, name: row.name })), [navRows])
  const onTabKey = (event: KeyboardEvent, index: number) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    selectTab(tabs[next].id)
    document.getElementById(`first-board-tab-${tabs[next].id}`)?.focus()
  }

  return <div className="min-w-0">
    <PageHeader title="首板模式" className="flex-wrap pl-14 lg:pl-5" right={<div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-secondary">{data ? statuses[data.status] : '正在连接'}</span>
      <button className={buttonClass} disabled={!config.data || (tab === 'events' ? events.isFetching : snapshot.isFetching || refresh.isPending)} onClick={refreshPage}><RefreshCw className={cn('h-3.5 w-3.5', snapshot.isFetching && 'motion-safe:animate-spin')} />刷新</button>
    </div>} />
    <div role="tablist" aria-label="首板工作台" className="flex overflow-x-auto border-b border-border px-4">{tabs.map((item, index) => <button key={item.id} id={`first-board-tab-${item.id}`} type="button" role="tab"
      tabIndex={tab === item.id ? 0 : -1} aria-selected={tab === item.id} aria-controls={`first-board-panel-${item.id}`} onClick={() => selectTab(item.id)} onKeyDown={event => onTabKey(event, index)}
      className={cn('shrink-0 border-b-2 px-4 py-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', tab === item.id ? 'border-accent font-medium text-foreground' : 'border-transparent text-secondary hover:text-foreground')}>{item.label}</button>)}</div>
    <div className="space-y-4 p-4 sm:p-5">
      <ErrorNotice error={config.error} retry={() => void config.refetch()} />
      {config.isLoading && <LoadingRows />}
      {feedback && <Notice><div className="flex items-center justify-between gap-3"><span>{feedback}</span><button className={buttonClass} onClick={() => setFeedback('')}>关闭提示</button></div></Notice>}
      {config.data && <div role="tabpanel" id={`first-board-panel-${tab}`} aria-labelledby={`first-board-tab-${tab}`} className="space-y-4">
        {tab === 'live' && <>
          <ErrorNotice error={snapshot.error ?? refresh.error} retry={() => refresh.mutate()} />
          {matrix.isError && <Notice error>行情能力状态读取失败。<a href="/settings?tab=data-sources" className="ml-2 underline">检查数据源配置</a></Notice>}
          {!!missing.length && <Notice>当前数据源缺少可用的{missing.map(item => item.label).join('、')}。<a href="/settings?tab=data-sources" className="ml-2 underline">配置数据源</a></Notice>}
          {data && <section className={`${panelClass} space-y-3`}>
            <header className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2">
              <h2 className="text-sm font-semibold">{statuses[data.status]}</h2>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-secondary">
                <span>市场环境：{data.environment.label || '待确认'}{data.environment.date ? `（${data.environment.date}）` : ''}</span>
                <span>{data.environment.allowed ? '环境允许提示' : '环境暂不允许提示'}</span>
                <span>候选形态 {data.coverage.candidate_count} · 股票 {data.coverage.symbol_count}</span>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-xs text-secondary">交易日期 {data.as_of ?? '—'} · 快照 {stamp(data.observed_at)} · 规则版本 {data.revision}</span>
                <button type="button" className={buttonClass} aria-label={monitorExpanded ? '收起盯盘详情' : '展开盯盘详情'} aria-expanded={monitorExpanded} aria-controls={monitorDetailsId} onClick={() => setMonitorExpanded(value => !value)}>
                  {monitorExpanded ? '收起' : '展开'}{monitorExpanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
                </button>
              </div>
            </header>
            <DataStatus data={data.data_status} retained={snapshot.isError || refresh.isError} />
            <div id={monitorDetailsId} hidden={!monitorExpanded}>
              <YesterdayFeedback data={data.yesterday_feedback} refreshFailed={snapshot.isError || refresh.isError} onOpenStock={openList}>
                <OpeningDistribution feedback={data.yesterday_feedback} refreshFailed={snapshot.isError || refresh.isError} />
              </YesterdayFeedback>
            </div>
          </section>}
          <CandidateTable view={view} loading={snapshot.isLoading} current={attentionCurrent} day={data?.as_of ?? ''} marks={stockMarks}
            canOrder={liveUsable && !order.isPending} onOpen={openStock} onBuy={(eventId, amount) => order.mutate({ event_id: eventId, side: 'buy', amount })} />
          <ErrorNotice error={order.error} />
          {data?.paper && <PaperAccount paper={data.paper} onOpen={openStock} />}
          {!!data?.limitations.length && <details className="text-xs text-secondary"><summary className="cursor-pointer">当前实现的边界</summary><ul className="ml-4 mt-2 list-disc space-y-2 leading-relaxed">{data.limitations.map(item => <li key={item}>{item}</li>)}</ul></details>}
        </>}
        {tab === 'events' && <EventsPanel events={events} day={eventDay} onDay={setEventDay} filters={eventFilters} onFilters={setEventFilters} poll={pollSignals}
          canSell={liveUsable && !order.isPending} orderError={order.error} onOpen={openStock} onOpenList={openList} onSell={sellEvent} />}
        {tab === 'research' && <><ErrorNotice error={versions.error} retry={() => void versions.refetch()} /><ResearchPanel config={config.data} versions={versions.data?.versions ?? []} onOpenStock={openStock} onLoadRules={loadRules} /></>}
        {tab === 'rules' && draft && <><ErrorNotice error={versions.error ?? accounts.error} retry={() => { void versions.refetch(); void accounts.refetch() }} /><RulesPanel config={config.data} draft={draft} onDraft={setDraft} onSave={() => save.mutate(draft)} versions={versions.data?.versions ?? []} accounts={accounts.data?.accounts ?? []} saving={save.isPending} error={save.error} reload={() => { if (config.data) setDraft(config.data); save.reset() }} /></>}
      </div>}
    </div>
    <StockPreviewDialog symbol={stock?.symbol ?? null} name={stock?.name} onClose={() => setStock(null)} navList={stock?.navList ?? (navList?.some(item => item.symbol === stock?.symbol) ? navList : undefined)} onNavigate={(symbol, name) => setStock(current => ({ symbol, name: name ?? symbol, navList: current?.navList }))} />
  </div>
}
