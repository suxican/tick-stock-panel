import { Fragment, useState } from 'react'
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
  plates_rank: [['plate_name', '板块'], ['source_score', '来源热度'], ['change_pct', '涨跌幅', 'pct'], ['net_flow_yuan', '净流入', 'yuan'], ['amount_yuan', '成交额', 'yuan'], ['updated_at', '源更新时间', 'time']],
  plates_list: [['plate_name', '板块'], ['plate_code', '板块代码'], ['date_in', '纳入日期'], ['date_out', '退出日期']],
  market_sentiment: [['trade_date', '日期'], ['open', '开盘'], ['high', '最高'], ['low', '最低'], ['close', '收盘'], ['previous_close', '前收']],
  sentiment_trend: [['time', '时刻'], ['value', '情绪序列 1'], ['aux_value', '情绪序列 2']],
  ths_hot_top: [['rank', '排名'], ['stock_name', '股票'], ['stock_code', '代码'], ['rank_change', '名次变动'], ['change_pct', '涨跌幅', 'pct'], ['price_yuan', '价格（元）'], ['updated_at', '源更新时间', 'time']],
  lhb_list: [['stock_name', '股票'], ['stock_code', '代码'], ['reason', '上榜原因'], ['change_pct', '涨跌幅', 'pct'], ['net_buy_yuan', '净买入', 'yuan'], ['amount_yuan', '成交额', 'yuan']],
  lhb_detail: [['reason_type', '上榜口径'], ['side', '方向', 'side'], ['rank', '排名'], ['seat_name', '席位'], ['buy_yuan', '买入金额', 'yuan'], ['sell_yuan', '卖出金额', 'yuan'], ['reason', '上榜原因']],
  lhb_stock_history: [['trade_date', '披露交易日'], ['change_pct', '涨跌幅', 'pct'], ['net_buy_yuan', '净买入', 'yuan']],
}
const views: Record<ResearchTab, readonly [ZzshareResearchKind, string][]> = {
  themes: [['plates_rank', '供应商板块榜'], ['plates_list', '当前板块目录']],
  sentiment: [['market_sentiment', '情绪日 K'], ['sentiment_trend', '日内情绪走势']],
  heat: [['ths_hot_top', '同花顺个股热榜']],
  lhb: [['lhb_list', '盘后龙虎榜']],
}
const captions: Record<ZzshareResearchKind, string> = {
  plates_rank: '按 ZZShare 指定日板块热度排序。来源热度与上方本地强度分分别计算。',
  plates_list: '供应商当前板块目录。切换交易日不会还原历史目录。',
  market_sentiment: '截至所选日近 30 个自然日的情绪点位。',
  sentiment_trend: '所选交易日的模型分时读数，数值单位为模型点位。',
  ths_hot_top: '按供应商返回名次展示。源更新时间与所选日不同的价格和涨跌幅显示为「—」。',
  lhb_list: '按所选交易日读取盘后公开榜单，选择股票可查看席位或历史上榜记录。',
  lhb_detail: '按上榜口径列示当日买卖席位。同一席位可出现在不同口径中。',
  lhb_stock_history: '仅列示截至所选交易日已披露的历史记录。',
}
const smallButton = 'rounded px-2 py-1 text-xs text-accent hover:bg-accent/10 focus-visible:outline-accent'
type Detail = { code: string; kind: 'lhb_detail' | 'lhb_stock_history'; name: string; rowKey: string }

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
  const measure = intraday ? 'value' : 'close'
  const time = intraday ? 'time' : 'trade_date'
  const points = rows.filter(row => typeof row[measure] === 'number' && typeof row[time] === 'string')
    .map(row => ({ value: Number(row[measure]), time: String(row[time]) })).sort((a, b) => a.time.localeCompare(b.time))
  if (points.length < 2) return null
  const min = Math.min(...points.map(p => p.value)), max = Math.max(...points.map(p => p.value))
  const line = points.map((p, i) => (i * 800 / (points.length - 1)).toFixed(2) + ',' +
    (max === min ? 50 : 98 - 96 * (p.value - min) / (max - min)).toFixed(2)).join(' ')
  const label = intraday ? '情绪序列 1' : '收盘情绪点位'
  return <figure className="my-3 border-y border-border/60 py-3 text-xs text-secondary">
    <figcaption className="mb-2 flex flex-wrap justify-between gap-2"><span>{label}</span><span>最低 {min} · 最高 {max}</span></figcaption>
    <svg viewBox="0 0 800 100" preserveAspectRatio="none" role="img" aria-label={label + '随观测时点变化'} className="h-28 w-full text-accent">
      <path d="M0 50H800" className="stroke-border" strokeDasharray="4 6" />
      <polyline points={line} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
    <div className="mt-1 flex justify-between tabular-nums"><span>{points[0].time}</span><span>{points.at(-1)?.time}</span></div>
  </figure>
}

