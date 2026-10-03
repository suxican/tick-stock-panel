import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Check, RefreshCw, Search } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { boardTag } from '@/components/stock-table/primitives'
import { useTableSort, type SortState } from '@/components/stock-table/useTableSort'
import { api, type HuichunCandidate, type HuichunReturn, type HuichunSnapshot, type MarketSnapshotRow } from '@/lib/api'
import type { ColumnConfig } from '@/lib/list-columns'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPrice, fmtPct, priceColorClass } from '@/lib/format'
import { RulesPanel } from './RulesPanel'
import { buttonClass, ErrorNotice, inputClass, LoadingRows, Notice, Pagination, panelClass, pct, price, primaryClass, stamp } from './shared'

const tabs = [{ id: 'candidates', label: '候选筛选' }, { id: 'tracking', label: '收益跟踪' }, { id: 'rules', label: '规则设置' }] as const
type Tab = typeof tabs[number]['id']
const PAGE_SIZE = 50
type CandidateRow = HuichunCandidate & { latest_price?: number | null; change_pct?: number | null }
const candidateColumns: ColumnConfig[] = [
  ['name', '名称'], ['symbol', '代码'], ['latest_price', '现价'], ['change_pct', '涨跌幅'],
  ['signal_date', '信号日期'], ['rally_return', '前段涨幅'], ['zero_distance', '零轴距离'],
  ['close_above_ma_pct', '高于均线'], ['ma_slope_pct', '均线涨幅'], ['raw_close', '信号收盘（元）'],
].map(([id, label]) => ({ id, label, source: { type: 'builtin', key: id }, visible: true }))

function candidateSortValue(row: CandidateRow, column: ColumnConfig) {
  const value = row[column.id as keyof CandidateRow]
  return typeof value === 'number' ? Number.isFinite(value) ? value : null : value ?? null
}
function latestPrice(quote: MarketSnapshotRow | undefined) {
  const value = quote?.raw_close ?? quote?.close
  return value != null && Number.isFinite(value) && value > 0 ? value : null
}
function latestChange(quote: MarketSnapshotRow | undefined) {
  const value = quote?.change_pct
  return value != null && Number.isFinite(value) ? value : null
}
function SortButton({ column, sort, onSort }: { column: ColumnConfig; sort: SortState | null; onSort: (key: string) => void }) {
  const active = sort?.key === column.id
  const Icon = active ? sort.dir === 'asc' ? ArrowUp : ArrowDown : ArrowUpDown
  return <button type="button" aria-label={`按${column.label}排序`} title={active ? sort.dir === 'asc' ? '当前升序，点击降序' : '当前降序，点击恢复默认顺序' : '点击升序排列'} className={cn('inline-flex min-h-9 items-center gap-1 whitespace-nowrap rounded hover:text-foreground focus-visible:outline focus-visible:outline-accent', active && 'text-accent')} onClick={() => onSort(column.id)}>{column.label}<Icon className="h-3 w-3" aria-hidden="true" /></button>
}
const returnLabels: Record<HuichunReturn['status'], string> = { ok: '已计算', pending: '未到期', unknown_gap: '数据缺失', invalid_factor: '复权异常', baseline_changed: '基准已变化' }
const returnDescriptions: Record<HuichunReturn['status'], string> = {
  ok: '目标交易日复权收盘价相对信号日的涨跌幅', pending: '尚未取得目标交易日的完整收盘数据',
  unknown_gap: '观察窗口内存在缺失数据，暂不计算收益', invalid_factor: '复权数据未通过核验，暂不计算收益',
  baseline_changed: '信号日价格或复权依据发生变化，原跟踪基准需要重新核验',
}

