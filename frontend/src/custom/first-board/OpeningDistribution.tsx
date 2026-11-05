import { useId } from 'react'
import type { FirstBoardFeedback, FirstBoardOpeningDistribution } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPct } from '@/lib/format'

const gaugeArc = 'M 25.2 82.1 A 53 53 0 1 1 124.8 82.1'

function OpeningGauge({ label, caption, value, limit, muted }: {
  label: string
  caption: string
  value: number | null
  limit: number
  muted: boolean
}) {
  const maskId = useId()
  const negative = value != null && value < 0
  const progress = Math.min(100, Math.abs(value ?? 0) / limit * 100)
  const maximum = `${Math.round(limit * 100)}%`
  const display = fmtPct(value).replace(/^\+/, '')
  const tone = muted || !value ? 'text-secondary' : negative ? 'text-bear' : 'text-bull'

  return <figure aria-label={`${label} ${display}（${caption}）`} className="mx-auto w-full max-w-36 text-center">
    <svg aria-hidden="true" viewBox="0 0 150 107" className="block w-full tabular-nums">
      <defs>
        <mask id={maskId} maskUnits="userSpaceOnUse" x="0" y="0" width="150" height="107">
          <path d={gaugeArc} fill="none" stroke="white" strokeWidth="14" pathLength="100" strokeDasharray="9.5 0.5" />
        </mask>
      </defs>
      <g fill="none" strokeWidth="15" mask={`url(#${maskId})`}>
        <path d={gaugeArc} className="stroke-muted/55" />
        {progress > 0 && <path d={gaugeArc} className={cn('stroke-current', tone)} pathLength="100"
          strokeDasharray={`${progress} 100`} transform={negative ? 'translate(150 0) scale(-1 1)' : undefined} />}
      </g>
      <text x="75" y="59" textAnchor="middle" className={cn('fill-current text-[19px] font-semibold', tone)}>{display}</text>
      <text x="75" y="79" textAnchor="middle" className="fill-foreground text-[13px]">{label}</text>
      <g className="fill-secondary text-[11px]" textAnchor="middle">
        <text x="24" y="100">{negative ? `-${maximum}` : '0%'}</text>
        <text x="126" y="100">{negative ? '0%' : maximum}</text>
      </g>
    </svg>
    <figcaption className="text-[11px] leading-tight text-secondary">{caption}</figcaption>
  </figure>
}

function OpeningBars({ bins, muted }: { bins: FirstBoardOpeningDistribution['bins']; muted: boolean }) {
  const peak = Math.max(1, ...bins.map(bin => bin.count))
  // The 13 server-defined bins are ordered from the positive tail to the negative tail, with flat in the middle.
  const flatIndex = Math.floor(bins.length / 2)
  return <dl aria-label="各开盘涨幅区间股票数" className="grid min-w-0 grid-cols-[max-content_minmax(0,1fr)] items-stretch text-xs tabular-nums">
    {bins.map(({ label, count }, index) => {
      if (count === 0 && (index === 0 || index === bins.length - 1)) return null
      const tone = muted || index === flatIndex ? 'text-muted' : index < flatIndex ? 'text-bull' : 'text-bear'
      return <div key={label} className="contents">
        <dt className="flex items-center justify-end whitespace-nowrap py-0.5 pr-2 text-secondary">{label}</dt>
        <dd className="flex min-h-5 items-center border-l border-border pr-7">
          <span className={cn('relative h-2.5 bg-current', tone)} style={{ width: `${count / peak * 100}%` }}>
            <span className="absolute left-full top-1/2 -translate-y-1/2 pl-1.5 text-xs text-secondary">{count}</span>
          </span>
        </dd>
      </div>
    })}
  </dl>
}

export function OpeningDistribution({ feedback, refreshFailed = false }: {
  feedback?: Pick<FirstBoardFeedback, 'opening_distribution' | 'status'>
  refreshFailed?: boolean
}) {
  const titleId = useId()
  const data = feedback?.opening_distribution
  const stale = feedback?.status === 'stale'
  const muted = refreshFailed || stale
  const notes: string[] = []
  if (refreshFailed && data) notes.push('刷新失败，显示上次统计')
  else if (stale && data) notes.push('行情已过期，显示上次统计')
  if (data && data.open_count < data.sample_count) notes.push(`有效开盘 ${data.open_count} / ${data.sample_count} 只`)
  if (data?.history_excluded_count) notes.push(`历史待确认 ${data.history_excluded_count} 只`)

  return <section aria-labelledby={titleId} className="min-w-0">
    <h3 id={titleId} className="text-sm font-semibold leading-relaxed">
      昨日非一字涨停{data && <> <span className={cn('tabular-nums', !muted && 'text-bull')}>{data.sample_count} 只</span></>} · 今日开盘涨幅分布
    </h3>
    {data && data.open_count > 0 ? <div className="mt-3 grid grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)] items-stretch gap-x-2 sm:gap-x-5">
      <OpeningBars bins={data.bins} muted={muted} />
      <div className="flex min-w-0 flex-col justify-around gap-2">
        <OpeningGauge label="打板成功率" caption="开盘上涨数量比例" value={data.up_ratio} limit={1} muted={muted} />
        <OpeningGauge label="打板盈利率" caption="开盘平均涨跌幅" value={data.mean_open_premium}
          limit={Math.max(1, Math.ceil(Math.abs(data.mean_open_premium ?? 0) * 10)) / 10} muted={muted} />
      </div>
    </div> : <p className="py-5 text-sm text-secondary">
      {data ? (data.sample_count ? '等待有效开盘价。' : '昨日暂无可确认的非一字涨停样本。')
        : feedback?.status === 'waiting' ? '正在准备昨日非一字涨停样本…' : '暂无开盘涨幅分布数据。'}
    </p>}
    {notes.length > 0 && <p className="mt-2 text-xs leading-relaxed text-secondary">{notes.join(' · ')}</p>}
  </section>
}
