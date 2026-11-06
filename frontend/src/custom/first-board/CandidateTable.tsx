import { useCallback, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, Star } from 'lucide-react'
import type { FirstBoardCandidate, FirstBoardPattern, FirstBoardState } from '@/lib/api'
import type { ColumnConfig } from '@/lib/list-columns'
import { markedFirst, type StockMarks, type useStockMarks } from '@/lib/useStockMarks'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { AttentionCell, attentionPriority, matchesAttention, type AttentionFilter } from './AttentionCell'
import { TechnicalEvidence } from './TechnicalEvidence'
import { PriorityCell, currentPriority, matchesPriority, priorityFilters, strongBoards, type PriorityFilter } from './PriorityCell'
import { buttonClass, ErrorNotice, inputClass, LoadingRows, money, panelClass, PatternBadge, patternLabels, pct, stamp, StateBadge, stateLabels } from './shared'

const candidateColumns: ColumnConfig[] = [
  ['symbol', '股票 / 行业'], ['pattern', '形态'], ['state', '状态'], ['price', '现价'], ['change_pct', '涨幅'],
  ['distance_to_limit_pct', '距涨停'], ['turnover_rate', '换手率'], ['priority_score', '首板优先分'], ['attention_context', '板块联动'], ['reasons', '入选与阻断原因'], ['quote_time', '报价时间'], ['actions', '模拟操作'],
].map(([id, label]) => ({ id, label, source: { type: 'builtin', key: id }, visible: true }))
const sortableColumns = new Set(['pattern', 'state', 'price', 'change_pct', 'distance_to_limit_pct', 'priority_score', 'attention_context'])
const scoreDescending = { key: 'priority_score', dir: 'desc' } as const
const changeDescending = { key: 'change_pct', dir: 'desc' } as const
const categoryOrder = { pattern: Object.keys(patternLabels), state: Object.keys(stateLabels) }
const categorySortHints: Record<string, string> = {
  pattern: `升序：${Object.values(patternLabels).join(' → ')}`,
  state: `升序：${Object.values(stateLabels).join(' → ')}`,
  attention_context: '按关注优先级排序；升序：未满足 → 单项满足 → 同板块双条件；待确认置底',
}