function ResearchTable({ rows, kind, day, open }: { rows: Row[]; kind: ZzshareResearchKind; day: string; open: OpenStock }) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const withDetail = kind === 'lhb_list'
  function toggle(next: Detail) {
    setDetail(prev => prev?.rowKey === next.rowKey && prev.kind === next.kind ? null : next)
  }
  return <div className="max-h-[65vh] overflow-auto">
    <table className="w-full text-left text-xs tabular-nums">
      <caption className="sr-only">{captions[kind]}</caption>
      <thead className="sticky top-0 z-[1] bg-surface text-secondary"><tr>
        {columns[kind].map(([field, label]) => <th key={field} scope="col" className="whitespace-nowrap px-2.5 py-2 font-medium">{label}</th>)}
        {withDetail && <th scope="col" className="whitespace-nowrap px-2.5 py-2 font-medium">披露明细</th>}
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
              return <td key={field} className={'px-2.5 py-2.5 ' + (field === 'reason' || field === 'seat_name' ? 'min-w-36 max-w-xs break-words' : 'whitespace-nowrap') + ' ' + changes}>
                {field === 'stock_name' && typeof row.symbol === 'string' && row.symbol ? <button type="button" className="font-medium text-accent hover:underline"
                  onClick={() => open(String(row.symbol), name, String(row.trade_date ?? day))}>{name}</button> : fieldValue(row, column)}
              </td>
            })}
            {withDetail && <td className="whitespace-nowrap px-1">{code && (['lhb_detail', 'lhb_stock_history'] as const).map(id =>
              <button type="button" key={id} className={smallButton} aria-pressed={expanded && detail?.kind === id}
                aria-label={name + (id === 'lhb_detail' ? '席位明细' : '上榜历史')} onClick={() => toggle({ code, kind: id, name, rowKey })}>
                {id === 'lhb_detail' ? '席位' : '历史'}
              </button>)}</td>}
          </tr>
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
  return <>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="my-1 text-xs leading-relaxed text-secondary">{captions[kind]}</p>
      <button type="button" disabled={query.isFetching} className={smallButton + ' inline-flex items-center gap-1 disabled:opacity-50'} onClick={() => void query.refetch()}>
        <RefreshCw aria-hidden className={'h-3 w-3 ' + (query.isFetching ? 'animate-spin' : '')} />刷新
      </button>
    </div>
    {query.isPending && <p role="status" className="py-5 text-sm text-secondary">正在读取 ZZShare 数据…</p>}
    {(query.error || (query.data && !result)) && <p role="alert" className="py-3 text-sm text-danger">{query.error?.message || '数据与所选日期不一致，请刷新。'}</p>}
    {result && <>
      <p className="mb-3 text-xs leading-relaxed text-secondary">
        来源 ZZShare · {kind === 'plates_list' ? '当前目录' : day}
        {result.fetched_at && <> · 采集于 {cnDateTimeFromUtc(result.fetched_at) || result.fetched_at}（北京时间）</>}
        {result.stale && <strong className="ml-2 text-warning">使用缓存，数据有延迟</strong>}
      </p>
      {result.warning && <p role="status" className="mb-2 text-xs text-warning">{result.warning}</p>}
      {result.rows.length === 0 ? <p className="py-5 text-sm text-secondary">暂无可展示记录。</p> : <>
        {(kind === 'market_sentiment' || kind === 'sentiment_trend') && <SentimentChart rows={result.rows} intraday={kind === 'sentiment_trend'} />}
        <ResearchTable key={kind} rows={result.rows} kind={kind} day={day} open={open} />
      </>}
    </>}
  </>
}

export function ResearchPanel({ tab, day, open, live }: { tab: ResearchTab; day: string; open: OpenStock; live: boolean }) {
  const options = views[tab]
  const [kind, setKind] = useState(options[0][0])
  const [plateType, setPlateType] = useState<PlateType>(17)
  return <section aria-label={options[0][1]} className="min-w-0 rounded-card border border-border bg-surface p-4 sm:p-5">
    {options.length === 1 ? <h2 className="mb-2 text-base font-semibold">{options[0][1]}</h2>
      : <div className="mb-2 flex gap-4 border-b border-border">{options.map(([id, label]) =>
        <button type="button" key={id} aria-pressed={kind === id} onClick={() => setKind(id)}
          className={'border-b-2 pb-2 text-sm font-medium ' + (kind === id ? 'border-accent text-foreground' : 'border-transparent text-secondary')}>{label}</button>
      )}</div>}
    {tab === 'themes' && <div aria-label="板块类型" role="group" className="mb-2 flex gap-1">
      {([[17, '题材'], [14, '行业'], [15, '概念']] as const).map(([id, label]) =>
        <button type="button" key={id} onClick={() => setPlateType(id)} aria-pressed={plateType === id}
          className={'rounded px-2.5 py-1 text-xs font-medium ' + (plateType === id ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated')}>{label}</button>
      )}
    </div>}
    <Resource key={kind} kind={kind} day={day} plateType={tab === 'themes' ? plateType : undefined} open={open} live={live} />
  </section>
}
