/**
 * 看板「最强风口」组件 —— 猫爪 fengk_kp 个股榜 (按强度降序), 自取数据、仅展示、不参与评分。
 * 最新交易日: 60 秒刷新, 失败保留上一份有效数据并标「过期」; 回看历史日: 按 tradedate 查询、不轮询,
 * 失败或为空都不回退到实时数据。
 */
import { useRef, type ReactNode } from 'react'
import { Compass } from 'lucide-react'
import type { MeozStrongestVent } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { useMeozStrongestVent } from '@/lib/useMeozWidgets'
import { fmtStockPct, pctClass, SectionTitle } from './shared'

/** ISO 时间 → HH:mm; 无法解析返回 null */
function hhmm(iso: string | null | undefined): string | null {
  const m = iso?.match(/[T ](\d{2}:\d{2})/)
  return m ? m[1] : null
}

/** 带符号的金额 (元 → 万 / 亿); fmtBigNum 不处理负数 */
function fmtSigned(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v < 0 ? `-${fmtBigNum(-v)}` : `+${fmtBigNum(v)}`
}

/** 每行最多显示的题材标签数, 其余折成「+N」(完整列表在悬停提示里) */
const MAX_THEME_TAGS = 2

type NavList ={ symbol: string; name?: string }[]

export function StrongestVentWidget({ tradeDate, isLatestDate, onOpenStock }: {
  /** 看板选中的交易日 (YYYY-MM-DD) */
  tradeDate: string
  /** 看板选中的是最新交易日 → 实时模式 */
  isLatestDate: boolean
  onOpenStock: (symbol: string, name?: string, navList?: NavList) => void
}) {
  const live = isLatestDate
  const query = useMeozStrongestVent(tradeDate, live)
  // 仅实时模式保留上一份有效数据; 历史日绝不显示实时 / 其他日期的数据
  const lastGood = useRef<MeozStrongestVent | null>(null)
  if (!live) lastGood.current = null
  else if (query.data) lastGood.current = query.data
  const data = live ? (query.data ?? lastGood.current ?? undefined) : query.data
  const stale = live && lastGood.current != null && (query.data === null || query.isError)

  let hint: ReactNode = null
  let body: ReactNode
  if (query.isLoading) {
    body = <div className="text-[11px] text-muted">加载中…</div>
  } else if (!data) {
    body = <div className="text-[11px] text-muted">来源不可用</div>
  } else if (data.rows.length === 0) {
    body = <div className="text-[11px] text-muted">{live ? '暂无信号' : '该日无数据'}</div>
  } else {
    const latest = data.rows.reduce<string | null>((acc, r) => (r.signal_at && (!acc || r.signal_at > acc) ? r.signal_at : acc), null)
    hint = `${hhmm(latest) ? `信号 ${hhmm(latest)} · ` : ''}${data.rows.length}只`
    const nav: NavList = data.rows.map(r => ({ symbol: r.symbol, name: r.name || undefined }))
    body = (
      // 版式与成交额榜 (StockList) 一致: 序号 | 名称+板块标 / 代码 | 主要数值 / 涨幅
      <div className="space-y-1">
        {data.rows.map((row, i) => {
          const board = boardTag(row.symbol)
          const shown = row.themes.slice(0, MAX_THEME_TAGS)
          const hidden = row.themes.length - shown.length
          return (
            <button key={row.symbol} type="button" onClick={() => onOpenStock(row.symbol, row.name || undefined, nav)}
              title={`强度 ${row.strength} · 主力买入 ${fmtBigNum(row.main_buy_amount)} / 主力卖出 ${fmtBigNum(row.main_sell_amount == null ? null : Math.abs(row.main_sell_amount))}`}
              className="grid w-full grid-cols-[18px_1fr_auto] items-center gap-1.5 rounded-md border border-transparent bg-elevated/40 px-1.5 py-1 text-left transition-colors hover:border-border/60 hover:bg-elevated hover:brightness-110">
              <span className="text-center font-mono text-[10px] text-muted">{i + 1}</span>
              <span className="block min-w-0">
                <span className="flex items-center gap-1">
                  <span className="truncate text-[11px] text-foreground">{row.name || row.symbol}</span>
                  {board && (
                    <span className={`inline-flex h-3 shrink-0 items-center justify-center rounded border px-1 text-[8px] font-bold leading-none ${board.color}`}>{board.label}</span>
                  )}
                </span>
                <span className="flex min-w-0 items-center gap-1">
                  <span className="shrink-0 font-mono text-[9px] text-muted">{row.symbol}</span>
                  {row.themes.length > 0 && (
                    <span data-testid="vent-themes" title={row.themes.join('、')} className="flex min-w-0 items-center gap-1 overflow-hidden">
                      {shown.map(t => (
                        <span key={t} className="shrink-0 truncate rounded bg-accent/10 px-1 text-[9px] leading-[14px] text-accent">{t}</span>
                      ))}
                      {hidden > 0 && <span className="shrink-0 text-[9px] text-muted">+{hidden}</span>}
                    </span>
                  )}
                </span>
              </span>
              <span className="block text-right">
                <span className={cn('block font-mono text-[11px]', pctClass(row.main_net_amount))}>{fmtSigned(row.main_net_amount)}</span>
                <span className={cn('block font-mono text-[9px]', pctClass(row.pct_chg))}>{fmtStockPct(row.pct_chg)}</span>
              </span>
            </button>
          )
        })}
      </div>
    )
  }

  const title = live ? '最强风口' : `最强风口 · 回看 ${tradeDate}`
  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 text-[12px] shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <SectionTitle icon={Compass} title={title} hint={stale ? `${hint ?? ''}(过期)` : hint} />
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
    </section>
  )
}
