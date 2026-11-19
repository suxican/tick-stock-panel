import { useId, useMemo, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp, Info } from 'lucide-react'
import type { FirstBoardFeedback, FirstBoardFeedbackMember, FirstBoardFeedbackSector } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum, fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { storage } from '@/lib/storage'
import { buttonClass, panelClass, pct, stamp } from './shared'

type Scope = 'all' | 'main' | 'growth'
/** 个股名单排序: 默认按当前涨幅; 有竞价数据时可按竞价指标排 (盘前 09:15–09:30 即可用) */
type SortKey = 'change' | 'auction_pct' | 'auction_amount' | 'auction_ratio' | 'first_seal'
/** HH:MM:SS → 秒; 排序按降序取值, 首封越早取值越大 (取负) */
const sealSeconds = (value?: string | null) => {
  const m = value ? /^(\d{2}):(\d{2}):(\d{2})$/.exec(value) : null
  return m ? -(Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3])) : null
}
const sortOptions: { id: SortKey; label: string; group: 'base' | 'auction' | 'seal'; value: (row: FirstBoardFeedbackMember) => number | null | undefined }[] = [
  { id: 'change', label: '涨幅', group: 'base', value: row => row.change_pct },
  { id: 'auction_pct', label: '竞价涨幅', group: 'auction', value: row => row.auction?.pct_chg },
  { id: 'auction_amount', label: '竞价金额', group: 'auction', value: row => row.auction?.amount },
  { id: 'auction_ratio', label: '竞价量比', group: 'auction', value: row => row.auction?.vol_ratio },
  { id: 'first_seal', label: '昨日首封', group: 'seal', value: row => sealSeconds(row.seal?.first_time) },
]
const layerRows: { key: 'early' | 'late' | 'unopened' | 'reopened'; label: string; hint: string }[] = [
  { key: 'early', label: '早封', hint: '昨日首次封板在 10:00 之前' },
  { key: 'late', label: '10:00 后封', hint: '昨日首次封板在 10:00 之后' },
  { key: 'unopened', label: '未开板', hint: '昨日封板后盘中没有开过板' },
  { key: 'reopened', label: '开过板', hint: '昨日盘中开过板后回封' },
]
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

function MemberTable({ rows, muted, showAuction, showSeal, onOpenStock }: { rows: FirstBoardFeedbackMember[]; muted: boolean; showAuction: boolean; showSeal: boolean; onOpenStock: (symbol: string, name: string, navList: StockLink[]) => void }) {
  const navList = rows.map(({ symbol, name }) => ({ symbol, name }))
  return <div className="max-h-96 overflow-auto">
    <table className={cn(tableClass, showAuction ? 'min-w-[1080px]' : 'min-w-[760px]', showSeal && 'min-w-[1240px]')} aria-label="昨日首板个股表现">
      <thead className="sticky top-0 bg-surface"><tr><th scope="col" className="py-3 pr-3 font-normal text-secondary">股票</th>{showSeal && <><th scope="col" className={headClass} title="昨日首次封板时间 (北京时间)">昨日首封</th><th scope="col" className={headClass} title="昨日封板后盘中是否开过板">开板</th><th scope="col" className={headClass} title="昨日收盘封单额; 上游口径异常的日子不显示">封单额</th></>}{showAuction && <><th scope="col" className={headClass}>竞价涨幅</th><th scope="col" className={headClass}>竞价金额</th><th scope="col" className={headClass}>竞价量比</th><th scope="col" className={headClass} title="竞价封涨停时涨停价上的委买金额">涨停委买</th></>}<th scope="col" className={headClass}>现价</th><th scope="col" className={headClass}>涨幅</th><th scope="col" className={headClass}>开盘溢价</th><th scope="col" className={headClass}>开盘后涨幅</th><th scope="col" className={headClass}>状态</th><th scope="col" className={headClass}>报价时间</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.symbol} className="border-t border-border">
        <td className="py-2.5 pr-3"><button type="button" className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpenStock(row.symbol, row.name, navList)}>{row.name}<span className="block font-normal text-secondary">{row.symbol}</span></button></td>
        {showSeal && <>
          <td className={cellClass}>{row.seal?.first_time ? row.seal.first_time.slice(0, 5) : '—'}</td>
          <td className={cellClass}>{row.seal?.opened == null ? '—' : row.seal.opened ? '开过' : '未开'}</td>
          <td className={cellClass}>{row.seal?.fd_amount == null ? '—' : fmtBigNum(row.seal.fd_amount)}</td>
        </>}
        {showAuction && <>
          <td className={cn(cellClass, priceColorClass(row.auction?.pct_chg))}>{fmtPct(row.auction?.pct_chg)}</td>
          <td className={cellClass}>{row.auction?.amount == null ? '—' : fmtBigNum(row.auction.amount)}</td>
          <td className={cellClass}>{row.auction?.vol_ratio == null ? '—' : row.auction.vol_ratio.toFixed(2)}</td>
          <td className={cellClass}>{row.auction?.limit_bid_amount == null ? '—' : fmtBigNum(row.auction.limit_bid_amount)}</td>
        </>}
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

