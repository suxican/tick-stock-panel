import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Radar } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { DimensionMembersDialog, type DatedThemeTarget } from '@/components/DimensionMembersDialog'
import { api, type ZzshareThemesResult } from '@/lib/api'
import { cnDateFromUtc, cnDateTimeFromUtc, fmtVolume, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import type { NavItem } from '@/lib/listNav'
import { ResearchPanel } from './ResearchPanel'
import { OfficialThemes } from './OfficialThemes'
import './radar.css'

const tabs = [
  ['themes', '强势题材'], ['sentiment', '市场情绪'], ['heat', '个股热榜'], ['lhb', '龙虎榜'],
] as const
type Tab = typeof tabs[number][0]
export type OpenStock = (symbol: string, name: string, date: string, navList?: NavItem[]) => void
type Theme = ZzshareThemesResult['rows'][number]
const action = 'min-h-9 rounded-btn border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-elevated focus-visible:outline-accent disabled:opacity-50'
const link = 'text-foreground underline decoration-border underline-offset-4 hover:text-accent hover:decoration-accent focus-visible:outline-accent'

function percent(value: number | null) {
  return value == null ? '—' : value.toFixed(1) + '%'
}

function ThemeStock({ label, stock, day, open }: { label: string; stock: Theme['anchor']; day: string; open: OpenStock }) {
  return <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
    <span className="text-xs text-secondary">{label}</span>
    {stock ? <>
      <button type="button" className={'text-sm font-medium ' + link} onClick={() => open(stock.symbol, stock.name, day)}>{stock.name}</button>
      <span className={'ml-auto font-mono text-sm ' + priceColorClass(stock.change_pct)}>{stock.change_pct > 0 ? '+' : ''}{percent(stock.change_pct)}</span>
    </> : <span className="text-xs text-secondary">暂无可核验标的</span>}
  </div>
}

function RankedThemes({ data, day, open, openTheme }: { data: ZzshareThemesResult; day: string; open: OpenStock; openTheme: (theme: Theme) => void }) {
  return <>
    <ol className="grid items-start gap-3 lg:grid-cols-2 xl:grid-cols-3">
      {data.rows.slice(0, 3).map((row, i) => <li key={row.plate_code} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <h3 className="min-w-0 text-lg font-semibold"><button type="button" className={'break-words text-left ' + link} onClick={() => openTheme(row)}>{row.plate_name}</button></h3>
            <span className="ml-auto text-xs text-secondary">第 {i + 1} 名</span>
          </div>
          <p className="mt-1 text-xs text-secondary">强度分 <b className="font-mono text-sm font-medium text-foreground">{row.score.toFixed(1)}</b></p>
          <dl className="mt-3 grid grid-cols-[1.2fr_1fr_.8fr] gap-2 border-y border-border/60 py-3">
            <div><dt className="text-xs text-secondary">平均涨幅</dt><dd className={'mt-1 font-mono text-xl font-semibold ' + priceColorClass(row.avg_change_pct)}>{row.avg_change_pct > 0 ? '+' : ''}{percent(row.avg_change_pct)}</dd></div>
            <div><dt className="text-xs text-secondary">上涨比例</dt><dd className="mt-1 font-mono text-lg">{percent(row.rising_pct)}</dd></div>
            <div><dt className="text-xs text-secondary">涨停</dt><dd className="mt-1 font-mono text-lg">{row.limit_up_count}<span className="ml-1 font-sans text-xs text-secondary">只</span></dd></div>
          </dl>
          {(row.coverage_pct < 100 || row.limit_down_count > 0) && <p className="mt-2 text-xs text-warning">
            {row.coverage_pct < 100 && <span>行情覆盖 {percent(row.coverage_pct)}</span>}
            {row.limit_down_count > 0 && <span className="ml-2">跌停 {row.limit_down_count} 只</span>}
          </p>}
        </div>
        <div className="flex flex-col gap-2">
          <ThemeStock label="龙头股" stock={row.leader} day={day} open={open} />
          <ThemeStock label="中军股" stock={row.anchor} day={day} open={open} />
        </div>
        <details className="border-t border-border/60 pt-2 text-xs text-secondary">
          <summary className="w-fit cursor-pointer py-1 text-accent focus-visible:outline-accent">题材详情</summary>
          <p className="mt-2 leading-relaxed">上涨 {row.rising_count ?? '—'} 只 · 强势 {row.strong_count ?? '—'} 只（{percent(row.strong_pct)}） · 跌停 {row.limit_down_count} 只</p>
          <p className="mt-1">有效行情 {row.valid_count}/{row.member_count} 只</p>
          <p className="mt-1">龙头成交额 {row.leader.amount_yuan == null ? '—' : fmtVolume(row.leader.amount_yuan) + '元'}</p>
          <p className="mt-1">中军成交额 {row.anchor?.amount_yuan == null ? '—' : fmtVolume(row.anchor.amount_yuan) + '元'}</p>
        </details>
      </li>)}
    </ol>
    <details className="mt-3 text-sm">
      <summary className="w-fit cursor-pointer text-accent">全部入围题材（{data.rows.length}）</summary>
      <div className="mt-2 max-h-80 overflow-auto">
        <table className="w-full text-left text-sm tabular-nums">
          <thead className="sticky top-0 bg-surface text-secondary"><tr>{['排名', '题材', '强度分', '行情覆盖', '龙头股'].map(title => <th key={title} className="whitespace-nowrap px-2 py-2 font-medium">{title}</th>)}</tr></thead>
          <tbody>{data.rows.map((row, i) => <tr key={row.plate_code} className="border-t border-border/60">
            <td className="p-2">{i + 1}</td><td className="p-2"><button type="button" className="text-left text-accent hover:underline" onClick={() => openTheme(row)}>{row.plate_name}</button></td><td className="p-2">{row.score.toFixed(1)}</td>
            <td className="p-2">{percent(row.coverage_pct)}</td>
            <td className="p-2">{row.leader && <button type="button" className="text-accent hover:underline" onClick={() => open(row.leader.symbol, row.leader.name, day)}>{row.leader.name}</button>}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </details>
  </>
}

function LocalThemes({ day, open, live }: { day: string; open: OpenStock; live: boolean }) {
  const client = useQueryClient()
  const [target, setTarget] = useState<DatedThemeTarget | null>(null)
  const query = useQuery({
    queryKey: QK.zzshareThemes(day), queryFn: () => api.zzshareThemes(day), retry: false,
    refetchInterval: q => q.state.data?.state === 'running' ? 3000 : false,
  })
  const start = useMutation({
    mutationFn: () => api.syncZzshareThemes(day),
    onSuccess: result => { if (result.date === day) client.setQueryData(QK.zzshareThemes(day), result) },
  })
  const data = query.data?.date === day ? query.data : undefined
  function openTheme(theme: Theme) {
    if (!data) return
    const date = data.date
    const code = theme.plate_code
    setTarget({
      kind: 'theme', value: theme.plate_name, date,
      members: {
        queryKey: QK.zzshareThemeMembers(code, date),
        queryFn: async () => {
          const result = await api.zzshareThemeMembers(code, date)
          if (result.date !== date || result.plate_code !== code) throw new Error('成分数据与所选题材或交易日不符。')
          return result
        },
      },
      intraday: {
        queryKey: QK.zzshareThemeIntraday(code, date),
        queryFn: async () => {
          const result = await api.zzshareThemeIntraday(code, date)
          if (result.status === 'ok' && result.date !== date) throw new Error('分时数据与所选交易日不符。')
          return result
        },
      },
    })
  }
  const busy = data?.state === 'running' || start.isPending
  const autoRefresh = live && !busy && !query.isError && data?.state === 'ready'
  const compute = start.mutate
  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && day === cnDateFromUtc(new Date().toISOString())) compute()
    }, 5 * 60_000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, compute, day])
  return <section aria-labelledby="theme-rank-title" className="space-y-3">
    <h2 id="theme-rank-title" className="sr-only">本地板块强度榜</h2>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-secondary" role="status">
        {data && <span className="font-medium text-foreground">{({ idle: '尚未计算', running: '暂定排名', ready: '计算完成', error: '计算未完成', no_data: '当日无可用排名' })[data.state]}</span>}
        {data?.state === 'running' && <span>已处理 {data.progress.done}/{data.progress.total} 个候选</span>}
        {data?.quote_asof ? <span>行情观测 {cnDateTimeFromUtc(data.quote_asof) || data.quote_asof}（北京）</span> : !!data?.rows.length && <span>本地日行情 · 盘中时间未核验</span>}
      </div>
      <button type="button" className={action + ' border-accent/40 bg-accent/10 text-accent'} disabled={busy || query.isPending} onClick={() => start.mutate()}>
        {busy ? '正在计算…' : '计算题材排名'}
      </button>
    </div>
    {query.isPending && <p role="status" className="mt-4 text-sm text-secondary">正在读取排名…</p>}
    {(query.error || start.error || (query.data && !data)) && <p role="alert" className="mt-3 text-sm text-danger">
      {start.error?.message || query.error?.message || '返回日期与所选交易日不一致。'}
      <button type="button" className="ml-2 text-accent underline" onClick={() => void query.refetch()}>重新读取</button>
    </p>}
    {data && <>
      {data.message && <p className={'text-sm leading-relaxed ' + (data.state === 'error' ? 'text-warning' : 'text-secondary')}>{data.message}</p>}
      {data.rows.length > 0 && <RankedThemes data={data} day={day} open={open} openTheme={openTheme} />}
    </>}
    <details className="mt-3 text-xs text-secondary">
      <summary className="w-fit cursor-pointer py-1 focus-visible:outline-accent">数据详情与评分口径</summary>
      <p className="mt-2 leading-relaxed">以当日 ZZShare 题材候选和成员股行情计算强度。{data?.scope}</p>
      {data && data.state !== 'idle' && <p className="mt-1">已处理 {data.progress.done}/{data.progress.total} 个候选 · 成功核验 {data.coverage.completed} 个 · {data.coverage.eligible} 个入围</p>}
      {data?.computed_at && <p className="mt-1">计算于 {cnDateTimeFromUtc(data.computed_at) || data.computed_at}（北京）</p>}
      {autoRefresh && <p className="mt-1">盘中每 5 分钟重算</p>}
      <p className="mt-2 max-w-prose leading-relaxed">板块当日完整成分中至少 5 只有效行情且覆盖率达 80%，平均涨幅为正、至少一半有效成分上涨才入围。各股数占比以有效成分为分母。强度合计 100 分，均涨幅占 40 分，上涨比例、涨幅超过 5% 的强股比例、涨停比例各占 20 分。均涨幅、强股比例、涨停比例分别在 5%、30%、10% 达到该项满分。龙头为涨幅最高的成分股，中军为上涨且成交额可核验的成分股中成交额最高者，两种角色可由同一股担任。</p>
    </details>
    <DimensionMembersDialog target={target} onClose={() => setTarget(null)} onStockClick={(symbol, name, navList) => {
      if (!target) return
      setTarget(null)
      open(symbol, name || symbol, target.date, navList)
    }} />
  </section>
}

