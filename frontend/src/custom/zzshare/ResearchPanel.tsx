import { Fragment, useState, type PointerEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api, type ZzshareResearchKind, type ZzshareResearchResult } from '@/lib/api'
import { cnDateTimeFromUtc, fmtVolume, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import type { OpenStock } from './ThemeRadar'

export type ResearchTab = 'themes' | 'sentiment' | 'heat' | 'lhb'
type PlateType = 14 | 15 | 17
type Row = ZzshareResearchResult['rows'][number]
type Column = readonly [field: string, label: string, format?: 'pct' | 'yuan' | 'time' | 'side']
const columns: Record<ZzshareResearchKind, readonly Column[]> = {
  plates_rank: [['plate_name', '板块'], ['source_score', '来源热度'], ['change_pct', '涨跌幅', 'pct'], ['net_flow_yuan', '净流入', 'yuan']],
  plates_list: [['plate_name', '板块'], ['plate_code', '板块代码'], ['date_in', '纳入日期'], ['date_out', '退出日期']],
  market_sentiment: [['trade_date', '日期'], ['open', '开盘'], ['high', '最高'], ['low', '最低'], ['close', '收盘'], ['previous_close', '前收']],
  sentiment_trend: [['time', '时刻'], ['value', '情绪序列 1'], ['aux_value', '情绪序列 2']],
  ths_hot_top: [['rank', '排名'], ['stock_name', '股票'], ['rank_change', '名次变动'], ['change_pct', '涨跌幅', 'pct']],
  lhb_list: [['stock_name', '股票'], ['change_pct', '涨跌幅', 'pct'], ['net_buy_yuan', '净买入', 'yuan'], ['reason', '上榜原因']],
  lhb_detail: [['reason_type', '上榜口径'], ['side', '方向', 'side'], ['rank', '排名'], ['seat_name', '席位'], ['buy_yuan', '买入金额', 'yuan'], ['sell_yuan', '卖出金额', 'yuan'], ['reason', '上榜原因']],
  lhb_stock_history: [['trade_date', '披露交易日'], ['change_pct', '涨跌幅', 'pct'], ['net_buy_yuan', '净买入', 'yuan']],
}
const additionalColumns: Partial<Record<ZzshareResearchKind, readonly Column[]>> = {
  plates_rank: [['amount_yuan', '成交额', 'yuan'], ['updated_at', '源更新时间', 'time']],
  ths_hot_top: [['price_yuan', '价格（元）'], ['updated_at', '源更新时间', 'time']],
  lhb_list: [['reason', '完整上榜原因'], ['amount_yuan', '成交额', 'yuan']],
}
const views: Record<ResearchTab, readonly [ZzshareResearchKind, string][]> = {
  themes: [['plates_rank', '供应商板块榜'], ['plates_list', '当前板块目录']],
  sentiment: [['market_sentiment', '日度情绪'], ['sentiment_trend', '日内情绪走势']],
  heat: [['ths_hot_top', '同花顺个股热榜']],
  lhb: [['lhb_list', '盘后龙虎榜']],
}
const captions: Record<ZzshareResearchKind, string> = {
  plates_rank: '按 ZZShare 指定日板块热度排序。来源热度与本地板块强度分分别计算。',
  plates_list: '供应商当前板块目录。切换交易日不会还原历史目录。',
  market_sentiment: '截至所选日近 30 个自然日的情绪点位。',
  sentiment_trend: '所选交易日的模型分时读数，数值单位为模型点位。',
  ths_hot_top: '按供应商返回名次展示。源更新时间与所选日不同的价格和涨跌幅显示为「—」。',
  lhb_list: '按所选交易日读取盘后公开榜单，选择股票可查看席位或历史上榜记录。',
  lhb_detail: '按上榜口径列示当日买卖席位。同一席位可出现在不同口径中。',
  lhb_stock_history: '仅列示截至所选交易日已披露的历史记录。',
}
const smallButton = 'rounded px-2 py-1.5 text-xs font-medium text-foreground hover:bg-elevated focus-visible:outline-accent disabled:opacity-50'
type Detail = { code: string; kind: 'lhb_detail' | 'lhb_stock_history'; name: string; rowKey: string }

function numericColumn([field, , format]: Column) {
  return format === 'pct' || format === 'yuan' || ['source_score', 'rank', 'rank_change', 'open', 'high', 'low', 'close', 'previous_close', 'value', 'aux_value', 'price_yuan'].includes(field)
}

function fieldValue(row: Row, [field, , format]: Column) {
  const value = row[field]
  if (value == null || value === '') return '—'
  if (format === 'side') return value === 'buy' ? '买方' : value === 'sell' ? '卖方' : String(value)
  if (format === 'time') return cnDateTimeFromUtc(String(value)) || String(value)
  if (format === 'pct' && typeof value === 'number') return (value > 0 ? '+' : '') + value.toFixed(2) + '%'
  if (format === 'yuan' && typeof value === 'number') return fmtVolume(value) + '元'
  return String(value)
}

function SentimentChart({ rows, intraday }: { rows: Row[]; intraday: boolean }) {
  const [selectedTime, setSelectedTime] = useState<string | null>(null)
  const measure = intraday ? 'value' : 'close'
  const time = intraday ? 'time' : 'trade_date'
  const points = rows.filter(row => typeof row[measure] === 'number' && Number.isFinite(row[measure]) && typeof row[time] === 'string')
    .map(row => ({ value: Number(row[measure]), time: String(row[time]), previous: row.previous_close })).sort((a, b) => a.time.localeCompare(b.time))
  if (!points.length) return <p className="py-6 text-sm text-secondary">暂无有效情绪点位，可展开原始明细查看。</p>
  const latest = points.at(-1)!
  const previous = intraday ? points.at(-2)?.value : latest.previous
  const delta = typeof previous === 'number' && Number.isFinite(previous) ? latest.value - previous : null
  const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  const selectedIndex = points.findIndex(p => p.time === selectedTime)
  const index = selectedIndex < 0 ? points.length - 1 : selectedIndex
  const selected = points[index]
  const min = Math.min(...points.map(p => p.value)), max = Math.max(...points.map(p => p.value))
  const padding = max === min ? Math.max(Math.abs(max) * .01, 1) : (max - min) * .1
  const top = max + padding, bottom = min - padding
  const x = (i: number) => points.length === 1 ? 400 : i * 800 / (points.length - 1)
  const y = (value: number) => 160 * (top - value) / (top - bottom)
  const line = points.map((p, i) => `${x(i)},${y(p.value)}`).join(' ')
  const label = intraday ? '情绪序列 1' : '收盘情绪点位'
  function selectFromPointer(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width) setSelectedTime(points[Math.max(0, Math.min(points.length - 1, Math.round((event.clientX - box.left) / box.width * (points.length - 1))))].time)
  }
  return <figure className="my-4 text-xs text-secondary">
    <figcaption className="mb-6 flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <span>{label}</span><strong className="text-2xl font-semibold tabular-nums text-foreground">{number(latest.value)} <span className="text-xs font-normal text-secondary">点</span></strong>
      <span>{intraday ? '较上一观测' : '较前收'} {delta == null ? '—' : `${delta > 0 ? '+' : ''}${number(delta)} 点`}</span>
      <span className="ml-auto">最新观测 {latest.time}</span>
    </figcaption>
    <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-2">
      <div className="flex h-48 flex-col justify-between text-right tabular-nums sm:h-56" aria-hidden="true">
        {[top, (top + bottom) / 2, bottom].map((value, i) => <span key={i}>{number(value)}</span>)}
      </div>
      <svg viewBox="0 0 800 160" preserveAspectRatio="none" role="slider" tabIndex={0}
        aria-label={label + '，使用左右方向键查看观测点'} aria-valuemin={0} aria-valuemax={points.length - 1} aria-valuenow={index}
        aria-valuetext={`${selected.time}，${number(selected.value)} 点`}
        className="h-48 w-full cursor-crosshair overflow-visible text-accent focus-visible:outline-accent sm:h-56"
        onPointerDown={selectFromPointer} onPointerMove={selectFromPointer}
        onPointerLeave={event => { if (event.pointerType === 'mouse') setSelectedTime(null) }}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : Math.max(0, Math.min(points.length - 1, index + (event.key === 'ArrowLeft' ? -1 : 1)))
          setSelectedTime(points[next].time)
        }}>
        {[0, 80, 160].map(pos => <path key={pos} d={`M0 ${pos}H800`} className="stroke-border" strokeDasharray="4 6" vectorEffect="non-scaling-stroke" />)}
        <polyline points={line} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        <path d={`M${x(index)} 0V160`} className="stroke-secondary" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
        <circle cx={x(index)} cy={y(selected.value)} r="3" fill="currentColor" />
      </svg>
      <div className="col-start-2 mt-2 flex justify-between gap-2 tabular-nums"><span>{points[0].time}</span><span>{latest.time}</span></div>
    </div>
    <div className="mt-4 flex flex-wrap justify-between gap-2">
      <output className="tabular-nums text-foreground">{selected.time} · {number(selected.value)} 点</output>
      <span>悬停、触摸或方向键查看读数 · 单位：模型点位</span>
    </div>
  </figure>
}

