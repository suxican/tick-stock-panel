import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, RefreshCw } from 'lucide-react'
import { api, type ZzshareHomeResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnDateFromUtc, cnDateTimeFromUtc, priceColorClass } from '@/lib/format'
import type { OpenStock } from './ThemeRadar'

type Seat = ZzshareHomeResult['rows'][number]
const control = 'min-h-9 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-sm text-foreground hover:bg-elevated focus-visible:outline-accent'
const stockLink = 'text-sm font-medium text-foreground underline decoration-border underline-offset-4 hover:text-accent hover:decoration-accent focus-visible:outline-accent'

function number(value: number | null | undefined, digits = 0) {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}
function percent(value: number | null | undefined) {
  return value == null ? '—' : (value > 0 ? '+' : '') + value.toFixed(2) + '%'
}
function movement(row: Seat) {
  if (row.new_entry === true) return '新进前三'
  if (row.rank_change == null) return '无可比名次'
  if (row.rank_change === 0) return '名次持平'
  return row.rank_change > 0 ? `上升 ${row.rank_change} 位` : `下降 ${-row.rank_change} 位`
}
function streak(row: Seat) {
  if (row.streak != null) return `连续 ${row.streak} 个交易日`
  if (row.streak_min > 0) return `至少 ${row.streak_min} 个交易日`
  return '连续天数未知'
}
function topicName(name: string) {
  const suffix = name.match(/\s*[（(](\d{2})(\d{2})(\d{2})[)）]$/)
  if (!suffix || !suffix.index) return name
  const iso = `20${suffix[1]}-${suffix[2]}-${suffix[3]}`
  return cnDateFromUtc(iso + 'T00:00:00Z') === iso ? name.slice(0, suffix.index) : name
}

function SeatCard({ row, day, open }: { row: Seat; day: string; open: OpenStock }) {
  function stocks(items: Seat['core_stocks'], detailed = false) {
    return <ul className="divide-y divide-border/50">{items.map((stock, i) => <li key={(stock.symbol || stock.name) + i} className="py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {stock.symbol ? <button type="button" className={stockLink} onClick={() => open(stock.symbol!, stock.name, day)}>{stock.name}</button>
          : <span className="text-sm font-medium">{stock.name}</span>}
        {stock.role && <span className="text-xs text-secondary">{stock.role}</span>}
        <span className={'ml-auto font-mono text-sm tabular-nums ' + priceColorClass(stock.change_pct)}>{percent(stock.change_pct)}</span>
      </div>
      {detailed && <p className="mt-1 text-xs text-secondary">价格 {stock.price_yuan == null ? '—' : stock.price_yuan.toFixed(2) + ' 元'} · 核心分 {number(stock.core_score, 2)}</p>}
    </li>)}</ul>
  }
  return <li className="flex min-w-0 flex-col rounded-lg border border-border bg-surface p-4">
    <div className="flex items-baseline justify-between gap-3">
      <h3 title={row.name} className="min-w-0 flex-1 break-words text-lg font-semibold text-foreground">{topicName(row.name)}</h3>
      <span className="shrink-0 text-xs text-secondary">第 <b className="font-mono text-sm text-foreground">{row.rank}</b> 席</span>
    </div>
    <div className="mt-1 flex flex-wrap items-baseline justify-between gap-2 text-xs text-secondary">
      <span>{row.scope === 'strategy' ? '策略池' : row.scope === 'all' ? '全池' : '范围未提供'}</span>
      <span>综合分 <b className="font-mono text-sm font-medium text-foreground">{number(row.score, 2)}</b></span>
    </div>
    <p className="mt-2 flex flex-wrap gap-x-2 gap-y-1 text-xs text-secondary">
      <span className={row.new_entry ? 'text-accent' : ''}>{movement(row)}</span>
      {!(row.new_entry && row.streak === 1) && <span>· {streak(row)}</span>}
      {row.mainline_tag && <span title="来源提供的题材标签">· {row.mainline_tag}</span>}
    </p>
    <dl className="my-3 grid grid-cols-[1.2fr_1fr_.8fr] gap-2 border-y border-border/60 py-3">
      <div><dt className="text-xs text-secondary">平均涨幅</dt><dd className={'mt-1 font-mono text-xl font-semibold tabular-nums ' + priceColorClass(row.avg_change_pct)}>{percent(row.avg_change_pct)}</dd></div>
      <div><dt className="text-xs text-secondary">上涨比例</dt><dd className="mt-1 font-mono text-lg tabular-nums">{row.rising_pct == null ? '—' : number(row.rising_pct, 1) + '%'}</dd></div>
      <div><dt className="text-xs text-secondary">涨停</dt><dd className="mt-1 font-mono text-lg tabular-nums">{number(row.limit_up_count)}<span className="ml-1 font-sans text-xs text-secondary">只</span></dd></div>
    </dl>
    {((row.valid_count == null || row.member_count == null || row.valid_count < row.member_count) || (row.limit_down_count ?? 0) > 0) &&
      <p className="mb-2 text-xs text-warning">
        {row.valid_count == null || row.member_count == null ? '行情覆盖未提供' : row.valid_count < row.member_count ? `行情不完整 ${row.valid_count}/${row.member_count}` : ''}
        {(row.limit_down_count ?? 0) > 0 && <span className="ml-2">跌停 {row.limit_down_count} 只</span>}
      </p>}
    <h4 className="text-xs font-medium text-secondary">官网核心股</h4>
    {row.core_stocks.length === 0 ? <p className="py-2 text-sm text-secondary">来源暂未提供核心股。</p> : stocks(row.core_stocks.slice(0, 2))}
    <details className="mt-auto border-t border-border/60 pt-2 text-xs text-secondary">
      <summary className="w-fit cursor-pointer py-1 text-accent focus-visible:outline-accent">题材详情{row.core_stocks.length > 2 ? ` · 全部 ${row.core_stocks.length} 只核心股` : ''}</summary>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2">
        <div><dt className="inline">有效/成分 </dt><dd className="inline tabular-nums text-foreground">{number(row.valid_count)}/{number(row.member_count)}</dd></div>
        <div><dt className="inline">上涨 </dt><dd className="inline text-foreground">{number(row.rising_count)} 只</dd></div>
        <div><dt className="inline">强势 </dt><dd className="inline text-foreground">{number(row.strong_count)} 只</dd></div>
        <div><dt className="inline">跌停 </dt><dd className="inline text-foreground">{number(row.limit_down_count)} 只</dd></div>
        <div><dt className="inline">持续性分 </dt><dd className="inline text-foreground">{number(row.continuity_score, 2)}</dd></div>
      </dl>
      {row.new_entry && row.streak === 1 && <p className="mt-2">{streak(row)}</p>}
      {row.core_stocks.length > 0 && <div className="mt-3">{stocks(row.core_stocks, true)}</div>}
    </details>
  </li>
}

