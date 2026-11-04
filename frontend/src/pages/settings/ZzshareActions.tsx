import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cnDateFromUtc } from '@/lib/format'
import { QK } from '@/lib/queryKeys'

const buttonStyle = 'rounded-btn bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/20 disabled:opacity-50'

export function ZzshareActions({ available }: { available: boolean }) {
  const qc = useQueryClient()
  const today = cnDateFromUtc(new Date().toISOString())
  const [date, setDate] = useState('')
  const daily = useMutation({
    mutationFn: async () => {
      const result = await api.testDataSource('zzshare', 'daily', ['600000.SH'])
      if (result.error) throw new Error(result.error)
      return result
    },
  })
  const sync = useMutation({
    mutationFn: (tradeDate: string) => api.syncZzshareLimitReasons(tradeDate),
    onSuccess: (result) => {
      if (result.state !== 'synced') return
      void qc.invalidateQueries({ queryKey: QK.extData })
      void qc.invalidateQueries({ queryKey: QK.extDataSchemaAll })
      void qc.invalidateQueries({ queryKey: QK.extSchemaAll })
      void qc.invalidateQueries({ queryKey: QK.extDataRows(result.table_id).slice(0, 2) })
      void qc.invalidateQueries({ queryKey: QK.limitLadder(result.date) })
      void qc.invalidateQueries({ queryKey: QK.limitLadder() })
    },
  })
  const sample = daily.data?.preview[0]

  return (
    <div className="space-y-4 text-xs">
      <section className="space-y-2">
        <h4 className="font-medium text-foreground">股票日 K</h4>
        <p className="text-secondary">试拉浦发银行（600000.SH）的行情。需要用于同步时，可在上方「能力路由」的日 K 项选择 zzshare。该源仅覆盖股票，ETF 和指数日 K 不受支持。</p>
        <button type="button" className={buttonStyle} disabled={!available || daily.isPending} onClick={() => daily.mutate()}>
          {daily.isPending ? '试拉中…' : '试拉股票日 K'}
        </button>
        {daily.data && <p role="status" className="text-secondary">
          {daily.data.rows > 0 ? `返回 ${daily.data.rows} 行，样本日期 ${sample?.date ?? '—'}，收盘价 ${sample?.close ?? '—'} 元。` : '未返回日 K，请稍后重试。'}
        </p>}
        {daily.isError && <p role="alert" className="text-danger">{daily.error.message}</p>}
      </section>
      <section className="space-y-2 border-t border-border/50 pt-4">
        <h4 className="font-medium text-foreground">盘后涨停归因</h4>
        <p className="text-secondary">选择已收盘的交易日。首次同步保存当日快照，重复同步读取已保存的数据。结果可在「数据 → 扩展数据」查看。</p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-secondary">
            交易日
            <input type="date" value={date} max={today} disabled={sync.isPending}
              className="rounded-input border border-border bg-base px-2 py-1 text-foreground"
              onChange={e => { setDate(e.target.value); sync.reset() }} />
          </label>
          <button type="button" className={buttonStyle} disabled={!available || !date || date > today || sync.isPending}
            onClick={() => sync.mutate(date)}>
            {sync.isPending ? '同步中…' : '同步涨停归因'}
          </button>
        </div>
        {sync.data && <p role="status" className="text-secondary">
          {sync.data.date} · {sync.data.state === 'no_data' ? '当日暂无归因数据，未写入快照，可稍后重试。' : (
            <>{sync.data.state === 'cached' ? '读取已有快照' : '已保存快照'}，{sync.data.raw_rows} 条归因记录，覆盖 {sync.data.stocks} 只股票。</>
          )}
        </p>}
        {sync.isError && <p role="alert" className="text-danger">{sync.error.message}</p>}
      </section>
    </div>
  )
}