function ResearchTable({ rows, kind, day, open }: { rows: Row[]; kind: ZzshareResearchKind; day: string; open: OpenStock }) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const withDetail = kind === 'lhb_list'
  const extraColumns = additionalColumns[kind]
  function toggle(next: Detail) {
    setDetail(prev => prev?.rowKey === next.rowKey && prev.kind === next.kind ? null : next)
  }
  return <div className="min-w-0 overflow-x-auto">
    <table className="w-full text-left text-sm tabular-nums">
      <caption className="sr-only">{captions[kind]}</caption>
      <thead className="bg-surface text-xs text-secondary"><tr>
        {columns[kind].map(column => <th key={column[0]} scope="col" className={'whitespace-nowrap px-2.5 py-3 font-medium ' + (numericColumn(column) ? 'text-right' : '')}>{column[1]}</th>)}
        {(withDetail || extraColumns) && <th scope="col" className="whitespace-nowrap px-2.5 py-3 text-right font-medium">查看</th>}
      </tr></thead>
      <tbody>{rows.map((row, index) => {
        const code = String(row.stock_code ?? '')
        const name = String(row.stock_name ?? code)
        const rowKey = [row.trade_date, row.time, row.plate_code, code, row.reason, index].join('-')
        const expanded = withDetail && !!code && detail?.rowKey === rowKey
        return <Fragment key={rowKey}>
          <tr className="border-t border-border/70 hover:bg-elevated/35">
            {columns[kind].map(column => {
              const [field, , format] = column
              const changes = format === 'pct' && typeof row[field] === 'number' ? priceColorClass(row[field]) : ''
              const numeric = numericColumn(column)
              return <td key={field} className={'px-2.5 py-3 ' + (field === 'reason' || field === 'seat_name' ? 'min-w-36 max-w-xs break-words' : 'whitespace-nowrap') + (numeric ? ' text-right ' : ' ') + changes}>
                {field === 'stock_name' ? <>
                  {typeof row.symbol === 'string' && row.symbol ? <button type="button" className="font-medium text-foreground underline decoration-border underline-offset-4 hover:text-accent focus-visible:outline-accent"
                    onClick={() => open(String(row.symbol), name, String(row.trade_date ?? day))}>{name}</button> : <span className="font-medium">{name || '—'}</span>}
                  <span className="mt-0.5 block text-xs text-secondary">{code || '—'}</span>
                </> : field === 'reason' && withDetail ? <span className="line-clamp-1">{fieldValue(row, column)}</span> : fieldValue(row, column)}
              </td>
            })}
            {(withDetail || extraColumns) && <td className="whitespace-nowrap px-1 text-right">
              {extraColumns && <button type="button" className={smallButton} aria-label={(name || String(row.plate_name ?? '')) + '详情'} aria-expanded={expandedRow === rowKey}
                onClick={() => setExpandedRow(prev => prev === rowKey ? null : rowKey)}>详情</button>}
              {withDetail && code && (['lhb_detail', 'lhb_stock_history'] as const).map(id =>
              <button type="button" key={id} className={smallButton} aria-pressed={expanded && detail?.kind === id}
                aria-label={name + (id === 'lhb_detail' ? '席位明细' : '上榜历史')} onClick={() => toggle({ code, kind: id, name, rowKey })}>
                {id === 'lhb_detail' ? '席位' : '历史'}
              </button>)}</td>}
          </tr>
          {expandedRow === rowKey && extraColumns && <tr><td colSpan={columns[kind].length + 1} className="border-t border-border/60 bg-elevated/40 p-3">
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">{extraColumns.map(column => <div key={column[0]}>
              <dt className="mb-1 text-xs text-secondary">{column[1]}{column[2] === 'time' ? '（北京）' : ''}</dt>
              <dd className="whitespace-normal break-words">{fieldValue(row, column)}</dd>
            </div>)}</dl>
          </td></tr>}
          {expanded && detail && <tr><td colSpan={columns[kind].length + 1} className="border-y border-border bg-base p-3">
            <div className="mb-1 text-sm font-medium">{detail.name} · {detail.kind === 'lhb_detail' ? '席位明细' : '上榜历史'}</div>
            <Resource key={code + detail.kind} kind={detail.kind} day={day} code={code} open={open} />
          </td></tr>}
        </Fragment>
      })}</tbody>
    </table>
  </div>
}