/** 指定时点只展示当时已经观察到的结果，包括来源计算和本地取数两个边界。 */
function matches(result: ZzshareHomeResult, day: string, cutoff?: string) {
  if (result.date !== day) return false
  if (!cutoff) return result.as_of == null
  const end = Date.parse(cutoff)
  if (Date.parse(result.as_of ?? '') !== end) return false
  if (result.rows.length === 0) return true
  const observed = Date.parse(result.fetched_at ?? '')
  return cnDateFromUtc(result.fetched_at) === day && Number.isFinite(observed) && observed <= end &&
    (result.source_at == null || Date.parse(result.source_at) <= end)
}

export function OfficialThemes({ day, live, open }: { day: string; live: boolean; open: OpenStock }) {
  const [selection, setSelection] = useState('')
  const [time, setTime] = useState('')
  const today = cnDateFromUtc(new Date().toISOString())
  const replay = selection !== ''
  const cutoff = selection === 'manual'
    ? (time ? `${day}T${time.length === 5 ? time + ':00' : time}+08:00` : undefined)
    : selection || undefined
  const future = !!cutoff && Date.parse(cutoff) > Date.now()
  const latest = useQuery({
    queryKey: QK.zzshareHome(day), queryFn: () => api.zzshareHome(day), enabled: !replay,
    refetchInterval: live && !replay ? 5 * 60_000 : false, refetchIntervalInBackground: false,
    staleTime: 60_000, retry: false,
  })
  const history = useQuery({
    queryKey: QK.zzshareHome(day, cutoff), queryFn: () => api.zzshareHome(day, cutoff),
    enabled: !!cutoff && !future, staleTime: 60_000, retry: false,
  })
  const query = replay ? history : latest
  const waiting = replay && (!cutoff || future)
  const result = !waiting && query.data && matches(query.data, day, cutoff) ? query.data : undefined
  const mismatch = !waiting && !!query.data && !result
  const points = latest.data?.date === day ? latest.data.points.filter(p => cnDateFromUtc(p) === day) : []
  const listLabel = day === today ? '当日最新榜' : '官方历史日榜'
  return <section aria-labelledby="official-rank-title" className="space-y-3">
    <h2 id="official-rank-title" className="sr-only">官网强势题材</h2>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-secondary">
        <span className="font-medium text-foreground">{replay ? '实录回看' : day !== today ? '官方历史日榜' : result?.mode === 'intraday' ? '盘中榜' : '官方日榜'}</span>
        {result && <span>行情快照 {result.market_snapshot_at || '未提供'}（北京）</span>}
        {replay && cutoff && <span>截至 {cnDateTimeFromUtc(cutoff)}（北京）</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1.5 text-xs text-secondary">查看时点
          <select className={control} value={selection} onChange={event => setSelection(event.target.value)}>
            <option value="">{listLabel}</option>
            {points.map(p => <option key={p} value={p}>{cnDateTimeFromUtc(p).slice(11)} 实录</option>)}
            <option value="manual">自选截止时间</option>
          </select>
        </label>
        {selection === 'manual' && <label className="flex items-center gap-1.5 text-xs text-secondary">截止时间（北京）
          <input type="time" step="1" value={time} className={control} onChange={e => setTime(e.target.value)} />
        </label>}
        <button type="button" className={control + ' inline-flex items-center gap-1 disabled:opacity-50'}
          disabled={waiting || query.isFetching} onClick={() => void query.refetch()}>
          <RefreshCw aria-hidden className={'h-3.5 w-3.5 ' + (query.isFetching ? 'animate-spin' : '')} />刷新
        </button>
      </div>
    </div>
    {waiting ? <p role="status" className="py-3 text-sm text-secondary">{future ? '截止时间晚于当前北京时间。' : '请选择要回看的北京时间。'}</p>
      : query.isPending ? <p role="status" className="py-3 text-sm text-secondary">正在读取官网席位…</p>
        : null}
    {(query.error || mismatch) && !waiting && <p role="alert" className="text-sm text-danger">
      {mismatch ? '榜单的日期或观测时间与所选时点不符，请刷新。' : query.error?.message}
    </p>}
    {result && <>
      {result.stale && <p role="status" className="text-sm text-warning">来源更新失败，沿用此前记录</p>}
      <details className="text-xs leading-relaxed text-secondary">
        <summary className="w-fit cursor-pointer py-1 focus-visible:outline-accent">数据详情与入选规则</summary>
        <p className="mt-2">按自在量化首页的席位顺序展示。{result.selection_rule === 'shared_strategy_top2_plus_all_top1' ? '策略池选前两席，再从全池补一席。' : '综合分与入选范围一起阅读。'}</p>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
          <a href="https://quant.zizizaizai.com/home" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">自在量化官网<ExternalLink aria-hidden className="h-3 w-3" /></a>
          <span>{day} · {replay ? '截至 ' + cnDateTimeFromUtc(cutoff).slice(11) : result.mode === 'intraday' ? '盘中榜' : '官方日榜'}</span>
          {result.state === 'ready' && <span>已返回 {result.rows.length}/3 席 · 策略池 {number(result.strategy_candidate_count)} 个 · 全部候选 {number(result.candidate_count)} 个</span>}
          {result.previous_date && <span>名次对照日 {result.previous_date}</span>}
        </p>
        <p className="flex flex-wrap gap-x-3">
          <span>源计算 {cnDateTimeFromUtc(result.source_at) || '—'}</span>
          <span>本地采集 {cnDateTimeFromUtc(result.fetched_at) || '—'}（北京）</span>
        </p>
        <p>交易时段服务约每 5 分钟记录一次。时点回看使用截止时间前的本地实录。</p>
      </details>
      {!replay && day !== today && <p className="text-xs text-secondary">历史日榜可能包含来源事后更新的结果；查看当时已知信息请切换到时点实录。</p>}
      {result.message && <p role="status" className={'text-xs leading-relaxed ' + (result.state === 'unavailable' ? 'text-warning' : 'text-secondary')}>{result.message}</p>}
      {result.state === 'ready' && result.rows.length > 0
        ? <>
          <ol aria-label="官网精选题材" className="grid items-start gap-3 lg:grid-cols-2 xl:grid-cols-3">{result.rows.map(row => <SeatCard key={row.tid} row={row} day={result.date} open={open} />)}</ol>
          {replay && <p className="text-xs text-secondary">核心股的价格取自本次实录。点击股票名称可查看以该交易日为终点的完整日 K。</p>}
        </>
        : <p className="rounded-lg border border-border px-4 py-6 text-center text-sm text-secondary">
          {result.state === 'ready' ? '该期官网榜单未选出题材。' : result.state === 'unavailable' ? '官网榜单暂不可用。' : replay ? '截至该时点没有可用的本地实录。' : '该交易日暂无官网榜单。'}
        </p>}
    </>}
  </section>
}
