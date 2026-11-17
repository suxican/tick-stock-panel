import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, RefreshCw } from 'lucide-react'
import { api, type EtfLedgerAppendResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { FrontendSlotContextMap } from '@/extensions/types'

const LEDGER_TABLE = 'etf_broad_flow_daily'
// 卡片底部已显示带年份的最新日期, 这里只需月-日
const monthDay = (iso: string) => iso.slice(5)

function summary({ added, latest, pending }: EtfLedgerAppendResult) {
  const waiting = pending.length ? `${pending.map(monthDay).join('、')} 待交易所份额与监管净值发布` : ''
  if (added.length) return `已追加 ${added.map(monthDay).join('、')}${waiting ? `；${waiting}` : ''}`
  if (waiting) return `暂无可追加：${waiting}`
  return latest ? `已是最新（${latest}）` : '尚无已发布的台账'
}

/** 数据页「宽基 ETF 日度份额」卡片: 立即执行与定时任务相同的追加, 未齐备的交易日继续等待。 */
export function LedgerUpdateAction({ tableId, refresh }: FrontendSlotContextMap['data.ext-card.actions']) {
  return tableId === LEDGER_TABLE ? <LedgerUpdate refresh={refresh} /> : null
}

function LedgerUpdate({ refresh }: { refresh: () => void }) {
  const qc = useQueryClient()
  const update = useMutation({
    mutationFn: () => api.etfLedgerAppend(),
    onSuccess: result => {
      if (!result.added.length) return
      refresh()
      void qc.invalidateQueries({ queryKey: QK.etfObservationAll })
    },
  })

  return (
    <div className="px-4 pb-3 space-y-1.5">
      <button type="button" disabled={update.isPending} onClick={() => update.mutate()}
        className="inline-flex min-h-8 w-full items-center justify-center gap-1.5 rounded-btn bg-accent/10 px-2 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-60">
        {update.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
        {update.isPending ? '更新中…' : '更新份额台账'}
      </button>
      {update.isSuccess && <p role="status" className="text-[10px] leading-relaxed text-secondary">{summary(update.data)}</p>}
      {update.isError && <p role="alert" className="text-[10px] leading-relaxed text-danger">{update.error.message}</p>}
    </div>
  )
}
