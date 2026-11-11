import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type NewsAnnouncementRow, type NewsRiskLevel } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const action = 'min-h-9 rounded-btn border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/20 focus-visible:outline-accent disabled:opacity-50'
const link = 'text-foreground underline decoration-border underline-offset-4 hover:text-accent hover:decoration-accent focus-visible:outline-accent'
const levels: Record<NewsRiskLevel, [string, string]> = {
  high: ['高', 'bg-danger/15 text-danger'],
  medium: ['中', 'bg-warning/15 text-warning'],
  low: ['低', 'bg-elevated text-secondary'],
}

function when(row: NewsAnnouncementRow) {
  const day = row.published_at.slice(5, 10)
  return row.has_time ? `${day} ${row.published_at.slice(11, 16)}` : day
}

export function Announcements({ day, open }: { day: string; open: (row: NewsAnnouncementRow, list: NewsAnnouncementRow[]) => void }) {
  const client = useQueryClient()
  const [type, setType] = useState<string | null>(null)
  const query = useQuery({
    queryKey: QK.newsAnnouncements(day),
    queryFn: () => api.newsAnnouncements(day),
    refetchInterval: q => q.state.data?.sync?.state === 'running' ? 2000 : false,
  })
  const sync = useMutation({
    mutationFn: () => api.syncNewsAnnouncements(day),
    onSettled: () => client.invalidateQueries({ queryKey: QK.newsAnnouncements(day) }),
  })
  const data = query.data?.date === day ? query.data : undefined
  const running = data?.sync?.state === 'running' || sync.isPending
  const rows = data ? data.rows.filter(row => !type || row.type === type) : []
  const used = data ? data.types.filter(t => data.counts[t.type] > 0) : []

  if (query.isPending) return <p role="status" className="text-sm text-secondary">正在读取本地公告…</p>
  if (query.isError) return <p role="alert" className="text-sm text-danger">
    {query.error.message || '读取失败。'}
    <button type="button" className="ml-2 text-accent underline" onClick={() => void query.refetch()}>重试</button>
  </p>
  if (!data) return null
  if (data.symbols === 0) return <p className="text-sm text-secondary">
    公告排雷只检查自选股，当前自选为空。<Link className={'ml-1 ' + link} to="/watchlist">前往自选</Link>
  </p>
  const unsynced = data.missing.length + data.partial.length
  return <section aria-label="公告排雷" className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p role="status" className="text-xs text-secondary">
        自选 <b className="font-medium text-foreground">{data.symbols}</b> 只 · {data.start} 至 {data.date} · 风险公告 <b className="font-medium text-foreground">{data.rows.length}</b> 条
        {running && data.sync && <span className="ml-3">正在同步 {data.sync.done}/{data.sync.total}</span>}
      </p>
      <button type="button" className={action} disabled={running} onClick={() => sync.mutate()}>{running ? '正在同步…' : '同步公告'}</button>
    </div>
    {!running && unsynced > 0 && <p role="alert" className="text-sm text-warning">
      {data.missing.length > 0 && <>{data.missing.length} 只尚未取得公告</>}
      {data.missing.length > 0 && data.partial.length > 0 && '，'}
      {data.partial.length > 0 && <>{data.partial.length} 只未覆盖完整回看期</>}，结果可能遗漏，请同步公告。
    </p>}
    {(sync.error || (!running && data.sync?.state === 'error')) && <p role="alert" className="text-sm text-warning">
      {sync.error?.message || data.sync?.message}
    </p>}
    {sync.data && !sync.data.started && !running && <p className="text-xs text-secondary">{sync.data.message}</p>}
    {used.length > 0 && <div className="flex flex-wrap gap-1" role="group" aria-label="风险类型">
      {[{ type: null, label: '全部', count: data.rows.length }, ...used.map(t => ({ ...t, count: data.counts[t.type] }))].map(t =>
        <button type="button" key={t.type ?? 'all'} aria-pressed={type === t.type} onClick={() => setType(t.type)}
          className={'rounded-btn px-2.5 py-1 text-xs font-medium focus-visible:outline-accent ' + (type === t.type ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated hover:text-foreground')}>
          {t.label} {t.count}
        </button>)}
    </div>}
    {rows.length > 0
      ? <ul className="divide-y divide-border/60 rounded-lg border border-border bg-surface">
        {rows.map(row => <li key={row.id} className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 px-3 py-2.5 sm:grid-cols-[auto_6rem_minmax(0,1fr)_auto]">
          <span className={'h-fit rounded px-1.5 text-xs font-medium ' + levels[row.level][1]} title={row.label}>{levels[row.level][0]}</span>
          <span className="text-xs text-secondary sm:pt-0.5">{row.label}</span>
          <span className="col-span-2 min-w-0 text-sm sm:col-span-1">
            <button type="button" className={'mr-2 font-medium ' + link} onClick={() => open(row, rows)}>{row.name}</button>
            {row.url ? <a className="break-words hover:text-accent hover:underline" href={row.url} target="_blank" rel="noopener noreferrer">{row.title}</a> : row.title}
          </span>
          <time className="col-span-2 font-mono text-xs text-secondary sm:col-span-1 sm:pt-0.5 sm:text-right" dateTime={row.published_at}
            title={row.has_time ? undefined : '巨潮只给出公告日期'}>{when(row)}</time>
        </li>)}
      </ul>
      : unsynced < data.symbols && <p className="text-sm text-secondary">回看期内自选股没有命中风险规则的公告。</p>}
    <details className="text-xs text-secondary">
      <summary className="w-fit cursor-pointer py-1 focus-visible:outline-accent">数据与口径</summary>
      <div className="mt-2 max-w-prose space-y-1 leading-relaxed">
        <p>来源为巨潮资讯网，只检查自选股，回看约 20 个交易日。同步在后台按股查询；次日 6 点后取得的日期视为完整，不再重复拉取。</p>
        <p>按公告标题识别风险：{data.types.map(t => t.label).join('、')}。中介机构的核查意见、法律意见不计入。标题规则可能误判或遗漏，以公告原文为准。</p>
        <p>沪市、北交所和深市主板的公告多数只有日期，没有具体时刻。</p>
      </div>
    </details>
  </section>
}