function Kpi({ label, children, value, muted = false, title }: { label: string; children: ReactNode; value?: number | null; muted?: boolean; title?: string }) {
  return <div className="flex items-baseline gap-1.5" title={title}>
    <dt className="text-xs text-secondary">{label}</dt>
    <dd className={cn('text-sm font-medium tabular-nums', value !== undefined && !muted ? priceColorClass(value) : 'text-foreground')}>{children}</dd>
  </div>
}

/**
 * 市场温度区：默认收起为一行关键指标，展开后再显示开盘分布、完整指标与个股/板块明细。
 * 展开状态按浏览器保存；市场范围切换在两种状态间保留。
 */
export function YesterdayFeedback({ data, refreshFailed = false, onOpenStock, children }: {
  data?: FirstBoardFeedback
  refreshFailed?: boolean
  onOpenStock: (symbol: string, name: string, navList: StockLink[]) => void
  children: ReactNode
}) {
  const [scope, setScope] = useState<Scope>('all')
  const [open, setOpen] = useState(() => storage.firstBoardFeedbackExpanded.get(false) === true)
  const [expanded, setExpanded] = useState(false)
  const [showQuoteNote, setShowQuoteNote] = useState(false)
  const [sectorKind, setSectorKind] = useState<'industry' | 'concept'>('industry')
  const [sortKey, setSortKey] = useState<SortKey>('change')
  const bodyId = useId()
  const detailId = useId()
  const titleId = useId()
  const quoteNoteId = useId()
  const summary = data?.groups[scope]
  const muted = refreshFailed || data?.status === 'stale'
  const available = !!data && data.status !== 'waiting' && data.status !== 'unavailable'
  const quoteNote = data && available && data.message ? `${refreshFailed ? '保留快照' : statusLabels[data.status]} · ${data.message}` : null
  const showAuction = !!data?.auction && (data.rows ?? []).some(row => row.auction)
  const showSeal = !!data?.layers && (data.rows ?? []).some(row => row.seal)
  const sortChoices = sortOptions.filter(option => option.group === 'base'
    || (option.group === 'auction' && showAuction) || (option.group === 'seal' && showSeal))
  const activeSort = sortChoices.some(option => option.id === sortKey) ? sortKey : 'change'
  const layers = data?.layers?.[scope]
  const members = useMemo(() => {
    const value = sortOptions.find(option => option.id === activeSort)!.value
    return (data?.rows ?? []).filter(row => scope === 'all' || row.board === scope)
      .toSorted((a, b) => (value(b) ?? -Infinity) - (value(a) ?? -Infinity))
  }, [data?.rows, scope, activeSort])
  const sectors = useMemo(() => (data?.sectors ?? []).filter(row => row.kind === sectorKind), [data?.sectors, sectorKind])
  const unknownLimits = summary ? Math.max(0, summary.sample_count - summary.limit_known_count) : 0
  const opening = data?.opening_distribution
  const toggleOpen = () => {
    const next = !open
    setOpen(next)
    storage.firstBoardFeedbackExpanded.set(next)
  }
  const statusBody = data?.status === 'waiting'
    ? <p role="status" aria-label="正在加载昨日首板反馈" className="text-xs text-secondary motion-safe:animate-pulse">{data.message || '正在建立昨日首板名单'}</p>
    : !available ? <p className="text-sm leading-relaxed text-secondary">{data?.message || '昨日首板反馈暂未生成。可刷新后重试。'}</p>
    : !summary?.sample_count ? <p className="text-sm leading-relaxed text-secondary">该范围没有可确认的昨日首板。缺少前日行情的股票不会被当作首板。</p>
    : null
  const notes = summary?.sample_count && available ? [
    summary.small_sample && '样本少于 10 只，比例容易受个别股票影响。',
    unknownLimits > 0 && `涨停状态待确认 ${unknownLimits} 只，二板数量为已确认值。`,
  ].filter(Boolean) as string[] : []

  return <section aria-labelledby={titleId} className={cn(panelClass, 'min-w-0 space-y-3')} data-yesterday-feedback>
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={titleId} className="text-sm font-semibold">昨日首板反馈</h2>
        {data && <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-secondary"><span>样本 {data.sample_date ?? '待确认'}</span><span>表现 {data.as_of ?? '待确认'}</span>{summary && <span>行情 {summary.quote_count} / {summary.sample_count}</span>}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1" role="group" aria-label="昨日首板市场范围">{scopes.map(item => <button type="button" key={item.id} aria-pressed={scope === item.id} disabled={!available} className={cn(buttonClass, 'min-h-8 px-2.5', scope === item.id && 'border-accent/50 text-foreground')} onClick={() => setScope(item.id)}>{item.label}</button>)}</div>
        <button type="button" className={cn(buttonClass, 'min-h-8')} aria-expanded={open} aria-controls={bodyId} onClick={toggleOpen}>{open ? '收起分布' : '展开分布'}{open ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}</button>
      </div>
    </div>
    {!open && (statusBody ?? (summary ? <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
      <Kpi label="平均溢价" value={summary.mean_change_pct} muted={muted}>{fmtPct(summary.mean_change_pct)}</Kpi>
      <Kpi label="上涨占比">{summary.up_ratio == null ? '—' : `${(summary.up_ratio * 100).toFixed(1)}%`}</Kpi>
      <Kpi label="当前二板">{summary.limit_up_count} / {summary.sample_count}</Kpi>
      <Kpi label="跌幅 ≥ 5%">{summary.loss_count} / {summary.sample_count}</Kpi>
      {opening && opening.open_count > 0 && <Kpi label="非一字涨停开盘上涨占比" title={`昨日非一字涨停 ${opening.sample_count} 只，有效开盘 ${opening.open_count} 只`}>{pct(opening.up_ratio)}</Kpi>}
    </dl> : null))}
    {!open && notes.length > 0 && <p className="text-xs text-secondary">{notes.join(' ')}</p>}
    {open && <div id={bodyId} className="grid min-w-0 gap-5 xl:grid-cols-2">
    <div className="min-w-0 space-y-3">{children}</div>
    <section aria-label="昨日首板整体表现" className="min-w-0 border-t border-border pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0">
      {statusBody ?? (data && summary ? <>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-3">
            <Metric label="平均溢价" value={summary.mean_change_pct} muted={muted}>{fmtPct(summary.mean_change_pct)}</Metric>
            <Metric label="中位数" value={summary.median_change_pct} muted={muted}>{fmtPct(summary.median_change_pct)}</Metric>
            <Metric label="上涨占比" hint={`${summary.up_count} / ${summary.quote_count} 只有效行情`}>{summary.up_ratio == null ? '—' : `${(summary.up_ratio * 100).toFixed(1)}%`}</Metric>
            <Metric label="当前二板" hint={summary.limit_up_ratio == null ? '比例待确认' : `占昨日样本 ${(summary.limit_up_ratio * 100).toFixed(1)}%`}>{summary.limit_up_count}<span className="text-sm font-normal text-secondary"> / {summary.sample_count}</span></Metric>
            <Metric label="开盘后涨幅" value={summary.mean_open_change} muted={muted} hint={`开盘数据 ${summary.open_count} / ${summary.sample_count}`}>{fmtPct(summary.mean_open_change)}</Metric>
            <Metric label="跌幅 ≥ 5%" hint={summary.loss_ratio == null ? '比例待确认' : `占有效行情 ${(summary.loss_ratio * 100).toFixed(1)}%`}>{summary.loss_count}<span className="text-sm font-normal text-secondary"> / {summary.sample_count}</span></Metric>
          </dl>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-secondary">
            <span className="relative inline-flex items-center gap-1">
              <span>{data.quote_mode === 'live' ? '盘中行情' : '最近行情'} · 统计更新 {stamp(data.observed_at)}</span>
              {quoteNote && <span className="inline-flex" onMouseEnter={() => setShowQuoteNote(true)} onMouseLeave={() => setShowQuoteNote(false)}>
                <button type="button" aria-label="行情说明" aria-describedby={showQuoteNote ? quoteNoteId : undefined}
                  className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-secondary hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                  onFocus={() => setShowQuoteNote(true)} onBlur={() => setShowQuoteNote(false)} onKeyDown={event => { if (event.key === 'Escape') setShowQuoteNote(false) }}>
                  <Info className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
                {showQuoteNote && <span id={quoteNoteId} role="tooltip" className="absolute left-0 top-full z-30 w-80 max-w-[calc(100vw-5rem)] pt-2">
                  <span className="block rounded-md border border-border bg-elevated p-3 text-xs leading-relaxed text-foreground">{quoteNote}</span>
                </span>}
              </span>}
            </span>
            <button type="button" className={buttonClass} aria-expanded={expanded} aria-controls={detailId} onClick={() => setExpanded(value => !value)}>{expanded ? '收起明细' : '查看明细'}{expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}</button>
          </div>
          {notes.map(note => <p key={note} className="mt-2 text-xs text-secondary">{note}</p>)}
        </> : null)}
      {!summary?.sample_count && quoteNote && <p className="mt-2 text-xs leading-relaxed text-secondary">{quoteNote}</p>}
    </section>
    {expanded && available && summary && <div id={detailId} className="min-w-0 space-y-5 border-t border-border pt-4 xl:col-span-2">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-medium">个股表现 <span className="font-normal text-secondary">{scopes.find(item => item.id === scope)?.label} · {members.length} 只</span></h4>
          {sortChoices.length > 1 && <div className="flex items-center gap-1" role="group" aria-label="个股排序">
            <span className="text-xs text-secondary" title={`只用于排序观察，不改变优先分或买入提示。${showAuction ? `竞价为猫爪集合竞价指标，09:15 起更新，取数 ${stamp(data?.auction?.fetched_at)}。` : ''}${showSeal ? '昨日首封越早越靠前。' : ''}`}>排序</span>
            {sortChoices.map(option => <button type="button" key={option.id} aria-pressed={activeSort === option.id} className={cn(buttonClass, 'min-h-7 px-2', activeSort === option.id && 'border-accent/50 text-foreground')} onClick={() => setSortKey(option.id)}>{option.label}</button>)}
          </div>}
        </div>
        {members.length ? <MemberTable rows={members} muted={muted} showAuction={showAuction} showSeal={showSeal} onOpenStock={onOpenStock} /> : <p className="text-xs text-secondary">该范围暂无可展示的个股明细。</p>}
      </div>
      {showSeal && layers && <div className="space-y-3">
        <h4 className="text-sm font-medium">按昨日封板质量分层 <span className="font-normal text-secondary">{scopes.find(item => item.id === scope)?.label}</span></h4>
        <div className="overflow-x-auto">
          <table className={cn(tableClass, 'min-w-[560px]')} aria-label="按昨日封板质量分层">
            <thead><tr><th scope="col" className="pb-3 pr-3 font-normal text-secondary">昨日封板</th><th scope="col" className={headClass}>样本</th><th scope="col" className={headClass}>平均溢价</th><th scope="col" className={headClass}>上涨占比</th><th scope="col" className={headClass}>当前二板</th></tr></thead>
            <tbody>{layerRows.map(({ key, label, hint }) => {
              const layer = layers[key]
              return <tr key={key} className="border-t border-border">
                <td className="py-2.5 pr-3" title={hint}>{label}</td>
                <td className={cellClass}>{layer.sample_count}</td>
                <td className={cn(cellClass, !muted && priceColorClass(layer.mean_change_pct))}>{fmtPct(layer.mean_change_pct)}</td>
                <td className={cellClass}>{pct(layer.up_ratio)}</td>
                <td className={cellClass}>{layer.sample_count ? layer.limit_up_count : '—'}</td>
              </tr>
            })}</tbody>
          </table>
        </div>
        <p className="text-xs leading-relaxed text-secondary">昨日封板信息来自本地涨停池归档（{data.seal_source?.date}，覆盖 {data.seal_source?.matched} / {data.seal_source?.sample} 只）；只作观察，不改变优先分或买入提示。各层样本可能很少，比例容易受个别股票影响。</p>
      </div>}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="text-sm font-medium">关联板块对照</h4><div className="flex gap-1" role="group" aria-label="关联板块分类">{(['industry', 'concept'] as const).map(kind => <button key={kind} type="button" aria-pressed={sectorKind === kind} className={cn(buttonClass, sectorKind === kind && 'border-accent/50 text-foreground')} onClick={() => setSectorKind(kind)}>{kind === 'industry' ? '行业' : '概念'}</button>)}</div></div>
        <p className="text-xs leading-relaxed text-secondary">行业／概念对照固定为沪深非 ST 全范围，不随上方市场范围切换。{data.membership_note || '按当前板块成分计算等权涨幅，非官方板块指数；概念可重复归属，不跨概念加总。'}</p>
        {sectors.length ? <SectorTable rows={sectors} muted={muted} /> : <p className="py-3 text-xs leading-relaxed text-secondary">暂无满足条件的{sectorKind === 'industry' ? '行业' : '概念'}对照。需要可核验的成分和行情覆盖，且至少包含 2 只昨日首板。</p>}
      </div>
    </div>}
    </div>}
    {refreshFailed && <p role="status" className="text-xs text-secondary">刷新失败，保留上次快照。请使用页面刷新重试。</p>}
  </section>
}
