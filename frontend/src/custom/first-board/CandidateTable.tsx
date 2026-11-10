import { useCallback, useId, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronDown, ChevronUp, SlidersHorizontal, Star } from 'lucide-react'
import type { FirstBoardCandidate, FirstBoardPattern, FirstBoardState } from '@/lib/api'
import type { ColumnConfig } from '@/lib/list-columns'
import { markedFirst, type StockMarks, type useStockMarks } from '@/lib/useStockMarks'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { AttentionCell, AttentionSummary, attentionPriority, matchesAttention, type AttentionFilter } from './AttentionCell'
import { TechnicalEvidence } from './TechnicalEvidence'
import { PriorityCell, PrioritySummary, currentPriority, matchesPriority, priorityFilters, strongBoards, type PriorityFilter } from './PriorityCell'
import { ErrorNotice, inputClass, LoadingRows, money, panelClass, PatternBadge, patternLabels, pct, roleLabels, stamp, StateBadge, stateLabels } from './shared'

const candidateColumns: ColumnConfig[] = [
  ['symbol', '股票 / 行业'], ['pattern', '形态'], ['state', '状态'], ['price', '现价'], ['change_pct', '涨幅'],
  ['distance_to_limit_pct', '距涨停'], ['priority_score', '首板优先分'], ['attention_context', '板块联动'], ['conclusion', '结论'], ['actions', '模拟操作'], ['details', '依据'],
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
const states = Object.keys(stateLabels) as FirstBoardState[]
const chipClass = 'inline-flex min-h-8 items-center gap-1 whitespace-nowrap rounded-btn border px-2.5 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'
const buyClass = 'inline-flex min-h-8 items-center justify-center rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'

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
  const { rows, stateCounts, stateTotal } = useMemo(() => {
    const inPriorityFilter = (row: FirstBoardCandidate) => {
      if (!boardIndex) return matchesPriority(row, priorityFilter, current)
      const boards = boardIndex.byRow.get(row)
      return !!boards && (boardId ? boards.has(boardId) : boards.size > 0)
    }
    // 状态分段的计数基于除状态外的其余筛选，切换状态时数字不跳动。
    const base = (all ?? []).filter(row =>
      (filter === 'all' || row.pattern === filter) && (!onlyActionable || row.can_buy)
      && matchesAttention(row.attention_context, attentionFilter, current) && inPriorityFilter(row))
    const counts = Object.fromEntries(states.map(state => [state, 0])) as Record<FirstBoardState, number>
    for (const row of base) if (row.state in counts) counts[row.state] += 1
    const selected = stateFilter === 'all' ? base : base.filter(row => row.state === stateFilter)
    const sorted = sortRows(selected, candidateColumns)
    if (sort?.key === 'priority_score') {
      const tier = (row: FirstBoardCandidate) => currentPriority(row, current) === null ? 2 : row.can_buy ? 0 : 1
      sorted.sort((a, b) => tier(a) - tier(b))
    }
    return { rows: markedFirst(sorted, marks, 'focused'), stateCounts: counts, stateTotal: base.length }
  }, [all, filter, stateFilter, onlyActionable, attentionFilter, priorityFilter, boardIndex, boardId, current, sortRows, sort?.key, marks])
  return {
    rows, stateCounts, stateTotal, sort, toggleSort, boardIndex, filter, setFilter, stateFilter, setStateFilter, onlyActionable, setOnlyActionable,
    attentionFilter, setAttentionFilter, priorityFilter, setPriorityFilter, boardId, setBoardId,
  }
}
type CandidateView = ReturnType<typeof useCandidateView>

