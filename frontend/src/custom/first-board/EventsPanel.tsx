import type { Dispatch, SetStateAction } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import type { FirstBoardEvent, FirstBoardEventFilters } from '@/lib/api'
import { FirstAlertRankings } from './FirstAlertRankings'
import { PriorityCell } from './PriorityCell'
import { TechnicalEvidence } from './TechnicalEvidence'
import { beijingDay, buttonClass, ErrorNotice, EventBadge, eventLabels, inputClass, LoadingRows, money, panelClass, PatternBadge, patternLabels, stamp } from './shared'

type StockList = { symbol: string; name: string }[]

function EventItem({ event, canSell, onOpen, onSell }: {
  event: FirstBoardEvent; canSell: boolean; onOpen: (symbol: string, name: string) => void; onSell: (event: FirstBoardEvent) => void
}) {
  return <li className="space-y-2 py-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-3"><button className="text-sm font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpen(event.symbol, event.name)}>{event.name} <span className="font-normal text-secondary">{event.symbol}</span></button><EventBadge type={event.event_type} /><PatternBadge pattern={event.pattern} label={event.pattern_label} /></div><span className="text-xs text-secondary">{stamp(event.ts)} · 版本 {event.revision}</span></div>
    <p className="text-sm leading-relaxed">{event.message}</p>
    {event.evidence.priority_score && <PriorityCell row={{ priority_score: event.evidence.priority_score }} current archived />}
    {event.pattern !== 'exit' && <TechnicalEvidence facts={event.evidence} pattern={event.pattern} day={event.date} historical />}
    <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-secondary"><span>信号价格 {money(event.price)} 元 · 行情时间 {stamp(event.quote_time)}</span>{event.event_type === 'exit_candidate' && <button className={buttonClass} disabled={!canSell || event.date !== beijingDay() || typeof event.evidence.qty !== 'number' || event.evidence.qty <= 0} onClick={() => onSell(event)}>模拟卖出</button>}</div>
    {event.blocked_reasons.length > 0 && <p className="text-xs text-secondary">{event.blocked_reasons.join('；')}</p>}
  </li>
}

export function EventsPanel({ events, day, onDay, filters, onFilters, poll, canSell, orderError, onOpen, onOpenList, onSell }: {
  events: UseQueryResult<{ events: FirstBoardEvent[]; day: string; total: number }>
  day: string; onDay: (day: string) => void
  filters: FirstBoardEventFilters; onFilters: Dispatch<SetStateAction<FirstBoardEventFilters>>
  poll: boolean; canSell: boolean; orderError: Error | null
  onOpen: (symbol: string, name: string) => void
  onOpenList: (symbol: string, name: string, list: StockList) => void
  onSell: (event: FirstBoardEvent) => void
}) {
  return <section className={`${panelClass} space-y-4`}>
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">信号记录 <span className="font-normal text-secondary">{events.data?.total ?? 0} 条</span></h2><div className="flex flex-wrap items-center gap-3 text-xs">
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">形态<select aria-label="信号形态" className={inputClass} value={filters.pattern ?? 'all'} onChange={event => onFilters(current => ({ ...current, pattern: event.target.value === 'all' ? undefined : event.target.value as FirstBoardEventFilters['pattern'] }))}><option value="all">全部形态</option>{Object.entries({ ...patternLabels, exit: '次日退出' }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">状态<select aria-label="信号状态" className={inputClass} value={filters.event_type ?? 'all'} onChange={event => onFilters(current => ({ ...current, event_type: event.target.value === 'all' ? undefined : event.target.value as FirstBoardEventFilters['event_type'] }))}><option value="all">全部状态</option>{Object.entries(eventLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="flex shrink-0 items-center gap-2 whitespace-nowrap">交易日期<input className={inputClass} type="date" max={beijingDay()} value={day} onChange={event => { if (event.target.value) onDay(event.target.value) }} /></label></div></div>
    <FirstAlertRankings key={day} day={day} poll={poll} onOpen={onOpenList} />
    <p className="text-xs leading-relaxed text-secondary">记录状态变化时的价格、规则版本与阻断原因。历史提示不代表当前仍可买入，模拟委托会在提交时重新核验行情。</p>
    <ErrorNotice error={events.error} retry={() => void events.refetch()} />
    <ErrorNotice error={orderError} />
    {events.isLoading ? <LoadingRows /> : events.data?.events.length
      ? <ul className="divide-y divide-border">{events.data.events.map(event => <EventItem key={event.id} event={event} canSell={canSell} onOpen={onOpen} onSell={onSell} />)}</ul>
      : <p className="py-5 text-sm text-secondary">{filters.pattern || filters.event_type ? '当前筛选条件下暂无信号，可切换形态或状态查看其他记录。' : '该日暂无首板信号。开启盯盘后，符合条件的状态变化会记录在这里。'}</p>}
  </section>
}
