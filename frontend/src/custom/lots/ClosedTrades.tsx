/**
 * 已结清交易 —— 批次结清后留下的记录, 默认收起; 用于回看与删除误记的记录。
 * 累计统计在复盘页「今日执行」展示, 这里不重复。
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Trash2 } from 'lucide-react'
import { api, TRADE_REASON_LABELS } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'

export function ClosedTrades() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (resetTimer.current) clearTimeout(resetTimer.current) }, [])

  const query = useQuery({ queryKey: QK.tradeJournal, queryFn: api.tradeJournalList })
  const del = useMutation({
    mutationFn: api.tradeJournalDelete,
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.tradeJournal }),
  })

  // 与批次删除一致: 第一次进确认态, 第二次真删, 3 秒后自动复位
  const handleDelete = (id: string) => {
    if (resetTimer.current) clearTimeout(resetTimer.current)
    if (confirmId === id) {
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  if (query.isError) {
    return (
      <div className="flex items-center justify-center gap-2 text-[11px] text-muted">
        已结清记录加载失败
        <button onClick={() => query.refetch()} className="rounded-btn border border-border px-2 py-0.5 text-secondary hover:bg-elevated cursor-pointer">重试</button>
      </div>
    )
  }
  const entries = query.data?.entries ?? []
  if (entries.length === 0) return null

  return (
    <section className="overflow-hidden rounded-xl border border-border bg-surface/40">
      <button
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-4 py-2.5 text-left text-xs text-secondary hover:bg-elevated/40 cursor-pointer"
      >
        <ChevronRight className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-90')} />
        已结清 {entries.length} 笔
      </button>
      {open && (
        <>
          <div className="overflow-x-auto border-t border-border/60">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 text-[10px] tracking-wide text-muted">
                  <th className="px-4 py-2 font-medium">标的</th>
                  <th className="px-2 py-2 font-medium">卖出日</th>
                  <th className="px-2 py-2 text-right font-medium">盈亏</th>
                  <th className="px-2 py-2 text-right font-medium">持有</th>
                  <th className="px-2 py-2 font-medium">归因</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {entries.map(e => (
                  <tr key={e.id} className="border-b border-border/40 last:border-0" title={e.note || undefined}>
                    <td className="px-4 py-2">
                      <span className="text-foreground">{e.name}</span>
                      {e.name !== e.symbol && <span className="ml-1.5 font-mono text-[10px] text-muted">{e.symbol}</span>}
                    </td>
                    <td className="px-2 py-2 text-muted">{e.sell_date}</td>
                    <td className={cn('px-2 py-2 text-right font-mono', priceColorClass(e.pnl_pct))}>{e.pnl_pct > 0 ? '+' : ''}{e.pnl_pct.toFixed(2)}%</td>
                    <td className="px-2 py-2 text-right text-secondary">{e.hold_days == null ? '—' : `${e.hold_days} 天`}</td>
                    <td className="px-2 py-2 text-secondary">{TRADE_REASON_LABELS[e.reason] ?? e.reason}</td>
                    <td className="px-3 py-2 text-right">
                      {confirmId === e.id ? (
                        <button
                          onClick={() => handleDelete(e.id)}
                          title="再次点击确认删除"
                          className="inline-flex items-center gap-1 rounded-md border border-danger/30 bg-danger/15 px-1.5 py-0.5 text-[9px] font-medium text-danger animate-pulse cursor-pointer"
                        >
                          <Trash2 className="h-2.5 w-2.5" />确认
                        </button>
                      ) : (
                        <button
                          onClick={() => handleDelete(e.id)}
                          title="删除这条记录 (不恢复批次)"
                          className="rounded-md p-1.5 text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-border/60 px-4 py-2 text-[10px] text-muted">盈亏为卖出价相对登记成本，不含费用；持有按交易日计</div>
        </>
      )}
    </section>
  )
}
