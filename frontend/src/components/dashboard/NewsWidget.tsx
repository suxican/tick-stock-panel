/**
 * 看板「新闻」组件 —— 猫爪 news 纯时间线 (时间、来源、标题), 自取数据、仅展示、不参与评分。
 * 新闻只有实时口径: 回看历史日不请求。来源筛选项由返回的 sources 动态生成, 选择存在实例参数里。
 * 标题仅在 url 为 http(s) 时渲染为新标签链接; 摘要点击箭头按需展开。
 */
import { useRef, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, Newspaper } from 'lucide-react'
import type { MeozNews } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useMeozNews } from '@/lib/useMeozWidgets'
import { SectionTitle } from './shared'

const SAFE_URL = /^https?:\/\//i

/** ISO 时间 → HH:mm; 无法解析返回 null */
function hhmm(iso: string | null | undefined): string | null {
  const m = iso?.match(/[T ](\d{2}:\d{2})/)
  return m ? m[1] : null
}

export function NewsWidget({ source: rawSource, isLatestDate, onSourceChange }: {
  /** 'all' 或某个来源的 source 字段值 */
  source?: string
  /** 看板选中的是最新交易日; 新闻只有实时数据 */
  isLatestDate: boolean
  onSourceChange: (source: string) => void
}) {
  const query = useMeozNews(isLatestDate)
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  // 刷新失败时继续展示上一份有效数据并标「过期」; 首次加载失败没有缓存, 显示「来源不可用」;
  // 历史日绝不展示实时缓存
  const lastGood = useRef<MeozNews | null>(null)
  if (!isLatestDate) lastGood.current = null
  else if (query.data) lastGood.current = query.data
  const data = isLatestDate ? (query.data ?? lastGood.current ?? undefined) : undefined
  const stale = isLatestDate && lastGood.current != null && (query.data === null || query.isError)

  const sources = data?.sources ?? []
  const source = rawSource && sources.some(s => s.source === rawSource) ? rawSource : 'all'
  const toggle = (id: string) => setOpen(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  let hint: string | null = null
  let body: ReactNode
  if (!isLatestDate) {
    body = <div className="text-[11px] text-muted">新闻仅实时数据, 切回最新交易日后查看</div>
  } else if (query.isLoading) {
    body = <div className="text-[11px] text-muted">加载中…</div>
  } else if (!data) {
    body = <div className="text-[11px] text-muted">来源不可用</div>
  } else {
    hint = hhmm(data.fetched_at)
    const rows = source === 'all' ? data.rows : data.rows.filter(r => r.source === source)
    if (rows.length === 0) {
      body = <div className="text-[11px] text-muted">暂无新闻</div>
    } else {
      body = (
        // 卡片行: 左列时间在上、来源在下, 中间标题 (最多两行), 右侧摘要展开箭头
        <div className="space-y-1">
          {rows.map(row => {
            const expanded = open.has(row.id)
            const linked = !!row.url && SAFE_URL.test(row.url)
            const titleCls = 'text-[11px] leading-4 text-foreground line-clamp-2'
            return (
              <div key={row.id}
                className="grid grid-cols-[3.5rem_1fr_auto] items-start gap-x-1.5 rounded-md border border-transparent bg-elevated/40 px-1.5 py-1 transition-colors hover:border-border/60 hover:bg-elevated">
                <span data-testid="news-meta" className="block min-w-0">
                  <span className="block font-mono text-[11px] leading-4 text-muted">{row.time_label ?? hhmm(row.display_at) ?? ''}</span>
                  <span className="block truncate text-[9px] leading-3 text-secondary" title={row.source_name}>{row.source_name}</span>
                </span>
                {linked ? (
                  <a href={row.url!} target="_blank" rel="noopener noreferrer" title={row.title}
                    className={cn(titleCls, 'min-w-0 hover:text-accent')}>{row.title}</a>
                ) : (
                  <span title={row.title} className={cn(titleCls, 'min-w-0')}>{row.title}</span>
                )}
                {row.summary && (
                  <button type="button" data-testid="news-expand" aria-expanded={expanded}
                    aria-label={expanded ? '收起摘要' : '展开摘要'} onClick={() => toggle(row.id)}
                    className="mt-0.5 shrink-0 text-muted hover:text-foreground">
                    {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  </button>
                )}
                {row.summary && expanded && (
                  <p className="col-span-2 col-start-2 mt-0.5 whitespace-pre-wrap break-words text-[11px] leading-4 text-secondary">{row.summary}</p>
                )}
              </div>
            )
          })}
        </div>
      )
    }
  }

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 text-[12px] shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <SectionTitle icon={Newspaper} title="新闻" hint={stale ? `${hint ?? ''}(过期)` : hint} />
      {/* 来源筛选: 仅有数据时显示; 组件可被缩到很窄, 分段允许换行而不是溢出 */}
      {isLatestDate && data && sources.length > 0 && (
        <div className="mb-1.5 flex min-w-0 flex-wrap gap-0.5 text-[11px]" role="tablist">
          {[{ source: 'all', source_name: '全部' }, ...sources].map(s => (
            <button key={s.source} type="button" role="tab" aria-selected={source === s.source}
              onClick={() => onSourceChange(s.source)}
              className={cn('rounded px-1.5 leading-5 transition-colors',
                source === s.source ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground')}>
              {s.source_name}
            </button>
          ))}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
    </section>
  )
}