function CandidateFilters({ view }: { view: CandidateView }) {
  const { boardIndex, boardId } = view
  const [moreOpen, setMoreOpen] = useState(false)
  const moreId = useId()
  const active = [view.filter !== 'all', view.attentionFilter !== 'all', view.priorityFilter !== 'all'].filter(Boolean).length
  const stateChoices: [FirstBoardState | 'all', string, number][] = [['all', '全部', view.stateTotal], ...states.map(state => [state, stateLabels[state], view.stateCounts[state]] as [FirstBoardState, string, number])]
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="候选状态" className="flex flex-wrap gap-1">
        {stateChoices.map(([value, label, count]) => {
          const pressed = view.stateFilter === value
          return <button key={value} type="button" aria-pressed={pressed} onClick={() => view.setStateFilter(value)}
            className={cn(chipClass, pressed ? 'border-accent/50 bg-accent/10 text-foreground' : 'border-border text-secondary hover:text-foreground', !count && !pressed && 'text-muted')}>
            {label}<span className="tabular-nums">{count}</span>
          </button>
        })}
      </div>
      <label className={cn(chipClass, 'cursor-pointer', view.onlyActionable ? 'border-accent/50 bg-accent/10 text-foreground' : 'border-border text-secondary hover:text-foreground')}>
        <input type="checkbox" className="accent-[hsl(var(--accent))]" checked={view.onlyActionable} onChange={event => view.setOnlyActionable(event.target.checked)} />只看可提示候选
      </label>
      <button type="button" aria-expanded={moreOpen} aria-controls={moreId} onClick={() => setMoreOpen(value => !value)}
        className={cn(chipClass, active ? 'border-accent/50 text-foreground' : 'border-border text-secondary hover:text-foreground')}>
        <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />更多筛选{active > 0 && <span className="tabular-nums">· {active}</span>}
      </button>
    </div>
    {/* 收起时保留挂载，筛选值与页面状态保持一致。 */}
    <div id={moreId} hidden={!moreOpen} className={cn('flex-wrap items-center gap-4 text-xs', moreOpen ? 'flex' : 'hidden')}>
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">形态<select className={inputClass} value={view.filter} onChange={event => view.setFilter(event.target.value as FirstBoardPattern | 'all')}><option value="all">全部形态</option>{Object.entries(patternLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">重点关注<select aria-label="重点关注" className={inputClass} value={view.attentionFilter} onChange={event => view.setAttentionFilter(event.target.value as AttentionFilter)}><option value="all">全部候选</option><option value="strong">强势板块</option><option value="follow">领涨带动</option><option value="both">同板块双条件</option></select></label>
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">评分筛选<select aria-label="评分筛选" className={inputClass} value={view.priorityFilter} onChange={event => { view.setPriorityFilter(event.target.value as PriorityFilter); view.setBoardId('') }}>{Object.entries(priorityFilters).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      {boardIndex && <label className="flex min-w-0 max-w-full items-center gap-2 whitespace-nowrap">板块<select aria-label="具体强势板块" className={cn(inputClass, 'max-w-[min(18rem,65vw)]')} value={boardId} onChange={event => view.setBoardId(event.target.value)}>
        <option value="">所有命中板块</option>
        {boardId && !boardIndex.choices.some(([id]) => id === boardId) && <option value={boardId}>原选板块当前无匹配</option>}
        {boardIndex.choices.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select></label>}
    </div>
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
    return <th key={column.id} scope="col" aria-sort={sortable ? active ? sort.dir === 'asc' ? 'ascending' : 'descending' : 'none' : undefined} className="sticky top-0 z-10 whitespace-nowrap bg-surface px-3 py-2.5 font-normal first:pl-3">
      {sortable ? <button type="button" aria-label={`按${column.id === 'attention_context' ? '板块联动关注优先级' : column.label}排序`} title={[action, categorySortHints[column.id]].filter(Boolean).join('；')} className={cn('inline-flex min-h-8 items-center gap-1 rounded hover:text-foreground focus-visible:outline focus-visible:outline-accent', active && 'font-medium text-foreground')} onClick={() => toggleSort(column.id)}>{column.label}<Icon className={cn('h-3 w-3', active && 'text-accent')} aria-hidden="true" /></button> : column.label}
    </th>
  })}</tr></thead>
}

/** 每行只给一句结论：可提示、个股自身的主要阻断原因，或仅受全局条件暂停。 */
function Conclusion({ row, current, stockReasons, globalOnly }: { row: FirstBoardCandidate; current: boolean; stockReasons: string[]; globalOnly: boolean }) {
  if (row.can_buy) {
    return current ? <p className="font-medium text-blue-700 dark:text-blue-300">达到提示条件，需确认可成交</p> : <p className="text-secondary">等待最新快照确认</p>
  }
  if (stockReasons.length) {
    return <div title={stockReasons.join('；')}>
      <p className="line-clamp-2">{stockReasons[0]}</p>
      {stockReasons.length > 1 && <p className="text-secondary">另有 {stockReasons.length - 1} 项，见依据</p>}
    </div>
  }
  return <p className="text-secondary">{globalOnly ? '受全局条件暂停' : '继续观察'}</p>
}

