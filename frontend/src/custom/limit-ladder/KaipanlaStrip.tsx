import type { KplSnapshot } from '@/lib/api'
import { cn } from '@/lib/cn'

const STALE_SECONDS = 120
const MAX_THEMES = 8

function pct(v: number | null | undefined): string | null {
  return v == null || Number.isNaN(v) ? null : `${(v * 100).toFixed(1)}%`
}

/** 连板梯队页的开盘啦（非官方）情绪条 + 题材行。只展示, 不参与任何计算。 */
export function KaipanlaStrip({ snapshot, error, selectedTheme, onThemeSelect, showThemes = true }: {
  snapshot: KplSnapshot | null | undefined
  error?: string
  selectedTheme?: string
  /** 开盘啦快照与梯队同日时才显示题材行 */
  showThemes?: boolean
  onThemeSelect: (code: string | undefined) => void
}) {
  if (!snapshot) {
    return error ? <div role="status" className="px-5 pb-1 text-[11px] text-muted">{error}</div> : null
  }
  const s = snapshot.summary
  const promo = snapshot.expression?.kpl_promo_2 != null
    ? `二板晋级 ${pct(snapshot.expression.kpl_promo_2)}`
    : snapshot.prev_expression?.kpl_promo_2 != null ? `昨日二板晋级 ${pct(snapshot.prev_expression.kpl_promo_2)}` : null
  const items = [
    s.kpl_strong != null ? `情绪 ${s.kpl_strong}` : null,
    `涨停 ${s.limit_up}`,
    s.limit_down != null ? `跌停 ${s.limit_down}` : null,
    `最高 ${s.max_boards} 板`,
    pct(s.broken_rate) ? `破板率 ${pct(s.broken_rate)}` : null,
    promo,
  ].filter(Boolean) as string[]
  const stale = snapshot.stale_seconds > STALE_SECONDS
  const themes = showThemes ? snapshot.themes.slice(0, MAX_THEMES) : []
  return (
    <div className="px-5 pb-1 text-xs" aria-label="开盘啦盘中情绪">
      <div className="flex flex-wrap items-baseline gap-x-2 text-secondary">
        <span className="text-[11px] text-muted" title="开盘啦（非官方）">开盘啦</span>
        <span className="tabular-nums text-foreground">{items.join(' · ')}</span>
        <span className={cn('ml-auto text-[11px] tabular-nums', stale ? 'text-warning' : 'text-muted')}>
          {stale ? '数据已过期 · ' : ''}{snapshot.fetched_at.slice(11, 16)} 更新
        </span>
      </div>
      {themes.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
          {themes.map(t => (
            <button
              key={t.code}
              type="button"
              data-theme={t.code}
              aria-pressed={selectedTheme === t.code}
              onClick={() => onThemeSelect(selectedTheme === t.code ? undefined : t.code)}
              className={cn('tabular-nums hover:text-accent', selectedTheme === t.code ? 'text-accent font-medium' : 'text-secondary')}
            >
              {t.name} {t.count}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
