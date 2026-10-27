import { useId, useMemo, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import type { FirstBoardFeedback, FirstBoardFeedbackMember, FirstBoardFeedbackSector } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { buttonClass, pct, stamp } from './shared'

type Scope = 'all' | 'main' | 'growth'
type StockLink = { symbol: string; name: string }
const scopes: { id: Scope; label: string }[] = [{ id: 'all', label: '全部' }, { id: 'main', label: '主板' }, { id: 'growth', label: '双创' }]
const statusLabels = { waiting: '正在准备', ready: '行情可用', partial: '部分数据待确认', unavailable: '暂不可用', stale: '行情已过期' }
const memberLabels = { valid: '行情有效', missing: '缺少行情', stale: '行情已过期', invalid: '数据待确认' }
const tableClass = 'w-full text-left text-xs'
const cellClass = 'whitespace-nowrap px-3 py-2.5 text-right tabular-nums'
const headClass = 'whitespace-nowrap px-3 pb-3 text-right font-normal text-secondary'

function Metric({ label, children, hint, value, muted = false }: { label: string; children: ReactNode; hint?: string; value?: number | null; muted?: boolean }) {
  return <div className="min-w-0">
    <dt className="text-xs text-secondary">{label}</dt>
    <dd className={cn('mt-1 text-lg font-medium tabular-nums', value !== undefined && !muted ? priceColorClass(value) : 'text-foreground')}>{children}</dd>
    {hint && <dd className="mt-0.5 text-xs text-secondary">{hint}</dd>}
  </div>
}

function SectorTable({ rows, muted }: { rows: FirstBoardFeedbackSector[]; muted: boolean }) {
  return <div className="overflow-x-auto">
    <table className={cn(tableClass, 'min-w-[740px]')} aria-label="昨日首板行业与概念对照">
      <thead><tr><th scope="col" className="pb-3 pr-3 font-normal text-secondary">行业／概念</th><th scope="col" className={headClass}>昨日首板</th><th scope="col" className={headClass}>首板股均涨幅</th><th scope="col" className={headClass}>当前二板</th><th scope="col" className={headClass}>板块成分均涨幅</th><th scope="col" className={headClass}>板块上涨占比</th><th scope="col" className={headClass}>板块行情覆盖</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.id} className="border-t border-border">
        <td className="py-2.5 pr-3"><span className="block font-medium">{row.name}</span><span className="text-secondary">{row.kind === 'industry' ? '行业' : '概念'}{row.status !== 'ready' ? ' · 数据不足' : ''}</span>{row.reason && <p className="mt-1 max-w-64 leading-relaxed text-secondary">{row.reason}</p>}</td>
        <td className={cellClass}>{row.sample_count}<span className="block text-secondary">有效 {row.quote_count}</span></td>
        <td className={cn(cellClass, !muted && priceColorClass(row.mean_change_pct))}>{fmtPct(row.mean_change_pct)}</td>
        <td className={cellClass}>{row.limit_known_count ? `${row.limit_known_count < row.sample_count ? '已确认 ' : ''}${row.limit_up_count}` : '—'}{row.limit_known_count < row.sample_count && <span className="block text-secondary">待确认 {row.sample_count - row.limit_known_count}</span>}</td>
        <td className={cn(cellClass, !muted && row.status === 'ready' && priceColorClass(row.sector_mean_change_pct))}>{fmtPct(row.sector_mean_change_pct)}</td>
        <td className={cellClass}>{pct(row.up_ratio)}</td>
        <td className={cellClass}>{row.member_quote_count} / {row.member_count}<span className="block text-secondary">{pct(row.coverage)}</span></td>
      </tr>)}</tbody>
    </table>
  </div>
}