function CandidateEvidence({ row, current, day, stockReasons }: { row: FirstBoardCandidate; current: boolean; day: string; stockReasons: string[] }) {
  return <div className="grid gap-x-6 gap-y-4 text-xs leading-relaxed md:grid-cols-3">
    <section className="min-w-0 space-y-2">
      <h4 className="font-medium">入选与阻断</h4>
      {stockReasons.length ? <ul className="list-disc space-y-1 pl-4">{stockReasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
        : <p className="text-secondary">{row.can_buy ? '达到实验提示条件，仍需确认可成交性。' : '没有个股自身的阻断原因。'}</p>}
      <ul className="space-y-1 text-secondary">
        {row.reasons.map(reason => <li key={reason}>{reason}</li>)}
        {row.sector_confirmation?.reason && !stockReasons.includes(row.sector_confirmation.reason) && <li>{row.sector_confirmation.reason}</li>}
        <li>形态参考日期：{String(row.evidence.reference_date ?? '—')}</li>
        {typeof row.evidence.breakout_price_raw === 'number' && <li>原价突破参考：{money(row.evidence.breakout_price_raw)} 元</li>}
      </ul>
      <p className="tabular-nums text-secondary">换手率 {pct(row.turnover_rate, 1)} · 报价时间 {stamp(row.quote_time)}</p>
    </section>
    <section className="min-w-0 space-y-2">
      <h4 className="font-medium">评分与日线</h4>
      <PriorityCell row={row} current={current} />
      <TechnicalEvidence facts={row.evidence} pattern={row.pattern} day={day} historical={!current || row.priority_score?.status === 'stale'} />
    </section>
    <section className="min-w-0 space-y-2">
      <h4 className="font-medium">板块联动</h4>
      <AttentionCell context={row.attention_context} snapshotCurrent={current} />
    </section>
  </div>
}

/** 只标出已命中的角色；待确认和未命中的原因在结论与依据中说明。 */
export function RoleTags({ row }: { row: FirstBoardCandidate }) {
  const role = row.stock_role
  if (!role) return null
  const hits = (Object.keys(roleLabels) as (keyof typeof roleLabels)[]).filter(name => role[name] === true)
  return <>{hits.map(name => {
    const boards = role.matches.filter(match => match.roles.includes(name)).map(match => match.name)
    return <span key={name} title={`${roleLabels[name]}：${boards.join('、')}`} className="ml-1 inline-flex shrink-0 items-center rounded border border-accent/40 px-1 text-[10px] leading-4 text-accent">{roleLabels[name]}</span>
  })}</>
}

function CandidateRow({ row, current, day, focused, canOrder, globalReasons, expanded, onToggle, onFocus, onOpen, onBuy }: {
  row: FirstBoardCandidate; current: boolean; day: string; focused: boolean; canOrder: boolean; globalReasons: Set<string>
  expanded: boolean; onToggle: () => void
  onFocus: (symbol: string) => void; onOpen: (symbol: string, name: string) => void
  onBuy: (eventId: string, amount: number) => void
}) {
  const board = boardTag(row.symbol)
  const evidenceId = useId()
  const stockReasons = row.blocked_reasons.filter(reason => !globalReasons.has(reason))
  const globalOnly = row.blocked_reasons.some(reason => globalReasons.has(reason))
  const highlight = current && row.can_buy
  return <>
    <tr data-candidate-row className={cn('border-t border-border align-top transition-colors hover:bg-elevated/40', highlight && 'bg-accent/5')}>
      <td className={cn('py-2.5 pl-3 pr-3', highlight && 'shadow-[inset_3px_0_0_0_hsl(var(--accent))]')}>
        <div className="flex items-start gap-1">
          <button className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpen(row.symbol, row.name)}>{row.name}{board && <span className={`ml-1 shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>{board.label}</span>}</button>
          <RoleTags row={row} />
          <button type="button" aria-pressed={focused} aria-label={`${focused ? '取消关注' : '特别关注'} ${row.name} ${row.symbol}`} title={focused ? '取消关注' : '特别关注并优先显示'}
            className={cn('-my-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded focus-visible:outline focus-visible:outline-accent', focused ? 'text-accent' : 'text-muted hover:text-foreground')}
            onClick={() => onFocus(row.symbol)}><Star className="h-3.5 w-3.5" fill={focused ? 'currentColor' : 'none'} aria-hidden="true" /></button>
        </div>
        <span className="block max-w-40 truncate text-secondary" title={`${row.symbol} · ${row.sector || '行业未确认'}`}>{row.symbol} · {row.sector || '行业未确认'}</span>
      </td>
      <td className="px-3 py-2.5"><PatternBadge pattern={row.pattern} label={row.pattern_label} /></td>
      <td className="px-3 py-2.5"><StateBadge state={row.state} /></td>
      <td className={cn('px-3 py-2.5 tabular-nums', priceColorClass(row.price ? row.change_pct : null))}>{fmtPrice(row.price)}</td>
      <td className={cn('px-3 py-2.5 tabular-nums', priceColorClass(row.change_pct))}>{fmtPct(row.change_pct)}</td>
      <td className="px-3 py-2.5 tabular-nums">{pct(row.distance_to_limit_pct)}</td>
      <td className="min-w-40 px-3 py-2.5"><PrioritySummary row={row} current={current} /></td>
      <td className="px-3 py-2.5"><AttentionSummary context={row.attention_context} snapshotCurrent={current} /></td>
      <td className="min-w-40 max-w-52 px-3 py-2.5 leading-snug"><Conclusion row={row} current={current} stockReasons={stockReasons} globalOnly={globalOnly} /></td>
      <td className="whitespace-nowrap px-3 py-2.5">{row.can_buy ? <>
        <button className={buyClass} disabled={!canOrder || !row.event_id || !row.suggested_amount} onClick={() => row.event_id && row.suggested_amount && onBuy(row.event_id, row.suggested_amount)}>模拟买入</button>
        <span className="mt-1 block text-secondary">{row.suggested_amount ? `${money(row.suggested_amount)} 元` : '需关联账户及可用额度'}</span>
      </> : <span className="text-muted">—</span>}</td>
      <td className="px-2 py-2">
        <button type="button" aria-expanded={expanded} aria-controls={expanded ? evidenceId : undefined} aria-label={`${expanded ? '收起' : '展开'}依据 ${row.name} ${row.pattern_label}`}
          className="inline-flex h-7 w-7 items-center justify-center rounded text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-accent" onClick={onToggle}>
          {expanded ? <ChevronUp className="h-4 w-4" aria-hidden="true" /> : <ChevronDown className="h-4 w-4" aria-hidden="true" />}
        </button>
      </td>
    </tr>
    {expanded && <tr data-evidence-row id={evidenceId}>
      <td colSpan={candidateColumns.length} className="border-t border-dashed border-border bg-elevated/30 px-3 py-3"><CandidateEvidence row={row} current={current} day={day} stockReasons={stockReasons} /></td>
    </tr>}
  </>
}

export function CandidateTable({ view, loading, current, day, marks, globalReasons, roleHidden = 0, canOrder, onOpen, onBuy }: {
  view: CandidateView; loading: boolean; current: boolean; day: string; marks: ReturnType<typeof useStockMarks>
  globalReasons: string[]; roleHidden?: number
  canOrder: boolean; onOpen: (symbol: string, name: string) => void; onBuy: (eventId: string, amount: number) => void
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const globalSet = useMemo(() => new Set(globalReasons), [globalReasons])
  const toggleFocus = (symbol: string) => {
    if (!marks.marks[symbol]?.focused && viewport.current) viewport.current.scrollTop = 0
    marks.toggle(symbol, 'focused')
  }
  const toggleRow = (key: string) => setExpanded(current => {
    const next = new Set(current)
    if (!next.delete(key)) next.add(key)
    return next
  })
  const { rows } = view
  return <section className={panelClass}>
    <div className="mb-3 flex flex-wrap items-start justify-between gap-3"><h2 className="pt-1.5 text-sm font-semibold">今日候选 <span className="font-normal text-secondary">{rows.length} 条{roleHidden > 0 && ` · 非所选角色未列出 ${roleHidden} 只`}</span></h2><CandidateFilters view={view} /></div>
    <ErrorNotice error={marks.error} retry={marks.retry} />
    <div ref={viewport} role="region" aria-label="今日候选列表" tabIndex={0} className="h-[560px] max-h-[70vh] overflow-auto overscroll-contain scrollbar-gutter-stable focus-visible:outline focus-visible:outline-accent">
      {loading ? <LoadingRows /> : rows.length ? <table aria-label="首板实时候选" className="w-full min-w-[1040px] text-left text-xs">
        <CandidateHeader view={view} />
        <tbody>{rows.map(row => {
          const key = `${row.symbol}-${row.pattern}`
          return <CandidateRow key={key} row={row} current={current} day={day} focused={!!marks.marks[row.symbol]?.focused}
            canOrder={canOrder} globalReasons={globalSet} expanded={expanded.has(key)} onToggle={() => toggleRow(key)}
            onFocus={toggleFocus} onOpen={onOpen} onBuy={onBuy} />
        })}</tbody>
      </table> : <p className="py-6 text-sm leading-relaxed text-secondary">{emptyMessage(view)}</p>}
    </div>
  </section>
}
