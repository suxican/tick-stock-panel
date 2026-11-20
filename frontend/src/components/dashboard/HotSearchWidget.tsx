/**
 * 看板「实时热搜」组件 —— 猫爪个股热榜 (同花顺人气 / 东财人气 / 同花顺飙升) 与板块热搜。
 * 自取数据 (useMeozHotRank, 60 秒刷新), 仅展示、不参与评分; 榜单选择存在实例参数里, 随布局保存。
 * 热搜只有实时口径, 回看历史日不请求。
 */
import { useRef, type ReactNode } from 'react'
import { Flame } from 'lucide-react'
import type { MeozHotRank, MeozHotStockKind } from '@/lib/api'
import { cn } from '@/lib/cn'
import { boardTag } from '@/components/stock-table/primitives'
import { HOT_STOCK_LABELS, useMeozHotRank } from '@/lib/useMeozThemeAttribution'
import { fmtStockPct, pctClass, SectionTitle } from './shared'

export type HotSearchKind = MeozHotStockKind | 'themes'

const KINDS: HotSearchKind[] = ['ths_normal', 'dfcf_renqi', 'ths_trend', 'themes']
const KIND_LABEL: Record<HotSearchKind, string> = { ...HOT_STOCK_LABELS, themes: '板块热搜' }

export function isHotSearchKind(v: unknown): v is HotSearchKind {
  return typeof v === 'string' && (KINDS as string[]).includes(v)
}

/** ISO 时间 → HH:mm; 无法解析返回 null */
function hhmm(iso: string | null | undefined): string | null {
  const m = iso?.match(/[T ](\d{2}:\d{2})/)
  return m ? m[1] : null
}

type NavList = { symbol: string; name?: string }[]

export function HotSearchWidget({ kind: rawKind, isLatestDate, onKindChange, onOpenStock }: {
  kind?: string
  /** 看板选中的是最新交易日; 热搜只有实时数据 */
  isLatestDate: boolean
  onKindChange: (kind: HotSearchKind) => void
  onOpenStock: (symbol: string, name?: string, navList?: NavList) => void
}) {
  const kind: HotSearchKind = isHotSearchKind(rawKind) ? rawKind : 'ths_normal'
  const query = useMeozHotRank(isLatestDate)
  // 刷新失败 (quiet 的 null / 非 quiet 的 isError) 时继续展示上一份有效榜单并标「过期」;
  // 首次加载就失败没有缓存, 仍显示「来源不可用」; 历史日不展示实时缓存
  const lastGood = useRef<MeozHotRank | null>(null)
  if (!isLatestDate) lastGood.current = null
  else if (query.data) lastGood.current = query.data
  const hot = isLatestDate ? (query.data ?? lastGood.current ?? undefined) : undefined
  const stale = isLatestDate && lastGood.current != null && (query.data === null || query.isError)

  let hint: string | null = null
  let body: ReactNode
  if (!isLatestDate) {
    body = <div className="text-[11px] text-muted">热搜仅实时数据, 切回最新交易日后查看</div>
  } else if (query.isLoading) {
    body = <div className="text-[11px] text-muted">加载中…</div>
  } else if (kind === 'themes') {
    const list = hot?.themes
    if (!list || !list.available) {
      body = <div className="text-[11px] text-muted">来源不可用</div>
    } else if (list.rows.length === 0) {
      body = <div className="text-[11px] text-muted">暂无数据</div>
    } else {
      hint = hhmm(list.rows.find(r => r.available_at)?.available_at) ?? hhmm(hot?.fetched_at)
      body = (
        // 卡片行版式与个股榜一致; 前三名次用热色强调 (接口只有名次与名称, 没有涨幅 / 热度值)
        <div className="space-y-1">
          {list.rows.map(row => (
            <div key={row.theme_id || row.rank}
              className="grid grid-cols-[18px_1fr] items-center gap-1.5 rounded-md border border-transparent bg-elevated/40 px-1.5 py-1.5">
              <span data-testid="theme-rank"
                className={cn('text-center font-mono text-[10px]', row.rank <= 3 ? 'font-bold text-bull' : 'text-muted')}>{row.rank}</span>
              <span className="min-w-0 truncate text-[11px] text-foreground" title={row.name}>{row.name}</span>
            </div>
          ))}
        </div>
      )
    }
  } else {
    const list = hot?.stocks?.[kind]
    if (!list || !list.available) {
      body = <div className="text-[11px] text-muted">来源不可用</div>
    } else if (list.rows.length === 0) {
      body = <div className="text-[11px] text-muted">暂无数据</div>
    } else {
      hint = hhmm(hot?.fetched_at)
      const nav: NavList = list.rows.map(r => ({ symbol: r.symbol, name: r.name ?? undefined }))
      body = (
        // 版式与成交额榜 (StockList) 一致: 名次 | 名称+板块标 / 代码 | 涨幅
        <div className="space-y-1">
          {list.rows.map(row => {
            const board = boardTag(row.symbol)
            return (
              <button key={row.symbol} type="button" onClick={() => onOpenStock(row.symbol, row.name ?? undefined, nav)}
                className="grid w-full grid-cols-[18px_1fr_auto] items-center gap-1.5 rounded-md border border-transparent bg-elevated/40 px-1.5 py-1 text-left transition-colors hover:border-border/60 hover:bg-elevated hover:brightness-110">
                <span className="text-center font-mono text-[10px] text-muted">{row.rank}</span>
                <span className="block min-w-0">
                  <span className="flex items-center gap-1">
                    <span className="truncate text-[11px] text-foreground" title={row.name ?? row.symbol}>{row.name ?? row.symbol}</span>
                    {board && (
                      <span className={`inline-flex h-3 shrink-0 items-center justify-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>{board.label}</span>
                    )}
                  </span>
                  <span className="block font-mono text-[9px] text-muted">{row.symbol}</span>
                </span>
                <span className={cn('font-mono text-[11px] font-semibold', pctClass(row.pct_chg))}>{fmtStockPct(row.pct_chg)}</span>
              </button>
            )
          })}
        </div>
      )
    }
  }

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 text-[12px] shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <SectionTitle icon={Flame} title="实时热搜" hint={stale ? `${hint ?? ''}(过期)` : hint} />
      {/* 组件可被缩到很窄, 分段允许换行而不是溢出 */}
      <div className="mb-1.5 flex min-w-0 flex-wrap gap-0.5 text-[11px]" role="tablist">
        {KINDS.map(k => (
          <button key={k} type="button" role="tab" aria-selected={kind === k} onClick={() => onKindChange(k)}
            className={cn('rounded px-1.5 leading-5 transition-colors',
              kind === k ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground')}>
            {KIND_LABEL[k]}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
    </section>
  )
}