function candidateSortValue(row: FirstBoardCandidate, column: ColumnConfig) {
  if (column.id === 'attention_context') return attentionPriority(row.attention_context)
  if (column.id === 'pattern' || column.id === 'state') {
    // 分类沿用界面已有的定义顺序，未知分类与缺失数值一样置底。
    const index = categoryOrder[column.id].indexOf(row[column.id])
    return index < 0 ? null : index
  }
  const value = row[column.id as keyof FirstBoardCandidate]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** 筛选与排序留在页面级，股票预览的上下翻页沿用同一份可见顺序。 */
export function useCandidateView(all: FirstBoardCandidate[] | undefined, current: boolean, marks: StockMarks) {
  const [filter, setFilter] = useState<FirstBoardPattern | 'all'>('all')
  const [stateFilter, setStateFilter] = useState<FirstBoardState | 'all'>('all')
  const [onlyActionable, setOnlyActionable] = useState(false)
  const [attentionFilter, setAttentionFilter] = useState<AttentionFilter>('all')
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>('all')
  const [boardId, setBoardId] = useState('')
  const sortValue = useCallback((row: FirstBoardCandidate, column: ColumnConfig) => {
    if (column.id === 'priority_score') return currentPriority(row, current)
    return column.id === 'attention_context' && !current ? null : candidateSortValue(row, column)
  }, [current])
  const hasScores = all?.some(row => currentPriority(row, current) !== null)
  const { sort, toggle: toggleSort, sortRows } = useTableSort<FirstBoardCandidate>(sortValue, hasScores ? scoreDescending : changeDescending)
  const boardIndex = useMemo(() => {
    if (priorityFilter !== 'industry' && priorityFilter !== 'concept') return null
    const byRow = new Map<FirstBoardCandidate, Map<string, string>>()
    const choices = new Map<string, string>()
    for (const row of all ?? []) {
      // Stock rows may have separate patterns and evidence eligibility.
      const boards = strongBoards(row, priorityFilter, current)
      byRow.set(row, boards)
      for (const [id, name] of boards) choices.set(id, name)
    }
    return { byRow, choices: [...choices].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN')) }
  }, [priorityFilter, all, current])
  const rows = useMemo(() => {
    const inPriorityFilter = (row: FirstBoardCandidate) => {
      if (!boardIndex) return matchesPriority(row, priorityFilter, current)
      const boards = boardIndex.byRow.get(row)
      return !!boards && (boardId ? boards.has(boardId) : boards.size > 0)
    }
    const selected = (all ?? []).filter(row =>
      (filter === 'all' || row.pattern === filter) && (stateFilter === 'all' || row.state === stateFilter)
      && (!onlyActionable || row.can_buy) && matchesAttention(row.attention_context, attentionFilter, current)
      && inPriorityFilter(row))
    const sorted = sortRows(selected, candidateColumns)
    if (sort?.key === 'priority_score') {
      const tier = (row: FirstBoardCandidate) => currentPriority(row, current) === null ? 2 : row.can_buy ? 0 : 1
      sorted.sort((a, b) => tier(a) - tier(b))
    }
    return markedFirst(sorted, marks, 'focused')
  }, [all, filter, stateFilter, onlyActionable, attentionFilter, priorityFilter, boardIndex, boardId, current, sortRows, sort?.key, marks])
  return {
    rows, sort, toggleSort, boardIndex, filter, setFilter, stateFilter, setStateFilter, onlyActionable, setOnlyActionable,
    attentionFilter, setAttentionFilter, priorityFilter, setPriorityFilter, boardId, setBoardId,
  }
}
type CandidateView = ReturnType<typeof useCandidateView>

function CandidateFilters({ view }: { view: CandidateView }) {
  const { boardIndex, boardId } = view
  return <div className="flex flex-wrap items-center gap-4 text-xs">
    <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">形态<select className={inputClass} value={view.filter} onChange={event => view.setFilter(event.target.value as FirstBoardPattern | 'all')}><option value="all">全部形态</option>{Object.entries(patternLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">状态<select aria-label="候选状态" className={inputClass} value={view.stateFilter} onChange={event => view.setStateFilter(event.target.value as FirstBoardState | 'all')}><option value="all">全部状态</option>{Object.entries(stateLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">重点关注<select aria-label="重点关注" className={inputClass} value={view.attentionFilter} onChange={event => view.setAttentionFilter(event.target.value as AttentionFilter)}><option value="all">全部候选</option><option value="strong">强势板块</option><option value="follow">领涨带动</option><option value="both">同板块双条件</option></select></label>
    <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">评分筛选<select aria-label="评分筛选" className={inputClass} value={view.priorityFilter} onChange={event => { view.setPriorityFilter(event.target.value as PriorityFilter); view.setBoardId('') }}>{Object.entries(priorityFilters).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    {boardIndex && <label className="flex min-w-0 max-w-full items-center gap-2 whitespace-nowrap">板块<select aria-label="具体强势板块" className={cn(inputClass, 'max-w-[min(18rem,65vw)]')} value={boardId} onChange={event => view.setBoardId(event.target.value)}>
      <option value="">所有命中板块</option>
      {boardId && !boardIndex.choices.some(([id]) => id === boardId) && <option value={boardId}>原选板块当前无匹配</option>}
      {boardIndex.choices.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
    </select></label>}
    <label className="flex items-center gap-2"><input type="checkbox" checked={view.onlyActionable} onChange={event => view.setOnlyActionable(event.target.checked)} />只看可提示候选</label>
  </div>
}

function emptyMessage(view: CandidateView) {
  if (view.priorityFilter !== 'all') return '当前评分筛选下暂无候选。可选择全部候选查看缺失证据或最近盘中评分。'
  if (view.stateFilter !== 'all') return '当前筛选条件下暂无候选，可切换状态或调整其他筛选条件。'
  if (view.attentionFilter !== 'all') return '当前筛选下没有满足重点关注条件的候选。数据不足或过期的候选不计入重点筛选，可选择全部候选查看联动依据。'
  if (view.onlyActionable) return '当前没有通过全部确认条件的候选，可取消筛选查看阻断原因。'
  return '暂无匹配的首板候选。系统需要完成日线建立观察池，并等待有效盘中报价；没有候选时不会生成买入提示。'
}

function CandidateHeader({ view }: { view: CandidateView }) {
  const { sort, toggleSort } = view
  return <thead><tr className="text-secondary">{candidateColumns.map(column => {
    const sortable = sortableColumns.has(column.id)
    const active = sort?.key === column.id
    const Icon = active ? sort.dir === 'asc' ? ArrowUp : ArrowDown : ArrowUpDown
    const action = active ? sort.dir === 'asc' ? '当前升序，点击降序' : '当前降序，点击恢复默认顺序' : '点击升序排列'
    return <th key={column.id} scope="col" aria-sort={sortable ? active ? sort.dir === 'asc' ? 'ascending' : 'descending' : 'none' : undefined} className="sticky top-0 z-10 whitespace-nowrap bg-surface px-3 py-3 font-normal first:pl-0">
      {sortable ? <button type="button" aria-label={`按${column.id === 'attention_context' ? '板块联动关注优先级' : column.label}排序`} title={[action, categorySortHints[column.id]].filter(Boolean).join('；')} className={cn('inline-flex min-h-9 items-center gap-1 rounded hover:text-foreground focus-visible:outline focus-visible:outline-accent', active && 'font-medium text-foreground')} onClick={() => toggleSort(column.id)}>{column.label}<Icon className={cn('h-3 w-3', active && 'text-accent')} aria-hidden="true" /></button> : column.label}
    </th>
  })}</tr></thead>
}

function CandidateRow({ row, current, day, focused, canOrder, onFocus, onOpen, onBuy }: {
  row: FirstBoardCandidate; current: boolean; day: string; focused: boolean; canOrder: boolean
  onFocus: (symbol: string) => void; onOpen: (symbol: string, name: string) => void
  onBuy: (eventId: string, amount: number) => void
}) {
  const board = boardTag(row.symbol)
  return <tr className="border-t border-border">
    <td className="py-3 pr-3 align-top"><button className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpen(row.symbol, row.name)}>{row.name}{board && <span className={`ml-1 shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>{board.label}</span>}<span className="block font-normal text-secondary">{row.symbol}</span></button><span className="mt-1 block text-secondary">{row.sector || '行业未确认'}</span>
      <button type="button" aria-pressed={focused} aria-label={`${focused ? '取消关注' : '特别关注'} ${row.name} ${row.symbol}`} title={focused ? '取消关注' : '特别关注并优先显示'}
        className={cn('mt-2 inline-flex min-h-8 items-center gap-1 whitespace-nowrap rounded border px-2 text-xs focus-visible:outline focus-visible:outline-accent', focused ? 'border-accent/50 bg-accent/10 text-accent' : 'border-border text-secondary hover:text-foreground')}
        onClick={() => onFocus(row.symbol)}><Star className="h-3.5 w-3.5" fill={focused ? 'currentColor' : 'none'} aria-hidden="true" />{focused ? '取消关注' : '特别关注'}</button>
    </td>
    <td className="px-3 py-3 align-top"><PatternBadge pattern={row.pattern} label={row.pattern_label} /></td>
    <td className="px-3 py-3 align-top"><StateBadge state={row.state} /></td>
    <td className={cn('px-3 py-3 align-top tabular-nums', priceColorClass(row.price ? row.change_pct : null))}>{fmtPrice(row.price)}</td>
    <td className={cn('px-3 py-3 align-top tabular-nums', priceColorClass(row.change_pct))}>{fmtPct(row.change_pct)}</td>
    <td className="px-3 py-3 align-top tabular-nums">{pct(row.distance_to_limit_pct)}</td>
    <td className="px-3 py-3 align-top tabular-nums">{pct(row.turnover_rate, 1)}</td>
    <td className="px-3 py-3 align-top"><PriorityCell row={row} current={current} /></td>
    <td className="px-3 py-3 align-top"><AttentionCell context={row.attention_context} snapshotCurrent={current} /></td>
    <td className="min-w-56 max-w-80 px-3 py-3 align-top leading-relaxed">
      <p>{row.blocked_reasons.length ? row.blocked_reasons.join('；') : row.can_buy ? '达到实验提示条件，仍需确认可成交性。' : '继续观察'}</p>
      <TechnicalEvidence facts={row.evidence} pattern={row.pattern} day={day} historical={!current || row.priority_score?.status === 'stale'} />
      <details className="mt-2 text-secondary"><summary className="cursor-pointer">入选依据</summary><ul className="mt-1 space-y-1">{row.reasons.map(reason => <li key={reason}>{reason}</li>)}{row.sector_confirmation?.reason && <li>{row.sector_confirmation.reason}</li>}<li>形态参考日期：{String(row.evidence.reference_date ?? '—')}</li>{typeof row.evidence.breakout_price_raw === 'number' && <li>原价突破参考：{money(row.evidence.breakout_price_raw)} 元</li>}</ul></details>
    </td>
    <td className="whitespace-nowrap px-3 py-3 align-top tabular-nums">{stamp(row.quote_time)}</td>
    <td className="px-3 py-3 align-top"><button className={buttonClass} disabled={!canOrder || !row.can_buy || !row.event_id || !row.suggested_amount} onClick={() => row.event_id && row.suggested_amount && onBuy(row.event_id, row.suggested_amount)}>模拟买入</button><span className="mt-1 block text-secondary">{row.suggested_amount ? `${money(row.suggested_amount)} 元` : '需关联账户及可用额度'}</span></td>
  </tr>
}

export function CandidateTable({ view, loading, current, day, marks, canOrder, onOpen, onBuy }: {
  view: CandidateView; loading: boolean; current: boolean; day: string; marks: ReturnType<typeof useStockMarks>
  canOrder: boolean; onOpen: (symbol: string, name: string) => void; onBuy: (eventId: string, amount: number) => void
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const toggleFocus = (symbol: string) => {
    if (!marks.marks[symbol]?.focused && viewport.current) viewport.current.scrollTop = 0
    marks.toggle(symbol, 'focused')
  }
  const { rows } = view
  return <section className={panelClass}>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">今日候选 <span className="font-normal text-secondary">{rows.length} 条</span></h2><CandidateFilters view={view} /></div>
    <ErrorNotice error={marks.error} retry={marks.retry} />
    <div ref={viewport} role="region" aria-label="今日候选列表" tabIndex={0} className="h-[560px] max-h-[70vh] overflow-auto overscroll-contain scrollbar-gutter-stable focus-visible:outline focus-visible:outline-accent">
      {loading ? <LoadingRows /> : rows.length ? <table aria-label="首板实时候选" className="w-full min-w-[1380px] text-left text-xs">
        <CandidateHeader view={view} />
        <tbody>{rows.map(row => <CandidateRow key={`${row.symbol}-${row.pattern}`} row={row} current={current} day={day} focused={!!marks.marks[row.symbol]?.focused}
          canOrder={canOrder} onFocus={toggleFocus} onOpen={onOpen} onBuy={onBuy} />)}</tbody>
      </table> : <p className="py-6 text-sm leading-relaxed text-secondary">{emptyMessage(view)}</p>}
    </div>
  </section>
}