function ReturnCell({ value }: { value: HuichunReturn | undefined }) {
  if (!value) return <td className="px-3 py-3 text-secondary">—</td>
  return <td className="whitespace-nowrap px-3 py-3" title={returnDescriptions[value.status]}><span className={cn('tabular-nums', value.status === 'ok' && value.return_pct != null ? value.return_pct > 0 ? 'text-bull' : value.return_pct < 0 ? 'text-bear' : 'text-foreground' : 'text-secondary')}>{value.status === 'ok' ? pct(value.return_pct, true) : returnLabels[value.status]}</span><span className="mt-1 block text-[11px] text-secondary">{value.target_date ?? '日期待确认'}</span></td>
}
function JobStatus({ data }: { data: HuichunSnapshot | undefined }) {
  const job = data?.job
  if (!job || job.status === 'idle') return null
  if (job.status === 'failed') return <Notice error>{job.kind === 'tracking' ? '收益刷新失败' : '筛选失败'}：{job.error || '请稍后重试。'}</Notice>
  if (job.status !== 'running') return null
  return <Notice><div className="flex flex-wrap items-center justify-between gap-2"><span>{job.kind === 'tracking' ? '正在更新跟踪收益' : '正在按已保存规则筛选'}，完成后自动更新。</span><span className="tabular-nums">{job.processed_symbols} / {job.total_symbols || '待统计'}</span></div>{job.total_symbols > 0 && <progress className="mt-2 h-1.5 w-full accent-accent" value={job.processed_symbols} max={job.total_symbols} aria-label="任务进度" />}</Notice>
}

