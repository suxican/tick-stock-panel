import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { api, type FirstBoardOpportunityRow, type FirstBoardPaperStatus } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { buttonClass, ErrorNotice, EventBadge, eventLabels, LoadingRows, PatternBadge } from './shared'

const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
const chipClass = 'inline-flex min-h-8 items-center gap-1.5 rounded-btn border px-2.5 py-1 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'

const closeLabels = { sealed: '收盘封板', not_sealed: '收盘未封', unverified: '待核验' }
const paperLabels: Record<FirstBoardPaperStatus, string> = {
  no_account: '未关联账户', no_signal: '—', not_ordered: '未下单', pending: '待成交', cancelled: '已取消', expired: '已过期',
  open: '已成交 · 持仓中', partially_closed: '部分平仓', closed: '已平仓', ambiguous: '来源不唯一', evidence_missing: '待核验',
}

function VerifiedCells({ row }: { row: FirstBoardOpportunityRow }) {
  const close = row.close
  const paper = row.paper
  const sealed = close?.status === 'sealed'
  return <>
    <td className="whitespace-nowrap px-3 py-2.5" title={close?.reason ?? undefined}>
      <span className={cn(close?.status === 'unverified' || !close ? 'text-muted' : sealed ? 'text-foreground' : 'text-secondary')}>{close ? closeLabels[close.status] : '待核验'}</span>
    </td>
    <td className="whitespace-nowrap px-3 py-2.5" title={paper?.reason ?? undefined}>
      {paper ? paperLabels[paper.status] : '—'}
      {paper?.status === 'closed' && paper.net_return != null && <span className="ml-1 tabular-nums text-secondary">{paper.net_return >= 0 ? '+' : ''}{(paper.net_return * 100).toFixed(2)}%</span>}
    </td>
  </>
}

/** 北京时间 HH:MM:SS；缺失或无法解析时显示 “—”，不让一条坏数据拖垮整页。 */
function time(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? clock.format(date) : '—'
}

function Timeline({ row, id }: { row: FirstBoardOpportunityRow; id: string }) {
  const steps: [string, string | null][] = [['板前提示', row.first_alert?.at ?? null], ['首次触板', row.first_touch?.at ?? null], ['开板', row.broken], ['回封', row.resealed]]
  return <div id={id} data-opportunity-timeline className="space-y-2 text-xs">
    <ol className="flex flex-wrap gap-x-8 gap-y-2">
      {steps.map(([label, value]) => <li key={label}><span className="text-secondary">{label}</span><span className="ml-2 tabular-nums">{time(value)}</span></li>)}
    </ol>
    {row.sealed_unalerted && <p className="leading-relaxed text-secondary">
      最早记录 <span className="tabular-nums text-foreground">{time(row.first_record.at)}</span> · {eventLabels[row.first_record.event_type as keyof typeof eventLabels] ?? '状态变化'}
      {row.first_record.blocked_reasons.length > 0 && `：${row.first_record.blocked_reasons.join('；')}`}
    </p>}
    <p className="text-secondary">原始事件 {row.event_count} 条，可切换到“原始事件”查看。</p>
  </div>
}

function Row({ row, open, verified, onToggle, onOpen }: { row: FirstBoardOpportunityRow; open: boolean; verified: boolean; onToggle: () => void; onOpen: (symbol: string, name: string) => void }) {
  const id = `opportunity-${row.symbol}`
  return <>
    <tr data-opportunity-row className="border-t border-border align-top">
      <td className="px-3 py-2.5">
        <button type="button" className="text-left text-sm font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpen(row.symbol, row.name)}>{row.name}</button>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-secondary"><span>{row.symbol}</span>
          {row.patterns.map(item => <PatternBadge key={item.pattern} pattern={item.pattern} label={item.pattern_label} />)}
          {row.sealed_unalerted && <span className="rounded border border-border px-1.5 py-0.5 text-[11px]">封板未提示</span>}</div>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{time(row.first_alert?.at)}</td>
      <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{time(row.first_touch?.at)}</td>
      <td className="whitespace-nowrap px-3 py-2.5"><EventBadge type={row.last_state.event_type} /><span className="ml-2 tabular-nums text-secondary">{time(row.last_state.at)}</span></td>
      {verified && <VerifiedCells row={row} />}
      <td className="px-2 py-2">
        <button type="button" aria-expanded={open} aria-controls={open ? id : undefined} aria-label={`${open ? '收起' : '展开'} ${row.name} 时间线`}
          className="inline-flex h-7 w-7 items-center justify-center rounded text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-accent" onClick={onToggle}>
          {open ? <ChevronUp className="h-4 w-4" aria-hidden="true" /> : <ChevronDown className="h-4 w-4" aria-hidden="true" />}
        </button>
      </td>
    </tr>
    {open && <tr className="bg-elevated/30"><td colSpan={verified ? 7 : 5} className="px-3 py-3"><Timeline row={row} id={id} /></td></tr>}
  </>
}

