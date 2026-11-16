/**
 * 账户规模: 复盘「今日执行」仓位核对的分母。按生效日期留档, 修改只追加新的一条,
 * 已核对的历史日子仍按当时的规模计算。金额以万元输入, 接口为人民币元。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api } from '@/lib/api'
import { cnToday } from '@/lib/cnTime'
import { QK } from '@/lib/queryKeys'

const toWan = (amount: number) => Number((amount / 10000).toFixed(2)).toString()

export function AccountSizeBar() {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: QK.accountSize, queryFn: api.accountSize })
  const [editing, setEditing] = useState(false)
  const history = query.data?.history ?? []
  const latest = history[history.length - 1]
  const save = useMutation({
    mutationFn: api.accountSizeUpdate,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.accountSize })
      toast('账户规模已保存', 'success')
      setEditing(false)
    },
    onError: (e: Error) => toast(`保存失败 · ${e.message}`, 'error'),
  })
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]" title="复盘仓位核对的分母：持仓市值 ÷ 账户规模">
      {latest ? (
        <span className="text-secondary">
          账户规模 <span className="font-mono text-foreground">{toWan(latest.amount)} 万</span>
          <span className="text-muted">（{latest.effective_date} 起）</span>
        </span>
      ) : (
        <span className="text-warning">{query.isError ? '账户规模读取失败' : '未填写账户规模，复盘无法核对仓位占比'}</span>
      )}
      <button type="button" onClick={() => setEditing(true)} disabled={query.isLoading}
        className="h-6 rounded-btn border border-border bg-base px-2 text-[12px] text-secondary hover:border-accent/40 hover:text-accent">
        {latest ? '修改' : '填写'}
      </button>
      {editing && (
        <AccountSizeModal initial={latest ? toWan(latest.amount) : ''} saving={save.isPending}
          onClose={() => setEditing(false)}
          onSave={(effective_date, amount) => save.mutate({ effective_date, amount })} />
      )}
    </div>
  )
}

function AccountSizeModal({ initial, saving, onClose, onSave }: {
  initial: string
  saving: boolean
  onClose: () => void
  onSave: (effectiveDate: string, amount: number) => void
}) {
  const [wan, setWan] = useState(initial)
  const [day, setDay] = useState(cnToday())
  const submit = () => {
    const value = Number(wan)
    if (!Number.isFinite(value) || value <= 0) {
      toast('账户规模须为正数（万元）', 'error')
      return
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) {
      toast('生效日期须为 YYYY-MM-DD', 'error')
      return
    }
    onSave(day, Math.round(value * 10000))
  }
  return (
    <Modal onClose={onClose} ariaLabel="账户规模"
      panelClassName="w-[88vw] max-w-xs bg-surface border border-border rounded-card shadow-xl p-4">
      <div className="space-y-3">
        <div>
          <div className="text-xs font-medium text-foreground">账户规模</div>
          <div className="mt-0.5 text-[12px] text-muted">入金、出金或盈亏变化较大时更新；从生效日期起用于仓位核对，之前的日子不变。</div>
        </div>
        <label className="block space-y-1 text-[12px] text-secondary">
          <span>账户规模（万元）</span>
          <input type="number" min={0} step="0.01" inputMode="decimal" aria-label="账户规模（万元）" value={wan}
            onChange={e => setWan(e.target.value)}
            className="h-8 w-full rounded-input border border-border bg-base px-2.5 text-sm text-foreground outline-none focus:border-accent" />
        </label>
        <label className="block space-y-1 text-[12px] text-secondary">
          <span>生效日期</span>
          <input type="date" aria-label="生效日期" value={day} onChange={e => setDay(e.target.value)}
            className="h-8 w-full rounded-input border border-border bg-base px-2.5 text-sm text-foreground outline-none focus:border-accent" />
        </label>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="h-7 rounded-btn px-3 text-xs text-secondary hover:text-foreground transition-colors">取消</button>
          <button onClick={submit} disabled={saving}
            className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 transition-colors disabled:opacity-60">保存</button>
        </div>
      </div>
    </Modal>
  )
}