export function Huichun() {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const tab: Tab = tabs.some(item => item.id === params.get('tab')) ? params.get('tab') as Tab : 'candidates'
  const config = useQuery({ queryKey: QK.huichunConfig, queryFn: api.huichunConfig, staleTime: 30_000 })
  const snapshot = useQuery({ queryKey: QK.huichunSnapshot, queryFn: api.huichunSnapshot, staleTime: 10_000,
    refetchInterval: query => query.state.data?.job.status === 'running' ? 2000 : false })
  const tracking = useQuery({ queryKey: QK.huichunTracking, queryFn: api.huichunTracking, staleTime: 30_000 })
  const [dateMode, setDateMode] = useState<'latest' | 'range'>('latest')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [dateError, setDateError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [page, setPage] = useState(0)
  const [trackingPage, setTrackingPage] = useState(0)
  const { sort, toggle: toggleSort, sortRows } = useTableSort<CandidateRow>(candidateSortValue)
  const [stock, setStock] = useState<{ symbol: string; name: string } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const finishedTrackingJob = useRef<string | null>(null)
  const scanResult = snapshot.data?.scan
  const busy = snapshot.data?.job.status === 'running'
  const setSnapshot = (data: HuichunSnapshot) => client.setQueryData(QK.huichunSnapshot, data)
  const scan = useMutation({ mutationFn: api.huichunScan, onSuccess: data => { setSnapshot(data); setNotice(null) } })
  const addTracking = useMutation({ mutationFn: api.huichunAddTracking, onSuccess: data => { client.setQueryData(QK.huichunTracking, data); void client.invalidateQueries({ queryKey: QK.huichunSnapshot }); setSelected([]); setNotice('已加入收益跟踪，可在“收益跟踪”查看后续 1～5 个交易日的表现。') } })
  const refresh = useMutation({ mutationFn: api.huichunRefreshTracking, onSuccess: setSnapshot })
  const remove = useMutation({ mutationFn: api.huichunRemoveTracking, onSuccess: data => client.setQueryData(QK.huichunTracking, data) })
  const records = tracking.data?.records ?? []
  const candidates = scanResult?.candidates ?? []
  const tracked = useMemo(() => new Set(records.map(row => `${row.symbol}|${row.signal_date}|${row.rule_revision}`)), [records])
  const candidatePage = Math.min(page, Math.max(0, Math.ceil(candidates.length / PAGE_SIZE) - 1))
  const trackPage = Math.min(trackingPage, Math.max(0, Math.ceil(records.length / PAGE_SIZE) - 1))
  const quoteSort = sort?.key === 'latest_price' || sort?.key === 'change_pct'
  const orderedCandidates = useMemo(() => quoteSort ? candidates : sortRows(candidates, candidateColumns), [candidates, quoteSort, sortRows])
  // 报价排序的请求集合固定为全体候选，避免排名和翻页改变请求集合形成循环。
  const quoteCandidates = quoteSort ? candidates : orderedCandidates.slice(candidatePage * PAGE_SIZE, (candidatePage + 1) * PAGE_SIZE)
  const quoteSymbolsKey = [...new Set(quoteCandidates.map(row => row.symbol))].sort().join(',')
  const quotes = useQuery({
    queryKey: QK.marketSnapshotForSymbols(quoteSymbolsKey),
    queryFn: async ({ signal }) => {
      const symbols = quoteSymbolsKey.split(',')
      const batches: Awaited<ReturnType<typeof api.marketSnapshotForSymbols>>[] = []
      // 全部分批请求成功后才发布，避免只按部分报价重排候选。
      for (let offset = 0; offset < symbols.length; offset += 100) {
        signal.throwIfAborted()
        batches.push(await api.marketSnapshotForSymbols(symbols.slice(offset, offset + 100)))
      }
      return { as_of: batches.every(batch => batch.as_of === batches[0].as_of) ? batches[0].as_of : null, rows: batches.flatMap(batch => batch.rows) }
    },
    enabled: tab === 'candidates' && !!quoteSymbolsKey,
    placeholderData: quoteSort ? previous => previous : undefined,
  })
  const quotesBySymbol = useMemo(() => new Map(quotes.data?.rows.map(row => [row.symbol, row]) ?? []), [quotes.data])
  const sortedCandidates = useMemo(() => quoteSort && quotes.data && !quotes.isPlaceholderData
    ? sortRows(candidates.map(row => ({ ...row, latest_price: latestPrice(quotesBySymbol.get(row.symbol)), change_pct: latestChange(quotesBySymbol.get(row.symbol)) })), candidateColumns)
    : orderedCandidates, [candidates, orderedCandidates, quoteSort, quotes.data, quotes.isPlaceholderData, quotesBySymbol, sortRows])
  const visibleCandidates = sortedCandidates.slice(candidatePage * PAGE_SIZE, (candidatePage + 1) * PAGE_SIZE)
  const availableIds = visibleCandidates.filter(row => !tracked.has(`${row.symbol}|${row.signal_date}|${row.rule_revision}`)).map(row => row.id)
  const navList = useMemo(() => [...new Map((tab === 'tracking' ? records : sortedCandidates).map(row => [row.symbol, { symbol: row.symbol, name: row.name }])).values()], [tab, records, sortedCandidates])
  const actionsUnavailable = busy || snapshot.isError || !snapshot.data
  const canAdd = !actionsUnavailable && !tracking.isError && !!tracking.data && !addTracking.isPending

  useEffect(() => { setSelected([]); setPage(0) }, [scanResult?.id])
  useEffect(() => {
    const job = snapshot.data?.job
    if (job?.kind === 'tracking' && job.status === 'completed' && job.id !== finishedTrackingJob.current) {
      finishedTrackingJob.current = job.id
      void client.invalidateQueries({ queryKey: QK.huichunTracking })
    }
  }, [snapshot.data?.job, client])
  useEffect(() => {
    if (snapshot.data && config.data && snapshot.data.config_revision > config.data.revision) void client.invalidateQueries({ queryKey: QK.huichunConfig })
  }, [snapshot.data?.config_revision, config.data?.revision, client])

  function changeTab(next: Tab) { setParams(previous => { const updated = new URLSearchParams(previous); updated.set('tab', next); return updated }, { replace: true }) }
  function tabKeys(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
    if (next === null) return
    event.preventDefault(); changeTab(tabs[next].id)
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
  }
  function startScan() {
    if (dateMode === 'range' && (!startDate || !endDate || startDate > endDate)) { setDateError('请填写有效的起止日期，开始日期不能晚于结束日期。'); return }
    setDateError(null)
    scan.mutate(dateMode === 'latest' ? {} : { start_date: startDate, end_date: endDate })
  }
  function sortCandidates(key: string) { toggleSort(key); setPage(0) }
  function sortDirection(keys: string[]) { return sort && keys.includes(sort.key) ? sort.dir === 'asc' ? 'ascending' : 'descending' : 'none' }
  const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())
  return <div className="flex h-full min-h-0 flex-col">
    <PageHeader title="回春模式" className="flex-wrap pl-14 lg:pl-5" titleExtra={<span className="rounded bg-elevated px-2 py-1 text-xs text-secondary">A0 日线</span>} right={<span className="text-xs text-secondary">{config.data ? `已保存规则 · 版本 ${config.data.revision}` : '读取规则中'}</span>} />
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div role="tablist" aria-label="回春模式功能" className="flex gap-1 border-b border-border px-4 sm:px-5">{tabs.map((item, index) => <button key={item.id} id={`huichun-tab-${item.id}`} role="tab" aria-selected={tab === item.id} aria-controls={`huichun-panel-${item.id}`} tabIndex={tab === item.id ? 0 : -1} className={cn('min-h-11 border-b-2 px-3 text-sm transition-colors focus-visible:outline focus-visible:outline-accent', tab === item.id ? 'border-accent text-foreground' : 'border-transparent text-secondary hover:text-foreground')} onClick={() => changeTab(item.id)} onKeyDown={event => tabKeys(event, index)}>{item.label}{item.id === 'tracking' && records.length > 0 && <span className="ml-1.5 text-xs tabular-nums text-secondary">{records.length}</span>}</button>)}</div>
      <div id={`huichun-panel-${tab}`} role="tabpanel" aria-labelledby={`huichun-tab-${tab}`} className="space-y-4 p-4 sm:p-5">
        <ErrorNotice error={config.error} retry={() => void config.refetch()} />
        <ErrorNotice error={snapshot.error} retry={() => void snapshot.refetch()} />
        <JobStatus data={snapshot.data} />
        {tab === 'candidates' && <>
          <section className={`${panelClass} space-y-4`} aria-labelledby="huichun-scan-heading">
            <div><h2 id="huichun-scan-heading" className="text-sm font-semibold">筛选候选股票</h2><p className="mt-1.5 text-xs leading-relaxed text-secondary">使用已有沪深主板、创业板和科创板日线，按当前名称排除 ST、*ST，在所选日期寻找符合规则的回春形态。缺失数据不补值，历史交易资格另行核验。</p></div>
            <form className="flex flex-wrap items-end gap-3" onSubmit={event => { event.preventDefault(); startScan() }}>
              <label className="block w-full space-y-1.5 text-xs sm:w-48">信号日期<select className={inputClass} value={dateMode} disabled={busy} onChange={event => { setDateMode(event.target.value as 'latest' | 'range'); setDateError(null) }}><option value="latest">最近完整交易日</option><option value="range">指定日期区间</option></select></label>
              {dateMode === 'range' && <><label className="block min-w-0 flex-1 space-y-1.5 text-xs sm:max-w-44">开始日期<input className={inputClass} type="date" required max={endDate || today} value={startDate} disabled={busy} onChange={event => setStartDate(event.target.value)} /></label><label className="block min-w-0 flex-1 space-y-1.5 text-xs sm:max-w-44">结束日期<input className={inputClass} type="date" required min={startDate} max={today} value={endDate} disabled={busy} onChange={event => setEndDate(event.target.value)} /></label></>}
              <button type="submit" className={primaryClass} disabled={actionsUnavailable || scan.isPending || !config.data || config.isError}><Search className="h-3.5 w-3.5" aria-hidden="true" />{busy && snapshot.data?.job.kind === 'scan' ? '筛选中' : scan.isPending ? '提交中' : '开始筛选'}</button>
              <button type="button" className={buttonClass} onClick={() => changeTab('rules')}>调整规则</button>
            </form>
            {dateError && <Notice error>{dateError}</Notice>}<ErrorNotice error={scan.error} />
          </section>
          {snapshot.isLoading ? <LoadingRows /> : !scanResult ? <div className="py-8 text-sm leading-relaxed text-secondary">尚未生成候选列表。确认规则后点击“开始筛选”，结果会保存在本页。</div> : <>
            {config.data && scanResult.rule_revision !== config.data.revision && <Notice>以下结果使用版本 {scanResult.rule_revision}，当前规则为版本 {config.data.revision}。重新筛选可应用新规则。</Notice>}
            <section className={panelClass} aria-labelledby="huichun-candidates-heading">
              <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 id="huichun-candidates-heading" className="text-sm font-semibold">候选股票 <span className="ml-1 font-normal text-secondary">{candidates.length} 条信号</span></h2><p className="mt-1.5 text-xs text-secondary">{scanResult.start_date} ～ {scanResult.end_date} · 规则版本 {scanResult.rule_revision} · {stamp(scanResult.created_at)}</p></div>{candidates.length > 0 && <button className={buttonClass} disabled={!canAdd || selected.length === 0} onClick={() => addTracking.mutate({ scan_id: scanResult.id, candidate_ids: selected })}>加入所选跟踪{selected.length > 0 ? `（${selected.length}）` : ''}</button>}</div>
              {notice && <div className="mt-3"><Notice>{notice}</Notice></div>}<div className="mt-3"><ErrorNotice error={addTracking.error ?? tracking.error} retry={tracking.error ? () => void tracking.refetch() : undefined} /></div>
              {candidates.length > 0 && <div className="mt-3 space-y-2"><p className="text-xs text-secondary">行情日期：{quotes.data?.as_of ?? (quotes.isFetching ? '读取中' : '暂无行情')} · 现价与涨跌幅为最新行情，信号收盘为历史基准。</p><ErrorNotice error={quotes.error} retry={() => void quotes.refetch()} /></div>}
              {scanResult.coverage.universe !== 'sh_sz_a_shares' && <p className="mt-3 text-xs text-secondary">本次结果为主板范围；重新筛选后纳入创业板和科创板。</p>}
              <p className="mt-3 text-xs text-secondary">{scanResult.coverage.st_filter === 'current_instrument_name' ? `已按当前名称排除 ST、*ST：${scanResult.coverage.st_excluded_count ?? '—'} 只` : '本次结果尚未排除 ST、*ST，请重新筛选。'}</p>
              {quoteSort && (quotes.isPending || quotes.isPlaceholderData) && <p role="status" className="mt-2 text-xs text-secondary">正在读取全部候选行情，完成后排序。</p>}
              {candidates.length === 0 ? <p className="py-8 text-sm text-secondary">该日期范围内暂无符合当前规则的候选。可调整日期或规则后重新筛选。</p> : <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[1280px] text-left text-xs" aria-label="回春候选股票"><thead className="text-secondary"><tr><th className="px-2 py-3"><input type="checkbox" aria-label="选择本页可跟踪候选" checked={availableIds.length > 0 && availableIds.every(id => selected.includes(id))} disabled={!canAdd || availableIds.length === 0} onChange={event => setSelected(previous => event.target.checked ? [...new Set([...previous, ...availableIds])] : previous.filter(id => !availableIds.includes(id)))} /></th><th className="whitespace-nowrap px-3 py-1.5 font-normal" aria-sort={sortDirection(['name', 'symbol'])}><SortButton column={candidateColumns[0]} sort={sort} onSort={sortCandidates} /><span className="mx-1">/</span><SortButton column={candidateColumns[1]} sort={sort} onSort={sortCandidates} /></th>{candidateColumns.slice(2).map(column => <th key={column.id} aria-sort={sortDirection([column.id])} className={cn('whitespace-nowrap px-3 py-1.5 font-normal', ['latest_price', 'change_pct'].includes(column.id) && 'text-right')}><SortButton column={column} sort={sort} onSort={sortCandidates} /></th>)}<th className="px-3 py-3 font-normal">操作</th></tr></thead><tbody>{visibleCandidates.map(row => {
                const added = tracked.has(`${row.symbol}|${row.signal_date}|${row.rule_revision}`)
                const board = boardTag(row.symbol)
                const quote = quotesBySymbol.get(row.symbol)
                const currentPrice = latestPrice(quote)
                const currentChange = latestChange(quote)
                return <tr key={row.id} className="border-t border-border">
                  <td className="px-2 py-3"><input type="checkbox" aria-label={`选择 ${row.name} ${row.signal_date}`} checked={selected.includes(row.id)} disabled={!canAdd || added} onChange={event => setSelected(previous => event.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id))} /></td>
                  <td className="px-3 py-3"><div className="flex items-center gap-2 whitespace-nowrap"><button className="font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => setStock({ symbol: row.symbol, name: row.name })}>{row.name}</button>{board && <span className={cn('rounded border px-1 py-px text-[10px] font-medium', board.color)} title={board.label === '创' ? '创业板' : '科创板'}>{board.label}</span>}</div><span className="mt-1 block whitespace-nowrap font-mono tabular-nums text-secondary">{row.symbol}</span></td>
                  <td className={cn('whitespace-nowrap px-3 py-3 text-right font-medium tabular-nums', priceColorClass(currentPrice == null ? null : currentChange))}>{fmtPrice(currentPrice)}</td>
                  <td className={cn('whitespace-nowrap px-3 py-3 text-right font-medium tabular-nums', priceColorClass(currentChange))}>{fmtPct(currentChange)}</td>
                  <td className="whitespace-nowrap px-3 py-3 tabular-nums">{row.signal_date}</td><td className="px-3 py-3 tabular-nums" title={`${row.g_date} 金叉 → ${row.d_date} 死叉`}>{pct(row.rally_return)}</td><td className="px-3 py-3 tabular-nums">{pct(row.zero_distance)}</td><td className="px-3 py-3 tabular-nums">{pct(row.close_above_ma_pct)}</td><td className="px-3 py-3 tabular-nums">{pct(row.ma_slope_pct)}</td><td className="px-3 py-3 tabular-nums">{price(row.raw_close)}</td><td className="px-3 py-3"><button className={`${buttonClass} whitespace-nowrap`} disabled={!canAdd || added} onClick={() => addTracking.mutate({ scan_id: scanResult.id, candidate_ids: [row.id] })}>{added && <Check className="h-3.5 w-3.5" aria-hidden="true" />}{added ? '已跟踪' : '加入跟踪'}</button></td>
                </tr>
              })}</tbody></table></div>}
              <Pagination page={candidatePage} pages={Math.ceil(candidates.length / PAGE_SIZE)} onPage={setPage} />
              <p className="mt-3 text-xs leading-relaxed text-secondary">以上为形态候选；历史 ST、停牌及可成交条件尚未全部核验。</p>
            </section>
            <details className="text-xs text-secondary"><summary className="cursor-pointer py-1">数据覆盖与筛选说明</summary><div className="mt-2 space-y-2 leading-relaxed"><p>覆盖 {scanResult.coverage.symbol_count} 只股票；复权异常排除 {scanResult.coverage.factor_excluded_count} 只；存在历史缺口 {scanResult.coverage.gap_symbol_count} 只。数据最近日期：{scanResult.coverage.latest_daily_date ?? '未知'}。</p><p>{scanResult.coverage.calendar_source === 'official_exchange_calendar' ? '交易日已按交易所日历核验。' : '交易日暂取已有市场日线记录，完整交易日历缺失，收益计算将标记为数据缺失。'}缺口会中断形态计算，数据不足的区间不产生信号。</p><ul className="ml-4 list-disc space-y-1">{scanResult.limitations.map(item => <li key={item}>{item}</li>)}</ul></div></details>
          </>}
        </>}
        {tab === 'tracking' && <section className={panelClass} aria-labelledby="huichun-tracking-heading">
          <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 id="huichun-tracking-heading" className="text-sm font-semibold">1～5 个交易日收益跟踪</h2><p className="mt-1.5 text-xs text-secondary">{records.length} 条记录 · 更新于 {stamp(tracking.data?.updated_at)}</p></div><button className={buttonClass} disabled={actionsUnavailable || refresh.isPending || tracking.isError || records.length === 0} onClick={() => refresh.mutate()}><RefreshCw className={cn('h-3.5 w-3.5', busy && snapshot.data?.job.kind === 'tracking' && 'motion-safe:animate-spin')} aria-hidden="true" />{busy && snapshot.data?.job.kind === 'tracking' ? '更新中' : '刷新收益'}</button></div>
          <p className="mt-3 max-w-prose text-xs leading-relaxed text-secondary">以信号日收盘为基准，用复权收盘价计算后续第 1～5 个交易日的累计涨跌幅。这是价格观察收益，未计成交限制、费用和滑点。规则调整后，已有记录保留加入时的版本。</p>
          <div className="mt-3 space-y-3"><ErrorNotice error={tracking.error} retry={() => void tracking.refetch()} /><ErrorNotice error={refresh.error ?? remove.error} /></div>
          {tracking.isLoading ? <LoadingRows /> : records.length === 0 ? <div className="py-8 text-sm leading-relaxed text-secondary">尚未添加跟踪股票。到“候选筛选”选择股票并加入跟踪。<button className={`${buttonClass} ml-3`} onClick={() => changeTab('candidates')}>查看候选</button></div> : <><div className="mt-4 overflow-x-auto"><table className="w-full min-w-[1000px] text-left text-xs" aria-label="回春收益跟踪"><thead className="text-secondary"><tr>{['股票', '信号日 / 版本', '基准收盘（元）', 'T+1', 'T+2', 'T+3', 'T+4', 'T+5', '操作'].map(label => <th key={label} className="whitespace-nowrap px-3 py-3 font-normal">{label}</th>)}</tr></thead><tbody>{records.slice(trackPage * PAGE_SIZE, (trackPage + 1) * PAGE_SIZE).map(row => <tr key={row.id} className="border-t border-border"><td className="px-3 py-3"><button className="whitespace-nowrap font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => setStock({ symbol: row.symbol, name: row.name })}>{row.name}</button><span className="mt-1 block text-secondary">{row.symbol}</span></td><td className="whitespace-nowrap px-3 py-3 tabular-nums">{row.signal_date}<details className="mt-1 text-secondary"><summary className="cursor-pointer">版本 {row.rule_revision}</summary><p className="mt-1 whitespace-normal leading-relaxed">前段涨幅 ≥ {pct(row.rules.rally_threshold)}；零轴距离 ≤ {pct(row.rules.zero_threshold)}；均线 {row.rules.ma_window} 日 / 比较 {row.rules.slope_lag} 日；预热 {row.rules.warmup_bars} 根。</p></details></td><td className="px-3 py-3 tabular-nums">{price(row.base_close)}</td>{[1, 2, 3, 4, 5].map(horizon => <ReturnCell key={horizon} value={row.returns.find(value => value.horizon === horizon)} />)}<td className="px-3 py-3"><button className={buttonClass} disabled={busy || remove.isPending || tracking.isError} onClick={() => remove.mutate(row.id)}>移除</button></td></tr>)}</tbody></table></div><Pagination page={trackPage} pages={Math.ceil(records.length / PAGE_SIZE)} onPage={setTrackingPage} /><p className="mt-4 text-xs leading-relaxed text-secondary">“未到期”表示仍在等待完整交易日；“数据缺失”表示观察窗口有缺口。复权异常或基准变化时暂停计算，空缺不按 0 收益处理。</p></>}
        </section>}
        {tab === 'rules' && (config.isLoading ? <LoadingRows /> : config.data ? <RulesPanel config={config.data} onSaved={next => { client.setQueryData(QK.huichunConfig, next); void client.invalidateQueries({ queryKey: QK.huichunSnapshot }) }} reload={() => config.refetch().then(result => result.data)} /> : null)}
      </div>
    </div>
    <StockPreviewDialog symbol={stock?.symbol ?? null} name={stock?.name} onClose={() => setStock(null)} navList={navList.some(row => row.symbol === stock?.symbol) ? navList : undefined} onNavigate={(symbol, name) => setStock({ symbol, name: name ?? symbol })} />
  </div>
}
