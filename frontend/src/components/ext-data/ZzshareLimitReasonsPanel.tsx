import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cnDateFromUtc } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { zzshareReasonsOptions } from '@/lib/useZzshareReasons'
import { SettingsModal } from '@/components/data/SettingsModal'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'

const buttonStyle = 'rounded-btn bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50'

export function ZzshareLimitReasonsPanel({ available = true }: { available?: boolean }) {
  const qc = useQueryClient()
  const today = cnDateFromUtc(new Date().toISOString())
  const [date, setDate] = useState('')
  const [viewDate, setViewDate] = useState<string | null>(null)
  const sync = useMutation({
    mutationFn: (tradeDate: string) => api.syncZzshareLimitReasons(tradeDate),
    onSuccess: (result) => {
      if (result.state !== 'synced') return
      void qc.invalidateQueries({ queryKey: QK.extData })
      void qc.invalidateQueries({ queryKey: QK.dataStatus })
      void qc.invalidateQueries({ queryKey: QK.extDataSchemaAll })
      void qc.invalidateQueries({ queryKey: QK.extSchemaAll })
      void qc.invalidateQueries({ queryKey: QK.extDataRows(result.table_id).slice(0, 2) })
      void qc.invalidateQueries({ queryKey: QK.limitLadder(result.date) })
      void qc.invalidateQueries({ queryKey: QK.limitLadder() })
    },
  })

  return (
    <section className="space-y-2 text-xs">
      <h4 className="font-medium text-foreground">盘后涨停归因</h4>
      <p className="text-secondary">选择已收盘的交易日，首次成功同步后创建「ZZShare 涨停归因」表。点击「查看涨停归因」浏览该日股票和完整原因，重复同步读取已保存的快照。</p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex flex-wrap items-center gap-2 text-secondary">
          交易日
          <input type="date" value={date} max={today} disabled={sync.isPending}
            className="min-w-0 rounded-input border border-border bg-base px-2 py-1 text-foreground"
            onChange={e => { setDate(e.target.value); sync.reset() }} />
        </label>
        <button type="button" className={buttonStyle} disabled={!available || !date || date > today || sync.isPending}
          onClick={() => sync.mutate(date)}>
          {sync.isPending ? '同步中…' : '同步涨停归因'}
        </button>
        <button type="button" className={buttonStyle} disabled={!date || date > today || sync.isPending}
          onClick={() => setViewDate(date)}>
          查看涨停归因
        </button>
      </div>
      {sync.data && <p role="status" className="text-secondary">
        {sync.data.date} · {sync.data.state === 'no_data' ? '当日暂无归因数据，未写入快照，可稍后重试。' : (
          <>{sync.data.state === 'cached' ? '读取已有快照' : '已保存快照'}，{sync.data.raw_rows} 条归因记录，覆盖 {sync.data.stocks} 只股票。</>
        )}
      </p>}
      {sync.isError && <p role="alert" className="text-danger">{sync.error.message}</p>}
      {viewDate && <ZzshareReasons date={viewDate} onClose={() => setViewDate(null)} />}
    </section>
  )
}

function ZzshareReasons({ date, onClose }: { date: string; onClose: () => void }) {
  const rows = useQuery(zzshareReasonsOptions(date))

  return (
    <SettingsModal title={`${date} 涨停归因`} onClose={onClose} width="max-w-3xl">
      {rows.isPending ? <p role="status" className="py-8 text-center text-sm text-secondary">正在读取归因…</p>
        : rows.isError ? <ErrorState hint={rows.error.message} retrying={rows.isFetching} onRetry={() => void rows.refetch()} />
        : !rows.data?.rows.length ? <EmptyState title="该日还没有归因快照" hint="请先同步该交易日，成功后再查看。" />
        : <div className="space-y-3">
          <p className="text-xs text-secondary">共 {rows.data.total} 只股票，点击股票展开完整原因。来源为 ZZShare 盘后归因。</p>
          {rows.data.rows.map(row => (
            <details key={row.symbol} className="rounded-lg border border-border bg-elevated/30">
              <summary className="cursor-pointer px-4 py-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60">
                {row.name} <span className="ml-2 font-mono text-xs text-secondary">{row.symbol}</span>
              </summary>
              <p className="whitespace-pre-wrap break-words border-t border-border/50 px-4 py-3 text-sm leading-relaxed text-secondary">{row.reason}</p>
            </details>
          ))}
        </div>}
    </SettingsModal>
  )
}
