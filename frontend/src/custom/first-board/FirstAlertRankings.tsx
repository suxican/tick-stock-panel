import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FirstBoardFirstAlertMethod, type FirstBoardFirstAlertRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { buttonClass, ErrorNotice, inputClass, LoadingRows, patternLabels, pct } from './shared'

const methods: Record<FirstBoardFirstAlertMethod, string> = {
  change: '涨幅', base: '不含人气', priority: '含人气', technical_baseline: '技术组原分', ma: '加均线', macd: '加 MACD', combined: '两项合用',
}
const methodKeys = Object.keys(methods) as FirstBoardFirstAlertMethod[]
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 })

function vote(value: boolean | null) {
  return value === true ? '符合' : value === false ? '不符' : '未知'
}

function popularityChange(row: FirstBoardFirstAlertRow, top: 3 | 5 | null) {
  const before = row.ranks.base?.day
  const after = row.ranks.priority?.day
  if (top == null || before == null || after == null) return null
  if (before > top && after <= top) return `纳入人气后换入前 ${top}`
  if (before <= top && after > top) return `纳入人气后移出前 ${top}`
  return null
}

function AlertEvidence({ row, reference }: { row: FirstBoardFirstAlertRow; reference: string | null }) {
  const info = row.popularity
  return <details className="mt-1.5 text-xs font-normal text-secondary">
    <summary className="cursor-pointer rounded py-0.5 focus-visible:outline focus-visible:outline-accent">首次依据</summary>
    <div className="mt-1 max-w-48 space-y-1 break-words leading-relaxed">
      <p>形态 {row.patterns.map(p => patternLabels[p]).join('、') || '未知'} · 代表形态 {row.pattern ? patternLabels[row.pattern] : '未知'}</p>
      <p>首次涨幅 {pct(row.change_pct)}。不含人气 {row.base_score ?? '—'} 分，含人气 {row.priority_score ?? '—'} 分。</p>
      {row.patterns.length > 1 && <p>两种方案各取当时得分最高的形态。两个总分的差值可能与下方代表形态的人气分不同。</p>}
      <p>当时人气 {info?.status === 'ready' && info.rank != null ? `第 ${info.rank} 名` : info?.status === 'unranked' ? '完整榜外' : row.popularity_known ? '已记录分项，名次未知' : '未知'}
        {info?.rank_gain != null && (info.rank_gain > 0 ? ` · 上升 ${info.rank_gain} 位` : info.rank_gain < 0 ? ` · 下降 ${-info.rank_gain} 位` : ' · 持平')}
        {info?.points != null && ` · ${info.points} / ${info.known_max ?? '—'} 分`}</p>
      <p>技术参考日 {reference ?? '未知'} · 均线 {vote(row.ma_support)} · MACD {vote(row.macd_support)}</p>
    </div>
  </details>
}

