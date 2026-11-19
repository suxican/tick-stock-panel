import { AlertTriangle, ChevronDown, ChevronUp, RotateCcw, X } from 'lucide-react'
import type { CapabilityRoute } from '@/lib/api'

const STATE_TEXT: Record<string, string> = { healthy: '正常', open: '熔断中', half_open: '待探测' }

function displayIn(cap: CapabilityRoute, name: string): string {
  if (name === 'tickflow') return 'TickFlow'
  return (cap.candidates.find(c => c.name === name) ?? cap.pending.find(c => c.name === name))?.display ?? name
}

function timeOf(iso: string | null): string {
  if (!iso) return ''
  const m = /T(\d{2}:\d{2})/.exec(iso)
  return m ? m[1] : iso
}

/** 兜底链能力 (depth5 / overseas) 的降级提示: 实际取数源不是主源时显示, 可重试主源。 */
export function FallbackBanner({ cap, busy, onRetry }: {
  cap: CapabilityRoute
  busy: boolean
  onRetry: () => void
}) {
  if (!cap.degraded) return null
  return (
    <div
      className="mt-1.5 flex items-center gap-1 text-[10px] text-warning min-w-0"
      title={cap.fallback_reason ?? undefined}
    >
      <AlertTriangle className="h-3 w-3 shrink-0" />
      <span className="truncate">当前由 {cap.active_display ?? cap.active_source} 兜底</span>
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        className="ml-auto inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-warning hover:bg-warning/10 disabled:opacity-50"
      >
        <RotateCcw className="h-2.5 w-2.5" />
        重试主源
      </button>
    </div>
  )
}

/** 兜底顺序编辑: 已选兜底源按顺序排列, 可上移/下移/移除, 可从其余候选添加; 链路状态按需展开。 */
export function FallbackControls({ cap, busy, onChange }: {
  cap: CapabilityRoute
  busy: boolean
  onChange: (next: string[]) => void
}) {
  const fallbacks = cap.fallbacks ?? []
  const addable = cap.candidates
    .map(c => c.name)
    .filter(name => name !== cap.current && !fallbacks.includes(name))

  const move = (index: number, delta: number) => {
    const next = [...fallbacks]
    const [item] = next.splice(index, 1)
    next.splice(index + delta, 0, item)
    onChange(next)
  }

  const iconBtn = 'inline-flex h-3.5 w-3.5 items-center justify-center rounded text-muted/60 hover:text-foreground hover:bg-elevated disabled:opacity-30 disabled:pointer-events-none'

  return (
    <div className="mt-1.5 text-[10px]">
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-[9px] font-medium uppercase tracking-wider text-muted/50 shrink-0">兜底</span>
        {fallbacks.length === 0 && <span className="text-muted/50">未启用</span>}
        {fallbacks.map((name, i) => {
          const label = displayIn(cap, name)
          return (
            <span key={name} className="inline-flex max-w-full min-w-0 items-center gap-0.5 rounded bg-elevated/60 pl-1 text-muted/80">
              <span className="font-mono text-muted/50">{i + 1}</span>
              <span className="min-w-0 truncate" title={label}>{label}</span>
              <button type="button" aria-label={`上移 ${label}`} disabled={busy || i === 0}
                onClick={() => move(i, -1)} className={iconBtn}>
                <ChevronUp className="h-2.5 w-2.5" />
              </button>
              <button type="button" aria-label={`下移 ${label}`} disabled={busy || i === fallbacks.length - 1}
                onClick={() => move(i, 1)} className={iconBtn}>
                <ChevronDown className="h-2.5 w-2.5" />
              </button>
              <button type="button" aria-label={`移除 ${label}`} disabled={busy}
                onClick={() => onChange(fallbacks.filter(n => n !== name))} className={iconBtn}>
                <X className="h-2.5 w-2.5" />
              </button>
            </span>
          )
        })}
        {addable.length > 0 && (
          <select
            aria-label="添加兜底源"
            value=""
            disabled={busy}
            onChange={e => e.target.value && onChange([...fallbacks, e.target.value])}
            className="h-4 rounded bg-elevated/60 px-0.5 text-[10px] text-muted/80 outline-none"
          >
            <option value="">+ 添加</option>
            {addable.map(name => <option key={name} value={name}>{displayIn(cap, name)}</option>)}
          </select>
        )}
      </div>
      {(cap.chain?.length ?? 0) > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer select-none text-muted/50 hover:text-muted">链路状态</summary>
          <ul className="mt-1 space-y-0.5">
            {cap.chain!.map(entry => (
              <li key={entry.source} className="flex items-center gap-1 min-w-0" title={entry.reason ?? undefined}>
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  entry.state === 'open' ? 'bg-warning' : entry.state === 'half_open' ? 'bg-muted' : 'bg-accent'
                }`} />
                <span className="min-w-0 truncate text-muted/80">{displayIn(cap, entry.source)}</span>
                <span className="shrink-0 text-muted/50">{STATE_TEXT[entry.state] ?? entry.state}</span>
                {entry.state === 'open' && (
                  <span className="truncate text-muted/50">
                    {entry.until ? `至 ${timeOf(entry.until)}` : '至重试'}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