/** 盘后复盘：按股票聚合的机会记录。只展示事件档案里的时间点；收盘结果与模拟成交核验不在这里。 */
export function OpportunityList({ day, poll, onOpen }: { day: string; poll: boolean; onOpen: (symbol: string, name: string) => void }) {
  const [verify, setVerify] = useState(false)
  const base = useQuery({ queryKey: QK.firstBoardOpportunities(day, false), queryFn: () => api.firstBoardOpportunities(day, false), refetchInterval: poll ? 5000 : false })
  // 核验只在按下按钮后请求, 不进入轮询; 失败时继续显示未核验的机会记录并可重试。
  const checked = useQuery({ queryKey: QK.firstBoardOpportunities(day, true), queryFn: () => api.firstBoardOpportunities(day, true), enabled: verify, retry: false })
  const data = checked.data ?? base.data
  const verified = !!checked.data?.verified
  const [onlyUnalerted, setOnlyUnalerted] = useState(false)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const rows = (data?.rows ?? []).filter(row => !onlyUnalerted || row.sealed_unalerted)
  const toggle = (symbol: string) => setExpanded(current => { const next = new Set(current); if (!next.delete(symbol)) next.add(symbol); return next })
  const summary = data?.summary
  const cells: [string, number | undefined, string?][] = [['记录股票', summary?.stock_count], ['独立买入提示', summary?.alerted_count], ['有触板记录', summary?.touched_count], ['封板未提示', summary?.sealed_unalerted_count]]
  if (verified) cells.push(['收盘封板', summary?.close_sealed_count, `已核日线 ${summary?.close_checked_count ?? 0} / ${summary?.stock_count ?? 0}`],
    ['已核验模拟成交', summary?.paper_filled_count, `已核提示股 ${summary?.paper_checked_count ?? 0} / ${summary?.alerted_count ?? 0}`])
  return <section aria-label="机会记录" className="space-y-3">
    <h3 className="text-sm font-semibold">机会记录 <span className="font-normal text-secondary">{summary?.stock_count ?? 0} 只{data && ` · 版本 ${data.revision}`}</span></h3>
    <ErrorNotice error={base.error} retry={() => void base.refetch()} />
    <ErrorNotice error={checked.error} retry={() => void checked.refetch()} />
    {base.isLoading ? <LoadingRows /> : data && <>
      <div data-opportunity-summary className="grid grid-cols-2 gap-3 rounded-card border border-border p-3 sm:grid-cols-4">
        {cells.map(([label, count, note]) => <div key={label}><p className="text-xs text-secondary">{label}</p><p className="mt-1 text-lg font-semibold tabular-nums">{count ?? 0}<span className="ml-1 text-xs font-normal text-secondary">只</span></p>{note && <p className="mt-0.5 text-[11px] text-secondary">{note}</p>}</div>)}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
      <div role="group" aria-label="机会记录筛选" className="flex flex-wrap gap-1">
        {([['全部', false, summary?.stock_count ?? 0], ['封板未提示', true, summary?.sealed_unalerted_count ?? 0]] as const).map(([label, value, count]) =>
          <button key={label} type="button" aria-pressed={onlyUnalerted === value} onClick={() => setOnlyUnalerted(value)}
            className={cn(chipClass, onlyUnalerted === value ? 'border-accent/50 bg-accent/10 text-foreground' : 'border-border text-secondary hover:text-foreground')}>{label}<span className="tabular-nums">{count}</span></button>)}
      </div>
      <button type="button" className={buttonClass} disabled={checked.isFetching} onClick={() => { if (verify) void checked.refetch(); else setVerify(true) }}>{checked.isFetching ? '核验中…' : verified ? '重新核验' : '核验收盘与成交'}</button>
      </div>
      {rows.length ? <div className="overflow-x-auto"><table aria-label="首板机会记录" className="w-full min-w-[640px] text-left text-xs">
        <thead className="text-secondary"><tr><th className="px-3 py-2 font-normal">股票 / 形态</th><th className="px-3 py-2 font-normal">首次提示</th><th className="px-3 py-2 font-normal">首次触板</th><th className="px-3 py-2 font-normal">最后观察</th>{verified && <><th className="px-3 py-2 font-normal">收盘结果</th><th className="px-3 py-2 font-normal">模拟成交</th></>}<th className="w-10" /></tr></thead>
        <tbody>{rows.map(row => <Row key={row.symbol} row={row} open={expanded.has(row.symbol)} verified={verified} onToggle={() => toggle(row.symbol)} onOpen={onOpen} />)}</tbody>
      </table></div> : <p className="py-4 text-sm text-secondary">{onlyUnalerted ? '当日没有封板未提示的股票。' : '该日暂无机会记录。开启盯盘后，符合条件的股票会按只汇总在这里。'}</p>}
      <p className="text-xs leading-relaxed text-secondary">触板指涨停价观察，不代表封单或可成交；最后观察是盘中最后一次状态，不是收盘结果。收盘结果与模拟成交需点“核验收盘与成交”，按当日日线和关联模拟账户核对，证据不足显示待核验。</p>
    </>}
  </section>
}