function Resource({ kind, day, code, plateType, open, live = false }: { kind: ZzshareResearchKind; day: string; code?: string; plateType?: PlateType; open: OpenStock; live?: boolean }) {
  const poll = live && ['plates_rank', 'ths_hot_top', 'market_sentiment', 'sentiment_trend'].includes(kind)
  const query = useQuery({
    queryKey: QK.zzshareResearch(kind, day, code, plateType),
    queryFn: () => plateType ? api.zzshareResearch(kind, day, code, plateType) : api.zzshareResearch(kind, day, code),
    retry: false, staleTime: 60_000, refetchInterval: poll ? 5 * 60_000 : false,
  })
  const result = query.data?.date === day && query.data.kind === kind ? query.data : undefined
  const sentiment = kind === 'market_sentiment' || kind === 'sentiment_trend'
  return <>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-secondary">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>{kind === 'plates_list' ? '当前目录' : day} · ZZShare</span>
        {result && <span>{result.rows.length} 条记录</span>}
        {result?.stale && <strong className="font-medium text-warning">使用缓存，数据有延迟</strong>}
        {query.isError && result && <strong className="font-medium text-warning">刷新失败，沿用此前记录</strong>}
      </div>
      <button type="button" disabled={query.isFetching} className={smallButton + ' inline-flex items-center gap-1'} onClick={() => void query.refetch()}>
        <RefreshCw aria-hidden className={'h-3 w-3 ' + (query.isFetching ? 'animate-spin' : '')} />刷新
      </button>
    </div>
    {kind === 'plates_list' && <p className="mb-3 text-xs leading-relaxed text-secondary">{captions[kind]}</p>}
    {kind === 'lhb_list' && <p className="mb-3 text-xs text-secondary">按所选交易日的盘后披露记录展示。</p>}
    {query.isPending && <div role="status" className="space-y-3 py-5 text-sm text-secondary">
      <p>正在读取 ZZShare 数据…</p><div aria-hidden className="h-10 rounded bg-elevated/50" /><div aria-hidden className="h-10 rounded bg-elevated/50" />
    </div>}
    {(query.error || (query.data && !result)) && <p role="alert" className="flex flex-wrap items-center gap-2 py-3 text-sm text-danger">
      {query.error?.message || '数据与所选日期不一致，请刷新。'}
      <button type="button" className={smallButton + ' underline underline-offset-4'} disabled={query.isFetching} onClick={() => void query.refetch()}>重试</button>
    </p>}
    {result && <>
      {result.warning && <p role="status" className="mb-2 text-xs text-warning">{result.warning}</p>}
      {result.rows.length === 0 ? <p className="py-8 text-center text-sm text-secondary">{kind === 'lhb_list' ? '所选交易日暂无龙虎榜记录。' : '暂无可展示记录。'}</p>
        : sentiment ? <>
          <SentimentChart rows={result.rows} intraday={kind === 'sentiment_trend'} />
          <details className="border-t border-border/60 py-3">
            <summary className="w-fit cursor-pointer text-sm font-medium text-foreground focus-visible:outline-accent">原始明细（{result.rows.length}）</summary>
            <div className="mt-3"><ResearchTable key={kind} rows={result.rows} kind={kind} day={day} open={open} /></div>
          </details>
        </> : <ResearchTable key={kind} rows={result.rows} kind={kind} day={day} open={open} />}
      <details className="mt-3 border-t border-border/60 pt-3 text-xs leading-relaxed text-secondary">
        <summary className="w-fit cursor-pointer font-medium focus-visible:outline-accent">数据详情</summary>
        <div className="mt-2 space-y-1">
          <p>{captions[kind]}</p>
          <p>本地采集 {cnDateTimeFromUtc(result.fetched_at) || '—'}（北京）；采集时间不代表行情时点。</p>
        </div>
      </details>
    </>}
  </>
}

