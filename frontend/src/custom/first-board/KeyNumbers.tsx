import type { FirstBoardFeedback, FirstBoardSnapshot } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'

const whole = (value: number | null | undefined) => value == null ? null : String(Math.round(value))

/**
 * 决定出手强度的几个当日数字，只在顶部出现一次。缺失显示占位符，不显示 0；
 * muted 表示反馈已过期或刷新失败，此时均涨不再按红绿着色。
 */
export function KeyNumbers({ ladder, feedback, muted }: {
  ladder?: FirstBoardSnapshot['market_ladder']; feedback?: FirstBoardFeedback; muted: boolean
}) {
  const all = feedback?.groups?.all
  const mean = all?.mean_change_pct ?? null
  // 涨停状态未确认的样本不能算作未涨停：全部未知显示占位符，部分未知标出待确认数。
  const known = all ? all.limit_known_count ?? all.sample_count : 0
  const promoted = !all || all.sample_count <= 0 || known <= 0 ? '—'
    : `${all.limit_up_count} / ${all.sample_count}${known < all.sample_count ? ` · 待确认 ${all.sample_count - known}` : ''}`
  const items: [string, string, string?][] = [
    ['封板率', ladder?.seal_rate == null ? '—' : `${Math.round(ladder.seal_rate * 100)}%`],
    ['昨日首板均涨', fmtPct(mean), muted ? 'text-foreground' : priceColorClass(mean)],
    ['晋级二板', promoted],
    ['涨停:跌停', whole(ladder?.limit_up) && whole(ladder?.limit_down) ? `${whole(ladder?.limit_up)}:${whole(ladder?.limit_down)}` : '—'],
    ['高度', ladder?.max_consecutive == null ? '—' : `${whole(ladder.max_consecutive)} 板`],
  ]
  return <dl aria-label="关键数字" className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-xs">
    {items.map(([label, text, tone]) => <div key={label} className="flex items-baseline gap-1.5">
      <dt className="text-secondary">{label}</dt>
      <dd className={cn('text-sm font-medium tabular-nums', tone)}>{text}</dd>
    </div>)}
  </dl>
}
