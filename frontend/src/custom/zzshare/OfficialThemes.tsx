import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, RefreshCw } from 'lucide-react'
import { api, type ZzshareHomeResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnDateFromUtc, cnDateTimeFromUtc, priceColorClass } from '@/lib/format'
import type { OpenStock } from './ThemeRadar'

type Seat = ZzshareHomeResult['rows'][number]
const control = 'rounded-btn border border-border bg-base px-2 py-1.5 text-xs text-foreground focus-visible:outline-accent'

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
  return <li className="flex min-w-0 flex-col rounded-lg border border-border bg-surface p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-secondary">
      <span>官网第 <strong className="font-mono text-xl font-semibold text-foreground">{row.rank}</strong> 席</span>
      <span>{row.scope === 'strategy' ? '策略池' : row.scope === 'all' ? '全池' : '范围未提供'}</span>
    </div>
    <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
      <h3 title={row.name} className="break-words text-lg font-semibold text-foreground">{topicName(row.name)}</h3>
      <span className="whitespace-nowrap text-xs text-secondary">综合分 <b className="text-base tabular-nums text-foreground">{number(row.score, 2)}</b></span>
    </div>
    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-secondary">
      <span>{movement(row)}</span><span>{streak(row)}</span>
      {row.mainline_tag && <span className="text-accent">{row.mainline_tag}</span>}
    </div>
    <dl className="my-3 grid grid-cols-2 gap-x-3 gap-y-1.5 border-y border-border/60 py-3 text-xs">
      <div><dt className="inline text-secondary">平均涨幅 </dt><dd className={'inline tabular-nums ' + priceColorClass(row.avg_change_pct)}>{percent(row.avg_change_pct)}</dd></div>
      <div><dt className="inline text-secondary">有效/成分 </dt><dd className="inline tabular-nums">{number(row.valid_count)}/{number(row.member_count)}</dd></div>
      <div><dt className="inline text-secondary">上涨 </dt><dd className="inline tabular-nums">{number(row.rising_count)} 只{row.rising_pct != null && `（${number(row.rising_pct, 1)}%）`}</dd></div>
      <div><dt className="inline text-secondary">强势 </dt><dd className="inline tabular-nums">{number(row.strong_count)} 只</dd></div>
      <div><dt className="inline text-secondary">涨停 / 跌停 </dt><dd className="inline tabular-nums">{number(row.limit_up_count)} / {number(row.limit_down_count)}</dd></div>
      <div><dt className="inline text-secondary">持续性分 </dt><dd className="inline tabular-nums">{number(row.continuity_score, 2)}</dd></div>
    </dl>
    <h4 className="text-xs font-medium text-secondary">官网核心股</h4>
    {row.core_stocks.length === 0 ? <p className="mt-1 text-xs text-secondary">来源暂未提供核心股。</p>
      : <ul className="mt-1 divide-y divide-border/40">{row.core_stocks.map((stock, i) =>
        <li key={(stock.symbol || stock.name) + i} className="py-1.5 text-xs">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            {stock.symbol ? <button type="button" className="text-sm font-medium text-accent hover:underline"
              onClick={() => open(stock.symbol!, stock.name, day)}>{stock.name}</button> : <span className="text-sm font-medium">{stock.name}</span>}
            {stock.role && <span className="text-secondary">{stock.role}</span>}
            <span className={'ml-auto tabular-nums ' + priceColorClass(stock.change_pct)}>{percent(stock.change_pct)}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 text-secondary">
            <span>价格 {stock.price_yuan == null ? '—' : stock.price_yuan.toFixed(2) + ' 元'}</span>
            <span>核心分 {number(stock.core_score, 2)}</span>
          </div>
        </li>)}</ul>}
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
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 id="official-rank-title" className="text-lg font-semibold">官网强势题材</h2>
        <p className="mt-1 text-xs leading-relaxed text-secondary">按自在量化首页的席位顺序展示。{result?.selection_rule === 'shared_strategy_top2_plus_all_top1' ? '策略池选前两席，再从全池补一席。' : '综合分与入选范围一起阅读。'}</p>
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
      <div className="space-y-1 text-xs leading-relaxed text-secondary">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <a href="https://quant.zizizaizai.com/home" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">自在量化官网<ExternalLink aria-hidden className="h-3 w-3" /></a>
          <span>{day} · {replay ? '截至 ' + cnDateTimeFromUtc(cutoff).slice(11) : result.mode === 'intraday' ? '盘中榜' : '官方日榜'}</span>
          {result.stale && <strong className="text-warning">来源更新失败，沿用此前记录</strong>}
          {result.state === 'ready' && <span>已返回 {result.rows.length}/3 席 · 策略池 {number(result.strategy_candidate_count)} 个 · 全部候选 {number(result.candidate_count)} 个</span>}
          {result.previous_date && <span>名次对照日 {result.previous_date}</span>}
        </p>
        <p className="flex flex-wrap gap-x-3">
          <span>行情快照 {result.market_snapshot_at || '—'}</span>
          <span>源计算 {cnDateTimeFromUtc(result.source_at) || '—'}</span>
          <span>本地采集 {cnDateTimeFromUtc(result.fetched_at) || '—'}（北京）</span>
        </p>
      </div>
      {result.message && <p role="status" className={'text-xs leading-relaxed ' + (result.state === 'unavailable' ? 'text-warning' : 'text-secondary')}>{result.message}</p>}
      {result.state === 'ready' && result.rows.length > 0
        ? <>
          <ol aria-label="官网精选题材" className="grid gap-3 md:grid-cols-3">{result.rows.map(row => <SeatCard key={row.tid} row={row} day={result.date} open={open} />)}</ol>
          {replay && <p className="text-xs text-secondary">核心股的价格取自本次实录。点击股票名称可查看以该交易日为终点的完整日 K。</p>}
        </>
        : <p className="rounded-lg border border-border px-4 py-6 text-center text-sm text-secondary">
          {result.state === 'ready' ? '该期官网榜单未选出题材。' : result.state === 'unavailable' ? '官网榜单暂不可用。' : replay ? '截至该时点没有可用的本地实录。' : '该交易日暂无官网榜单。'}
        </p>}
    </>}
    <p className="text-xs leading-relaxed text-secondary">交易时段服务约每 5 分钟记录一次。时点回看使用截止时间前的本地实录。历史日榜可能包含来源事后更新的结果。</p>
  </section>
}