function MemberTable({ rows, muted, onOpenStock }: { rows: FirstBoardFeedbackMember[]; muted: boolean; onOpenStock: (symbol: string, name: string, navList: StockLink[]) => void }) {
  const navList = rows.map(({ symbol, name }) => ({ symbol, name }))
  return <div className="max-h-96 overflow-auto">
    <table className={cn(tableClass, 'min-w-[760px]')} aria-label="昨日首板个股表现">
      <thead className="sticky top-0 bg-surface"><tr><th scope="col" className="py-3 pr-3 font-normal text-secondary">股票</th><th scope="col" className={headClass}>现价</th><th scope="col" className={headClass}>涨幅</th><th scope="col" className={headClass}>开盘溢价</th><th scope="col" className={headClass}>开盘后涨幅</th><th scope="col" className={headClass}>状态</th><th scope="col" className={headClass}>报价时间</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.symbol} className="border-t border-border">
        <td className="py-2.5 pr-3"><button type="button" className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpenStock(row.symbol, row.name, navList)}>{row.name}<span className="block font-normal text-secondary">{row.symbol}</span></button></td>
        <td className={cn(cellClass, !muted && row.status === 'valid' && priceColorClass(row.price == null ? null : row.change_pct))}>{fmtPrice(row.price)}</td>
        <td className={cn(cellClass, !muted && row.status === 'valid' && priceColorClass(row.change_pct))}>{fmtPct(row.change_pct)}</td>
        <td className={cn(cellClass, !muted && row.status === 'valid' && priceColorClass(row.open_premium))}>{fmtPct(row.open_premium)}</td>
        <td className={cn(cellClass, !muted && row.status === 'valid' && priceColorClass(row.open_change))}>{fmtPct(row.open_change)}</td>
        <td className="max-w-64 px-3 py-2.5 text-right"><span>{row.status !== 'valid' ? memberLabels[row.status] : row.at_limit === true ? '当前二板' : row.at_limit === false ? '当前未涨停' : '涨停状态待确认'}</span>{row.reason && <p className="mt-1 leading-relaxed text-secondary">{row.reason}</p>}</td>
        <td className={cn(cellClass, 'text-secondary')}>{stamp(row.quote_time)}</td>
      </tr>)}</tbody>
    </table>
  </div>
}

