import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Newspaper } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { api, type NewsCatalystGroup, type NewsCatalystItem, type NewsCatalystsResult } from '@/lib/api'
import { cnDateFromUtc, cnDateTimeFromUtc, fmtPct, fmtVolume, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { Announcements } from './Announcements'
import type { NavItem } from '@/lib/listNav'

type Ready = Extract<NewsCatalystsResult, { state: 'ok' }>
type OpenStock = (item: NewsCatalystItem, list: NewsCatalystItem[]) => void
const link = 'text-foreground underline decoration-border underline-offset-4 hover:text-accent hover:decoration-accent focus-visible:outline-accent'

function freshness(group: NewsCatalystGroup, days: number) {
  if (group.fresh) return <span className="font-medium text-accent">首次出现</span>
  if (group.fresh === null) return <span title="近期解读未取全，无法判断是否首次出现">回看不全</span>
  return <span>近 {days} 日出现 {group.seen_dates.length} 日</span>
}

function Context({ day }: { day: string }) {
  const phases = useQuery({ queryKey: QK.regimePhases(day, day), queryFn: () => api.regimePhases(day, day) })
  const segment = phases.data?.segments.at(-1)
  if (!segment) return null
  const mainlines = segment.top_mainlines.slice(0, 3).map(m => m.member)
  return <span>情绪阶段 <b className="font-medium text-foreground">{segment.label}</b>
    {mainlines.length > 0 && <> · 主线 <b className="font-medium text-foreground">{mainlines.join('、')}</b></>}
  </span>
}

function GroupList({ groups, days, selected, onSelect }: {
  groups: NewsCatalystGroup[]; days: number; selected: string | null; onSelect: (keyword: string) => void
}) {
  return <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border bg-surface">
    {groups.map(group => <li key={group.keyword}>
      <button type="button" aria-pressed={group.keyword === selected} onClick={() => onSelect(group.keyword)}
        className={'grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left focus-visible:outline-accent '
          + (group.keyword === selected ? 'bg-accent/10' : 'hover:bg-elevated')}>
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="truncate text-sm font-medium">{group.keyword}</span>
          {group.batch && <span className="shrink-0 rounded bg-accent/10 px-1.5 text-[11px] text-accent">批量</span>}
        </span>
        <span className={'font-mono text-sm ' + priceColorClass(group.avg_change_pct)}>{fmtPct(group.avg_change_pct)}</span>
        <span className="text-xs text-secondary">{group.stock_count} 只异动 · {group.limit_up_count} 只涨停 · {freshness(group, days)}</span>
        <span className="text-right text-xs text-secondary">
          {group.next_day ? <>次日 <span className={'font-mono ' + priceColorClass(group.next_day.avg_change_pct)}>{fmtPct(group.next_day.avg_change_pct)}</span></> : null}
        </span>
      </button>
    </li>)}
  </ul>
}

function GroupDetail({ group, items, data, open }: { group: NewsCatalystGroup; items: NewsCatalystItem[]; data: Ready; open: OpenStock }) {
  return <section aria-label={group.keyword + ' 关联个股'} className="rounded-lg border border-border bg-surface p-4 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:overflow-auto">
    <h2 className="text-lg font-semibold">{group.keyword}</h2>
    <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-xs text-secondary sm:grid-cols-4">
      {([['领涨', group.leader], ['成交额最大', group.anchor]] as const).map(([label, stock]) => <div key={label} className="min-w-0">
        <dt>{label}</dt>
        <dd className="mt-0.5 truncate text-sm text-foreground">{stock ? <>{stock.name} <span className={'font-mono ' + priceColorClass(stock.change_pct)}>{fmtPct(stock.change_pct)}</span></> : '—'}</dd>
      </div>)}
      <div><dt>解读最早发布</dt><dd className="mt-0.5 font-mono text-sm text-foreground">{group.first_published_at?.slice(11, 16) ?? '—'}</dd></div>
      <div><dt>次日{group.next_day ? `（${group.next_day.date.slice(5)}）` : ''}</dt>
        <dd className="mt-0.5 text-sm text-foreground">{group.next_day
          ? <><span className={'font-mono ' + priceColorClass(group.next_day.avg_change_pct)}>{fmtPct(group.next_day.avg_change_pct)}</span> · {group.next_day.limit_up_count} 只涨停</>
          : data.next_date ? '暂无行情' : '尚未产生'}</dd></div>
    </dl>
    <ul className="mt-3 divide-y divide-border/60 border-t border-border/60">
      {items.map(item => <li key={item.symbol} className="py-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <button type="button" className={'text-sm font-medium ' + link} onClick={() => open(item, items)}>{item.name}</button>
          {item.tag && <span className="text-xs text-secondary">{item.tag}</span>}
          <span className={'ml-auto font-mono text-sm ' + priceColorClass(item.change_pct)}>{fmtPct(item.change_pct)}</span>
          {data.next_date && <span className="w-24 text-right text-xs text-secondary">次日 <span className={'font-mono ' + priceColorClass(item.next_change_pct)}>{fmtPct(item.next_change_pct)}</span></span>}
          <span className="w-20 text-right font-mono text-xs text-secondary">{fmtVolume(item.amount)}</span>
        </div>
        <details className="mt-1 text-xs text-secondary">
          <summary className="w-fit cursor-pointer focus-visible:outline-accent">{item.keywords.join(' · ')}</summary>
          <p className="mt-1 max-w-prose whitespace-pre-wrap break-words leading-relaxed">{item.content}</p>
        </details>
      </li>)}
    </ul>
  </section>
}

function Catalysts({ data, open }: { data: Ready; open: OpenStock }) {
  const [batchOnly, setBatchOnly] = useState(true)
  const [picked, setPicked] = useState<string | null>(null)
  const batches = data.groups.filter(group => group.batch)
  const groups = batchOnly && batches.length > 0 ? batches : data.groups
  const group = groups.find(g => g.keyword === picked) ?? groups[0]
  const bySymbol = new Map(data.items.map(item => [item.symbol, item]))
  const members = group ? group.symbols.flatMap(symbol => bySymbol.get(symbol) ?? []) : []
  return <>
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-secondary">
      <p role="status">
        <b className="font-medium text-foreground">{data.counts.stocks}</b> 只异动 · <b className="font-medium text-foreground">{data.counts.limit_up}</b> 只涨停 · <b className="font-medium text-foreground">{batches.length}</b> 个批量催化
        <span className="ml-3">{data.live ? '当日数据，取于' : '取于'} {cnDateTimeFromUtc(data.fetched_at) || data.fetched_at}（北京）</span>
      </p>
      {batches.length > 0 && batches.length < data.groups.length && <label className="flex cursor-pointer items-center gap-1.5">
        <input type="checkbox" checked={batchOnly} onChange={event => setBatchOnly(event.target.checked)} />只看批量
      </label>}
    </div>
    {data.quote_state === 'unavailable' && <p role="alert" className="text-sm text-warning">本地缺少该交易日的日线行情，涨跌幅暂不可用，涨停数取自上游标签。请在「数据」页同步日线。</p>}
    {group
      ? <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <GroupList groups={groups} days={data.lookback.days} selected={group.keyword} onSelect={setPicked} />
        <GroupDetail group={group} items={members} data={data} open={open} />
      </div>
      : <p className="text-sm text-secondary">当日的异动解读没有两只以上个股共用的关键词。</p>}
    <details className="text-xs text-secondary">
      <summary className="w-fit cursor-pointer py-1 focus-visible:outline-accent">数据与口径</summary>
      <div className="mt-2 max-w-prose space-y-1 leading-relaxed">
        <p>来源为扶摇（同花顺）异动解读，只收录当日已经大涨、涨停或大跌的个股，因此这里核对的是催化的广度、新鲜度和次日延续，不能反映“有消息但盘面没有反应”。</p>
        <p>按解读关键词聚合，同一只个股可属于多个关键词；至少 3 只异动且 2 只涨停记为“批量”。涨跌幅、涨停数和成交额取自本地日线。</p>
        <p>新鲜度回看前 {data.lookback.days} 个交易日，其中 {data.lookback.cached} 日已取得解读。“次日”为同一批个股下一交易日的平均涨跌幅。</p>
      </div>
    </details>
  </>
}

type StockTarget = { symbol: string; name: string; date: string; navList: NavItem[] }

function CatalystsTab({ day, onOpen }: { day: string; onOpen: (stock: StockTarget) => void }) {
  // 当日数据每次查询都会请求上游，窗口聚焦等自动重取限制为每分钟一次。
  const query = useQuery({ queryKey: QK.newsCatalysts(day), queryFn: () => api.newsCatalysts(day), staleTime: 60_000 })
  const data = query.data?.requested_date === day ? query.data : undefined
  return <>
    {data && <p className="text-xs text-secondary"><Context day={data.trade_date} /></p>}
    {query.isPending && <p role="status" className="text-sm text-secondary">正在读取异动解读，首次查看需回补近期数据…</p>}
    {query.isError && <p role="alert" className="text-sm text-danger">
      {query.error.message || '读取失败。'}
      <button type="button" className="ml-2 text-accent underline" onClick={() => void query.refetch()}>重试</button>
    </p>}
    {data && data.trade_date !== day && <p className="text-sm text-secondary">{day} 暂无异动解读或不是交易日，显示 {data.trade_date} 的数据。</p>}
    {data?.state === 'source_unavailable' && <p role="alert" className="text-sm text-warning">
      催化解读需要扶摇数据源。<Link className={'ml-1 ' + link} to="/settings?tab=data-sources&highlight=fuyao">前往配置</Link>
    </p>}
    {data?.state === 'no_data' && <p role="alert" className="text-sm text-warning">
      {data.message}
      <button type="button" className="ml-2 text-accent underline" disabled={query.isFetching} onClick={() => void query.refetch()}>重试</button>
    </p>}
    {data?.state === 'ok' && <Catalysts key={data.trade_date} data={data}
      open={(item, list) => onOpen({ symbol: item.symbol, name: item.name, date: data.trade_date, navList: list.map(s => ({ symbol: s.symbol, name: s.name })) })} />}
  </>
}

const tabs = [['catalysts', '催化验证'], ['announcements', '公告排雷']] as const
type Tab = typeof tabs[number][0]

export function NewsAnalysis() {
  const today = cnDateFromUtc(new Date().toISOString())
  const [day, setDay] = useState(today)
  const [tab, setTab] = useState<Tab>('catalysts')
  const [stock, setStock] = useState<StockTarget | null>(null)
  return <div className="min-h-full bg-base text-foreground">
    <header className="border-b border-border bg-surface px-4 py-3 pl-14 md:px-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Newspaper aria-hidden className="h-5 w-5 text-accent" />资讯分析</h1>
        <span className="flex items-center gap-2 text-xs text-secondary">交易日 <DatePicker value={day} max={today} onChange={date => { setDay(date); setStock(null) }} /></span>
      </div>
    </header>
    <div className="mx-auto max-w-6xl space-y-4 px-3 py-4 sm:px-6">
      <nav aria-label="资讯分析视图" className="flex gap-5 border-b border-border">
        {tabs.map(([id, label]) => <button type="button" key={id} aria-pressed={id === tab}
          onClick={() => { setTab(id); setStock(null) }}
          className={'whitespace-nowrap border-b-2 px-1 pb-3 pt-1 text-sm font-medium focus-visible:outline-accent ' + (id === tab ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground')}>
          {label}
        </button>)}
      </nav>
      {tab === 'catalysts'
        ? <CatalystsTab day={day} onOpen={setStock} />
        : <Announcements day={day} open={(row, list) => setStock({ symbol: row.symbol, name: row.name, date: row.published_at.slice(0, 10),
          navList: [...new Map(list.map(r => [r.symbol, { symbol: r.symbol, name: r.name }])).values()] })} />}
    </div>
    <StockPreviewDialog symbol={stock?.symbol ?? null} name={stock?.name} tradeDate={stock?.date ?? null} navList={stock?.navList}
      onNavigate={(symbol, name) => setStock(prev => prev && { ...prev, symbol, name: name || symbol })} onClose={() => setStock(null)} />
  </div>
}
