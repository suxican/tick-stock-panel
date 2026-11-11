import { useId, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Check, ChevronDown, ChevronUp, Clock, X } from 'lucide-react'
import type { FirstBoardSnapshot } from '@/lib/api'
import { cn } from '@/lib/cn'
import { DataStatus, dataStatusSummary } from './DataStatus'
import { panelClass, stamp } from './shared'

type Tone = 'ok' | 'bad' | 'warn' | 'neutral'
const chipClass = 'inline-flex min-h-7 items-center gap-1.5 whitespace-nowrap rounded-btn border px-2 text-xs'
const toneClass: Record<Tone, string> = {
  ok: 'border-border text-foreground',
  bad: 'border-danger/40 bg-danger/5 text-foreground',
  warn: 'border-amber-500/40 text-foreground',
  neutral: 'border-border text-secondary',
}
const toneIcon: Record<Tone, ReactNode> = {
  ok: <Check className="h-3.5 w-3.5 text-accent" aria-hidden="true" />,
  bad: <X className="h-3.5 w-3.5 text-danger" aria-hidden="true" />,
  warn: <AlertTriangle className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" aria-hidden="true" />,
  neutral: <Clock className="h-3.5 w-3.5 text-muted" aria-hidden="true" />,
}
const realtimeChips = { available: ['ok', '实时行情可用'], blocked: ['bad', '实时行情受阻'], unknown: ['neutral', '实时行情待确认'] } as const

function Chip({ tone, title, children }: { tone: Tone; title?: string; children: ReactNode }) {
  return <span className={cn(chipClass, toneClass[tone])} title={title}>{toneIcon[tone]}{children}</span>
}

/**
 * 盯盘闸门：回答"现在能不能提示"。全局阻断原因只在这里出现一次，候选行不再逐行重复；
 * 9 项数据覆盖默认收成一个标签，需要时展开查看口径与缺失原因。
 */
export function LiveStatus({ data, retained }: { data: FirstBoardSnapshot; retained: boolean }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const status = data.data_status?.version === 1 ? data.data_status : undefined
  const summary = dataStatusSummary(status)
  const realtime = status && realtimeChips[status.realtime_status]
  const reasons = [...new Set(data.global_blocked_reasons ?? [])].filter(reason => reason !== data.message)
  // 盯盘中只在仍有全局阻断时提示，正常状态不重复说明文字。
  const showMessage = data.status !== 'ready'
  const env = data.environment
  const ladder = env.ladder
  const live = data.market_ladder
  const whole = (value?: number | null) => value == null ? '—' : String(Math.round(value))
  const ratio = (value?: number | null) => value == null ? '—' : `${(value * 100).toFixed(0)}%`
  const mainlines = data.mainlines?.items ?? []
  const conceptLines = mainlines.filter(item => item.kind === 'concept')
  return <section aria-label="盯盘状态" className={cn(panelClass, 'space-y-3 py-3')}>
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {realtime && <Chip tone={realtime[0]} title={status?.realtime_reason ?? undefined}>{realtime[1]}</Chip>}
        <Chip tone={env.allowed ? 'ok' : 'bad'} title={env.reason}>市场环境：{env.label || '待确认'}{env.date ? `（${env.date}）` : ''}<span className="sr-only">{env.allowed ? '环境允许提示' : '环境暂不允许提示'}</span></Chip>
        {/* 阶段是多日收盘平滑后的周期位置，只能取上一完成交易日；当日强弱看右侧盘中梯队。 */}
        {env.phase_label && <Chip tone="neutral" title={ladder ? `${env.date ?? '参考交易日'} 收盘连板梯队：高度 ${whole(ladder.max_consecutive)} 板，晋级 ${ratio(ladder.promo_rate)}，封板 ${ratio(ladder.seal_rate)}；首板 ${whole(ladder.first_board)} 家，二板以上 ${whole(ladder.ge2_count)} 家，涨停 ${whole(ladder.limit_up)} 家，炸板 ${whole(ladder.broken_limit)} 家，跌停 ${whole(ladder.limit_down)} 家` : undefined}>
          情绪阶段（昨日）：{env.phase_label}{ladder && !live && <span className="tabular-nums text-secondary">高度 {whole(ladder.max_consecutive)} 板 · 晋级 {ratio(ladder.promo_rate)} · 封板 {ratio(ladder.seal_rate)}</span>}
        </Chip>}
        {live && <Chip tone="neutral" title={`${live.live ? '盘中实时' : '最近行情'}（${live.as_of ?? ''}，快照 ${stamp(live.observed_at)}）：涨停 ${whole(live.limit_up)} 家，炸板 ${whole(live.broken)} 家，跌停 ${whole(live.limit_down)} 家；首板 ${whole(live.first_board)} 家，二板以上 ${whole(live.ge2_count)} 家；昨日连板 ${whole(live.promo_pool)} 家${live.sealed_corrected ? '；已按五档盘口扣除假涨停' : ''}。口径同市场看板。`}>
          {live.live ? '盘中梯队' : '今日梯队'}：<span className="tabular-nums">高度 {whole(live.max_consecutive)} 板 · 晋级 {ratio(live.promo_rate)} · 封板 {ratio(live.seal_rate)} · 涨停 {whole(live.limit_up)}</span>
        </Chip>}
        {data.mainlines && <Chip tone="neutral" title={mainlines.length ? mainlines.map(item => `${item.kind === 'industry' ? '行业' : '概念'}第 ${item.rank}：${item.name}（涨停 ${whole(item.limit_up_count)} 家，最高 ${whole(item.max_boards)} 板）`).join('；') : data.mainlines.reason}>
          <span className="max-w-64 truncate">昨日主线：{mainlines.length ? (conceptLines.length ? conceptLines : mainlines).slice(0, 3).map(item => item.name).join('、') : '待确认'}</span>
        </Chip>}
        {summary && <button type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(value => !value)}
          className={cn(chipClass, toneClass[summary.tone], 'hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent')}>
          {toneIcon[summary.tone]}数据覆盖 {summary.label}{retained && '（上次摘要）'}
          {open ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
        </button>}
      </div>
      <span className="text-xs tabular-nums text-secondary">交易日期 {data.as_of ?? '—'} · 快照 {stamp(data.observed_at)} · 规则版本 {data.revision}</span>
    </div>
    {(showMessage || reasons.length > 0) && <div role="status" className="rounded-btn border border-border bg-elevated/50 px-3 py-2 text-xs leading-relaxed">
      {showMessage && <p className="text-sm">{data.message}</p>}
      {reasons.length > 0 && <ul className={cn('space-y-0.5 text-secondary', showMessage && 'mt-1')}>{reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
      {status?.realtime_status === 'blocked' && <Link className="mt-1 inline-block text-accent underline" to="/settings?tab=data-sources">检查实时行情数据源</Link>}
    </div>}
    {status && <div id={panelId} hidden={!open}><DataStatus data={status} retained={retained} /></div>}
  </section>
}
