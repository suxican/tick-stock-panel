/**
 * 设置→系统区的「开盘啦（非官方）」开关 — 经 settings.system.section 插槽渲染。
 * 开启后连板梯队页叠加开盘啦盘中情绪、涨停原因与题材; 删除本扩展即整行消失。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { useKaipanlaStatus } from '@/lib/useKaipanla'

function clock(iso: string | null): string | null {
  return iso ? iso.slice(11, 19) : null
}

export function KaipanlaSettings() {
  const qc = useQueryClient()
  const status = useKaipanlaStatus()
  const enabled = !!status.data?.enabled
  const toggle = useMutation({
    mutationFn: (next: boolean) => api.kaipanlaSetEnabled(next),
    onSettled: () => qc.invalidateQueries({ queryKey: QK.kaipanlaAll }),
  })
  const success = clock(status.data?.last_success_at ?? null)
  const error = status.data?.last_error
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-sm text-foreground">开盘啦（非官方）</div>
        <div className="text-[11px] text-muted">非官方接口，仅供个人研究；App 升级可能失效</div>
        {enabled && (success || error) && (
          <div className="text-[11px] text-muted truncate">
            {success && <span>最近成功 {success}</span>}
            {error && <span className="ml-2 text-warning">最近错误：{error}</span>}
          </div>
        )}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        aria-label="启用开盘啦数据"
        disabled={status.isLoading || toggle.isPending}
        onClick={() => toggle.mutate(!enabled)}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors duration-200 disabled:opacity-50',
          enabled ? 'border-accent/50 bg-accent' : 'border-border bg-elevated hover:border-muted',
        )}
      >
        <span
          className={cn(
            'inline-block h-3.5 w-3.5 rounded-full border border-black/5 bg-white shadow-sm transition-transform duration-200',
            enabled ? 'translate-x-[18px]' : 'translate-x-0.5',
          )}
        />
      </button>
    </div>
  )
}