export function ResearchPanel({ tab, day, open, live }: { tab: ResearchTab; day: string; open: OpenStock; live: boolean }) {
  const options = views[tab]
  const [kind, setKind] = useState(options[0][0])
  const [plateType, setPlateType] = useState<PlateType>(17)
  return <section aria-label={options[0][1]} className="min-w-0 rounded-card border border-border bg-surface p-4 sm:p-5">
    {tab === 'sentiment' && <div className="mb-3 flex gap-4 border-b border-border">{options.map(([id, label]) =>
        <button type="button" key={id} aria-pressed={kind === id} onClick={() => setKind(id)}
          className={'border-b-2 pb-2 text-sm font-medium focus-visible:outline-accent ' + (kind === id ? 'border-accent text-foreground' : 'border-transparent text-secondary hover:text-foreground')}>{label}</button>
      )}</div>}
    {tab === 'themes' && <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <div aria-label="板块类型" role="group" className="flex gap-1">
        {([[17, '题材'], [14, '行业'], [15, '概念']] as const).map(([id, label]) =>
          <button type="button" key={id} onClick={() => setPlateType(id)} aria-pressed={plateType === id}
            className={'rounded px-3 py-1.5 text-sm font-medium focus-visible:outline-accent ' + (plateType === id ? 'bg-elevated text-foreground' : 'text-secondary hover:bg-elevated')}>{label}</button>
        )}
      </div>
      <button type="button" className={smallButton} onClick={() => setKind(kind === 'plates_list' ? 'plates_rank' : 'plates_list')}>
        {kind === 'plates_list' ? '返回热榜' : '当前板块目录'}
      </button>
    </div>}
    <Resource key={kind} kind={kind} day={day} plateType={tab === 'themes' ? plateType : undefined} open={open} live={live} />
  </section>
}
