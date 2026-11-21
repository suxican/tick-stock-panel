import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import type { FirstBoardTemperature, FirstBoardTemperatureTier } from '@/lib/api'
import { positiveBadgeClass } from './shared'

const tierClasses: Record<FirstBoardTemperatureTier, string> = {
  attack: positiveBadgeClass,
  probe: 'border-border text-secondary',
  rest: 'border-amber-500/50 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  unknown: 'border-border text-muted',
}
const rule = '进攻：封板率 ≥70% 且昨日首板均涨 ≥2%；休息：封板率 <60%，或封板率 <70% 且昨日首板均涨 <0；其余为试错。'

function stageLabel(value: FirstBoardTemperature) {
  if (value.stage === 'early') return '早盘初判'
  if (value.stage === 'session') return '盘中'
  if (value.stage === 'close') return value.as_of ? `${value.as_of.slice(5, 7)}/${value.as_of.slice(8, 10)} 收盘` : '收盘'
  return null
}

/**
 * 今日首板温度：盯盘状态区最前面的一句结论。只作显示，不改变买入提示与建议金额；
 * 旧后端没有该字段或版本未知时不渲染。
 */
export function TemperatureBadge({ value }: { value?: FirstBoardTemperature }) {
  if (!value || value.version !== 'temperature-v1') return null
  const title = [rule, '只作显示，不影响买入提示与建议金额。', '赚钱效应仍靠选股，进攻档不代表可以无差别打板。',
    value.stage === 'early' && '早盘参考性较弱。', value.tier === 'unknown' && `待确认原因：${value.reason}`].filter(Boolean).join('\n')
  const stage = stageLabel(value)
  return <div data-first-board-temperature title={title} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
    <span className={cn('inline-flex shrink-0 items-center whitespace-nowrap rounded border px-2 py-0.5 text-sm font-medium', tierClasses[value.tier])}>首板温度 · {value.label}</span>
    <span className="tabular-nums">封板率 {value.seal_rate == null ? '—' : `${Math.round(value.seal_rate * 100)}%`}</span>
    <span className="tabular-nums">昨日首板均涨 <span className={priceColorClass(value.feedback_mean)}>{fmtPct(value.feedback_mean)}</span></span>
    {stage && <span className="text-secondary">{stage}</span>}
  </div>
}
