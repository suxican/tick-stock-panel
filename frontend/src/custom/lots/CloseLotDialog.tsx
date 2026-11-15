/**
 * 结清批次弹窗 —— 卖出后留下一条交易记录(卖出价/卖出日/归因), 再移除批次与其监控规则。
 * 与"删除"的区别只在留痕; 归因由用户自选, 程序与 AI 都不改判。
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, TRADE_REASON_LABELS, type Lot, type TradeReason } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cnToday } from '@/lib/cnTime'
import { fmtPrice } from '@/lib/format'
import { Modal } from '@/components/Modal'
import { DatePicker } from '@/components/DatePicker'

const REASONS = Object.entries(TRADE_REASON_LABELS) as [TradeReason, string][]

export function CloseLotDialog({ lot, name, onClose }: { lot: Lot; name: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [price, setPrice] = useState('')
  const [sellDate, setSellDate] = useState(() => cnToday())
  const [reason, setReason] = useState<TradeReason | null>(null)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  const sellPrice = Number(price)
  const ready = price.trim() !== '' && Number.isFinite(sellPrice) && sellPrice > 0 && !!sellDate && reason !== null

  const close = useMutation({
    mutationFn: () => api.lotClose(lot.id, { sell_price: sellPrice, sell_date: sellDate, reason: reason!, note: note.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      qc.invalidateQueries({ queryKey: QK.holdingAnalysisAll })
      qc.invalidateQueries({ queryKey: QK.tradeJournal })
      qc.invalidateQueries({ queryKey: QK.reviewBellwethersAll })
      onClose()
    },
    onError: err => setError(String((err as Error)?.message ?? err)),
  })

  const pnl = ready ? (sellPrice / lot.cost_price - 1) * 100 : null

  return (
    <Modal onClose={onClose} ariaLabel="结清批次" panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
      <div className="border-b border-border/60 px-4 py-3">
        <div className="text-sm font-medium text-foreground">
          结清 <span className="font-mono">{lot.symbol}</span>{name && name !== lot.symbol ? ` ${name}` : ''}
        </div>
        <div className="mt-1 text-[11px] text-muted">
          成本 {fmtPrice(lot.cost_price)}{lot.qty > 0 ? ` · ${lot.qty} 股` : ''}{lot.buy_date ? ` · 买入 ${lot.buy_date}` : ''}
        </div>
      </div>
      <div className="space-y-3 px-4 py-4">
        <div className="grid grid-cols-2 gap-3">
          <label className="block space-y-1.5">
            <span className="block text-[11px] text-muted">卖出价</span>
            <input
              aria-label="卖出价"
              type="number" min={0} step="any" placeholder="0" autoFocus
              value={price}
              onChange={e => setPrice(e.target.value)}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground"
            />
          </label>
          <div className="space-y-1.5">
            <span className="block text-[11px] text-muted">卖出日</span>
            <DatePicker value={sellDate} onChange={v => setSellDate(v || '')} placeholder="选择日期" />
          </div>
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-[11px] text-muted">这笔交易</legend>
          <div className="flex flex-wrap gap-1.5">
            {REASONS.map(([key, label]) => (
              <label
                key={key}
                className={`cursor-pointer rounded-btn border px-2.5 py-1.5 text-xs transition-colors focus-within:ring-2 focus-within:ring-accent ${reason === key ? 'border-accent/50 bg-accent/10 text-accent' : 'border-border text-secondary hover:bg-elevated'}`}
              >
                <input type="radio" name="trade-reason" className="sr-only" checked={reason === key} onChange={() => setReason(key)} />{label}
              </label>
            ))}
          </div>
        </fieldset>

        <label className="block space-y-1.5">
          <span className="text-[11px] text-muted">备注 (可选)</span>
          <textarea
            aria-label="备注"
            value={note}
            onChange={e => setNote(e.target.value)}
            maxLength={200}
            rows={2}
            className="w-full resize-none rounded-btn border border-border bg-base px-3 py-2 text-xs text-foreground"
          />
        </label>

        {pnl !== null && (
          <div className="text-[11px] text-muted">
            相对登记成本 <span className={`font-mono ${pnl > 0 ? 'text-bull' : pnl < 0 ? 'text-bear' : 'text-muted'}`}>{pnl > 0 ? '+' : ''}{pnl.toFixed(2)}%</span>, 不含费用
          </div>
        )}
        {error && <div role="alert" className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">{error}</div>}
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-border/60 px-4 py-3">
        <span className="text-[10px] text-muted">结清后批次与其监控规则一并移除</span>
        <div className="flex items-center gap-2">
          <button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
          <button
            onClick={() => { setError(''); close.mutate() }}
            disabled={!ready || close.isPending}
            className="h-9 rounded-btn bg-accent/90 px-4 text-xs font-medium text-white hover:bg-accent cursor-pointer disabled:cursor-not-allowed disabled:opacity-50"
          >
            {close.isPending ? '结清中...' : '结清并记录'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