export function ThemeRadar() {
  const today = cnDateFromUtc(new Date().toISOString())
  const [day, setDay] = useState(today)
  const [tab, setTab] = useState<Tab>('themes')
  const [themeView, setThemeView] = useState<'official' | 'boards' | 'local'>('official')
  const status = useQuoteStatus({ enabled: day === today })
  const live = day === today && status.data?.is_trading_hours === true
  const [stock, setStock] = useState<{symbol: string; name: string; date: string; navList?: NavItem[]} | null>(null)
  const open: OpenStock = (symbol, name, date, navList) => setStock({ symbol, name, date, navList })
  return <div className="radar-page min-h-full bg-base text-foreground">
    <header className="border-b border-border bg-surface px-4 py-3 pl-14 md:px-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Radar aria-hidden className="h-5 w-5 text-accent" />题材雷达</h1>
        <div className="flex items-center gap-2 text-xs text-secondary">交易日 <DatePicker value={day} max={today} onChange={date => { setDay(date); setStock(null) }} /></div>
      </div>
    </header>
    <div className="mx-auto max-w-6xl space-y-4 px-3 py-4 sm:px-6">
      <nav aria-label="题材雷达视图" className="grid grid-cols-4 gap-1 border-b border-border sm:flex sm:gap-5">
        {tabs.map(([id, label]) => <button type="button" key={id} aria-pressed={id === tab}
          onClick={() => { setTab(id); setStock(null) }}
          className={'whitespace-nowrap border-b-2 px-1 pb-3 pt-1 text-sm font-medium focus-visible:outline-accent ' + (id === tab ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground')}>
          {label}
        </button>)}
      </nav>
      {tab === 'themes' && <nav aria-label="题材榜单" className="flex flex-wrap gap-1">
        {([['official', '官网精选'], ['boards', '供应商热榜'], ['local', '本地强度']] as const).map(([id, label]) =>
          <button type="button" key={id} aria-pressed={id === themeView} onClick={() => { setThemeView(id); setStock(null) }}
            className={'rounded-btn px-3 py-2 text-sm font-medium focus-visible:outline-accent ' + (id === themeView ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated hover:text-foreground')}>{label}</button>
        )}
      </nav>}
      {tab === 'themes' && themeView === 'official' && <OfficialThemes key={day} day={day} open={open} live={live} />}
      {tab === 'themes' && themeView === 'local' && <LocalThemes key={day} day={day} open={open} live={live} />}
      {(tab !== 'themes' || themeView === 'boards') && <ResearchPanel key={day + tab} day={day} tab={tab} open={open} live={live} />}
    </div>
    <StockPreviewDialog symbol={stock?.symbol ?? null} name={stock?.name} tradeDate={stock?.date ?? null} navList={stock?.navList}
      onNavigate={(symbol, name) => setStock(prev => prev && { ...prev, symbol, name: name || symbol })} onClose={() => setStock(null)} />
  </div>
}