export function YesterdayFeedback({ data, refreshFailed = false, onOpenStock, children }: {
  data?: FirstBoardFeedback
  refreshFailed?: boolean
  onOpenStock: (symbol: string, name: string, navList: StockLink[]) => void
  children: ReactNode
}) {
  const [scope, setScope] = useState<Scope>('all')
  const [expanded, setExpanded] = useState(false)
  const [sectorKind, setSectorKind] = useState<'industry' | 'concept'>('industry')
  const detailId = useId()
  const titleId = useId()
  const summary = data?.groups[scope]
  const muted = refreshFailed || data?.status === 'stale'
  const available = !!data && data.status !== 'waiting' && data.status !== 'unavailable'
  const members = useMemo(() => (data?.rows ?? []).filter(row => scope === 'all' || row.board === scope).toSorted((a, b) => (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity)), [data?.rows, scope])
  const sectors = useMemo(() => (data?.sectors ?? []).filter(row => row.kind === sectorKind), [data?.sectors, sectorKind])
  const unknownLimits = summary ? Math.max(0, summary.sample_count - summary.limit_known_count) : 0

  return <div className="grid min-w-0 gap-5 xl:grid-cols-2" data-yesterday-feedback>
    <div className="min-w-0 space-y-3">{children}</div>
    <section aria-labelledby={titleId} className="min-w-0 border-t border-border pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={titleId} className="text-sm font-semibold">昨日首板反馈</h3>
        <div className="flex gap-1" role="group" aria-label="昨日首板市场范围">{scopes.map(item => <button type="button" key={item.id} aria-pressed={scope === item.id} disabled={!available} className={cn(buttonClass, 'min-h-8 px-2.5', scope === item.id && 'border-accent/50 text-foreground')} onClick={() => setScope(item.id)}>{item.label}</button>)}</div>
      </div>
      {data && <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-secondary"><span>样本 {data.sample_date ?? '待确认'}</span><span>表现 {data.as_of ?? '待确认'}</span>{summary && <span>行情 {summary.quote_count} / {summary.sample_count}</span>}</p>}
      {data?.status === 'waiting' ? <div role="status" aria-label="正在加载昨日首板反馈" className="mt-4 grid grid-cols-3 gap-4">{Array.from({ length: 6 }, (_, index) => <div key={index} className="h-12 rounded bg-elevated motion-safe:animate-pulse" />)}<p className="col-span-3 text-xs text-secondary">{data.message || '正在建立昨日首板名单'}</p></div>
        : !available ? <p className="py-5 text-sm leading-relaxed text-secondary">{data?.message || '昨日首板反馈暂未生成。可刷新后重试。'}</p>
        : !summary?.sample_count ? <p className="py-5 text-sm leading-relaxed text-secondary">该范围没有可确认的昨日首板。缺少前日行情的股票不会被当作首板。</p>
        : <>
          <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-3">
            <Metric label="平均溢价" value={summary.mean_change_pct} muted={muted}>{fmtPct(summary.mean_change_pct)}</Metric>
            <Metric label="中位数" value={summary.median_change_pct} muted={muted}>{fmtPct(summary.median_change_pct)}</Metric>
            <Metric label="上涨占比" hint={`${summary.up_count} / ${summary.quote_count} 只有效行情`}>{summary.up_ratio == null ? '—' : `${(summary.up_ratio * 100).toFixed(1)}%`}</Metric>
            <Metric label="当前二板" hint={summary.limit_up_ratio == null ? '比例待确认' : `占昨日样本 ${(summary.limit_up_ratio * 100).toFixed(1)}%`}>{summary.limit_up_count}<span className="text-sm font-normal text-secondary"> / {summary.sample_count}</span></Metric>
            <Metric label="开盘后涨幅" value={summary.mean_open_change} muted={muted} hint={`开盘数据 ${summary.open_count} / ${summary.sample_count}`}>{fmtPct(summary.mean_open_change)}</Metric>
            <Metric label="跌幅 ≥ 5%" hint={summary.loss_ratio == null ? '比例待确认' : `占有效行情 ${(summary.loss_ratio * 100).toFixed(1)}%`}>{summary.loss_count}<span className="text-sm font-normal text-secondary"> / {summary.sample_count}</span></Metric>
          </dl>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-secondary">
            <span>{data.quote_mode === 'live' ? '盘中行情' : '最近行情'} · 统计更新 {stamp(data.observed_at)}</span>
            <button type="button" className={buttonClass} aria-expanded={expanded} aria-controls={detailId} onClick={() => setExpanded(value => !value)}>{expanded ? '收起明细' : '查看明细'}{expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}</button>
          </div>
          {summary.small_sample && <p className="mt-2 text-xs text-secondary">样本少于 10 只，比例容易受个别股票影响。</p>}
          {!!unknownLimits && <p className="mt-2 text-xs text-secondary">涨停状态待确认 {unknownLimits} 只，二板数量为已确认值。</p>}
        </>}
      {refreshFailed && <p role="status" className="mt-2 text-xs text-secondary">刷新失败，保留上次快照。请使用页面刷新重试。</p>}
      {data && available && data.message && <p className="mt-2 text-xs leading-relaxed text-secondary">{refreshFailed ? '保留快照' : statusLabels[data.status]} · {data.message}</p>}
    </section>
    {expanded && available && summary && <div id={detailId} className="min-w-0 space-y-5 border-t border-border pt-4 xl:col-span-2">
      <div className="space-y-2 text-xs leading-relaxed text-secondary">
        <p>固定样本：上一交易日收盘涨停，前一交易日未收盘涨停的沪深非 ST 股票；不受今日候选筛选影响。溢价为价格表现，不等于实际成交收益。</p>
        <p>平均开盘溢价 <span className={cn('tabular-nums', !muted && priceColorClass(summary.mean_open_premium))}>{fmtPct(summary.mean_open_premium)}</span> · 有效开盘数据 {summary.open_count} / {summary.sample_count}；双创包含创业板、科创板。</p>
        {!!data.history_excluded_count && <p>{data.history_excluded_count} 只股票缺少首板识别所需的历史或涨停依据，未纳入确认样本。</p>}
        {data.quote_mode === 'snapshot' && <p>行情时点以个股报价时间为准；统计更新不代表已确认收盘。</p>}
      </div>
      <div className="space-y-3"><h4 className="text-sm font-medium">个股表现 <span className="font-normal text-secondary">{scopes.find(item => item.id === scope)?.label} · {members.length} 只</span></h4>{members.length ? <MemberTable rows={members} muted={muted} onOpenStock={onOpenStock} /> : <p className="text-xs text-secondary">该范围暂无可展示的个股明细。</p>}</div>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="text-sm font-medium">关联板块对照</h4><div className="flex gap-1" role="group" aria-label="关联板块分类">{(['industry', 'concept'] as const).map(kind => <button key={kind} type="button" aria-pressed={sectorKind === kind} className={cn(buttonClass, sectorKind === kind && 'border-accent/50 text-foreground')} onClick={() => setSectorKind(kind)}>{kind === 'industry' ? '行业' : '概念'}</button>)}</div></div>
        <p className="text-xs leading-relaxed text-secondary">行业／概念对照固定为沪深非 ST 全范围，不随上方市场范围切换。{data.membership_note || '按当前板块成分计算等权涨幅，非官方板块指数；概念可重复归属，不跨概念加总。'}</p>
        {sectors.length ? <SectorTable rows={sectors} muted={muted} /> : <p className="py-3 text-xs leading-relaxed text-secondary">暂无满足条件的{sectorKind === 'industry' ? '行业' : '概念'}对照。需要可核验的成分和行情覆盖，且至少包含 2 只昨日首板。</p>}
      </div>
    </div>}
  </div>
}