export function FirstAlertRankings({ day, poll, onOpen }: {
  day: string; poll: boolean; onOpen: (symbol: string, name: string, list: { symbol: string; name: string }[]) => void
}) {
  const [revision, setRevision] = useState<number | null>(null)
  const [top, setTop] = useState<3 | 5 | null>(3)
  const [offset, setOffset] = useState(0)
  const query = useQuery({
    queryKey: QK.firstBoardFirstAlertRankings(day, revision, top, offset),
    queryFn: () => api.firstBoardFirstAlertRankings(day, revision, top, offset),
    retry: false, refetchInterval: q => poll && (q.state.error as { status?: number } | null)?.status !== 404 ? 5000 : false,
  })
  const oldEndpoint = (query.error as { status?: number } | null)?.status === 404
  const result = query.data
  const report = result?.source === 'first_buy_candidate' && result.date === day && (revision == null || result.revision === revision)
    && result.offset === offset && result.top_n === top ? result : null
  const versions = report?.available_revisions ?? (revision != null ? [revision] : [])
  const open = (row: FirstBoardFirstAlertRow) => onOpen(row.symbol, row.name || row.symbol,
    report?.rows.map(item => ({ symbol: item.symbol, name: item.name || item.symbol })) ?? [])
  return <section className="min-w-0 space-y-2 border-y border-border py-3" aria-label="首次提示排名对照">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-sm font-semibold">首次提示排名对照</h3>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
        <label className="flex items-center gap-1">规则版本<select aria-label="排名规则版本" className={inputClass} value={revision ?? ''} onChange={e => { setOffset(0); setRevision(e.target.value === '' ? null : Number(e.target.value)) }}>
          <option value="">当日默认{revision == null && report ? `（${report.revision}）` : ''}</option>
          {versions.map(v => <option key={v} value={v}>版本 {v}</option>)}
        </select></label>
        <label className="flex items-center gap-1">每日名额<select aria-label="排名每日名额" className={inputClass} value={top ?? ''} onChange={e => { setOffset(0); setTop(e.target.value === '' ? null : Number(e.target.value) as 3 | 5) }}>
          <option value={3}>各组前 3</option><option value={5}>各组前 5</option><option value="">全部</option>
        </select></label>
      </div>
    </div>
    {query.isLoading && <LoadingRows />}
    {oldEndpoint ? <p className="text-xs text-secondary">当前服务未提供首次提示排名。</p> : <ErrorNotice error={query.error} retry={() => void query.refetch()} />}
    {query.isError && report && <p className="text-xs text-secondary">刷新失败，以下为上次读取的排名。</p>}
    {report && <>
      <p className="text-xs text-secondary">{day} · 版本 {report.revision} · 匹配 {report.matched} / 当日 {report.total} 只 · {report.coverage.batches} 个首次提示时点</p>
      <details className="text-xs leading-relaxed text-secondary">
        <summary className="cursor-pointer py-1 focus-visible:outline focus-visible:outline-accent">排名口径与证据覆盖</summary>
        <div className="space-y-1 pt-1">
          <p>全日每只股票首次买入候选计一次。先提示先占当日席位，同一时点按各方案排序。{top != null ? `当前显示任一方案入选前 ${top} 席的股票。` : '当前显示全部首次提示。'}</p>
          <p>当时涨幅可核验 {report.coverage.change_known} · 原优先分可核验 {report.coverage.priority_known} · 个股人气可核验 {report.coverage.popularity_known} · 技术证据齐全 {report.coverage.technical_known}。</p>
          <p>技术组只比较同有原分、上日均线及 MACD 证据的股票。四种技术排序在同批同形态、原分相近时比较。缺少相应证据的席位显示为 —。</p>
        </div>
      </details>
      {report.rows.length > 0 ? <div className="overflow-x-auto" role="region" tabIndex={0} aria-label="首次提示排名，可横向滚动">
        <table className="w-full min-w-[840px] text-left text-xs tabular-nums" aria-label="首次提示排名">
          <thead className="text-secondary"><tr>
            <th scope="col" className="sticky left-0 bg-surface p-2 font-normal">首次提示股票</th>
            {methodKeys.map(key => <th key={key} scope="col" className="p-2 font-normal">{methods[key]}</th>)}
          </tr></thead>
          <tbody>{report.rows.map(row => <tr key={row.symbol} className="border-t border-border">
            <th scope="row" className="sticky left-0 w-44 bg-surface p-2 align-top">
              <button type="button" className="text-left font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => open(row)}>{row.name || row.symbol}</button>
              <span className="block font-normal text-secondary">{row.symbol}</span>
              <time className="block font-normal text-secondary" dateTime={row.signal_at}>{Number.isFinite(Date.parse(row.signal_at)) ? clock.format(new Date(row.signal_at)) : '时间待核验'}</time>
              {popularityChange(row, top) && <span className="mt-1 block font-medium">{popularityChange(row, top)}</span>}
              <AlertEvidence row={row} reference={report.reference_date} />
            </th>
            {methodKeys.map(key => {
              const rank = row.ranks[key]
              return <td key={key} className="p-2 align-top">{rank
                ? <><span className={top != null && rank.day <= top ? 'font-semibold text-foreground' : 'text-secondary'}>第 {rank.day} 席</span><span className="mt-0.5 block text-secondary">同批 {rank.batch}/{rank.batch_total}</span></>
                : '—'}</td>
            })}
          </tr>)}</tbody>
        </table>
      </div> : <p className="text-sm text-secondary">{report.total === 0 ? '该交易日与版本未记录买入候选。' : '当前筛选或分页没有可展示的排名。'}</p>}
      {(report.matched > report.limit || offset > 0) && <nav className="flex flex-wrap items-center gap-2 text-xs" aria-label="排名分页">
        <button type="button" className={buttonClass} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - report.limit))}>上一页</button>
        <span>{report.rows.length ? `${offset + 1}–${offset + report.rows.length} / ${report.matched}` : `共 ${report.matched} 只`}</span>
        <button type="button" className={buttonClass} disabled={offset + report.limit >= report.matched} onClick={() => setOffset(offset + report.limit)}>下一页</button>
      </nav>}
    </>}
    {!query.isLoading && !query.isError && !report && <p className="text-xs text-secondary">当前日期与版本缺少可核验的排名数据。</p>}
  </section>
}
