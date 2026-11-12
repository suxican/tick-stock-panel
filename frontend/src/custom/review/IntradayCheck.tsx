/**
 * 今日盘中剧本核验条 —— 10:00 后按上一篇复盘的结构化剧本判断今天倾向哪个情景。
 * 定时任务(10:02)写入结果; 也可手动"立即核验"(只归档, 不推送)。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Timer } from 'lucide-react'
import { api, type RecapIntradayCheck } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

function Summary({ r }: { r: RecapIntradayCheck }) {
  const time = r.generated_at.slice(11, 16)
  if (r.status !== 'ok' || r.amount_30m == null) {
    return <span className="text-muted">{time} · {r.message || '暂无法判定'}</span>
  }
  const vs = r.prev_amount_30m ? (r.amount_30m / r.prev_amount_30m - 1) * 100 : null
  const leaning = r.leaning ?? []
  return (
    <span className="min-w-0 text-secondary">
      {time} · 开盘半小时成交额 <b className="font-mono text-foreground">{Math.round(r.amount_30m).toLocaleString()}亿</b>
      {vs != null && <span className="font-mono text-muted">(较上一交易日 {vs > 0 ? '+' : ''}{vs.toFixed(0)}%)</span>}
      {' → '}
      {leaning.length ? leaning.map((s, i) => (
        <span key={s.name}>
          {i > 0 && ' / '}
          倾向<b className="text-foreground">「{s.name}{s.primary ? '·主判断' : ''}」</b>
          {s.action && <span className="text-secondary">: {s.action}</span>}
        </span>
      )) : <span>未落入任何情景区间</span>}
      <span className="text-muted"> · 来自 {r.from_as_of?.slice(5)} 剧本, 全天条件待收盘</span>
    </span>
  )
}

export function IntradayCheckStrip() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.reviewIntradayCheck,
    queryFn: () => api.reviewIntradayCheck(),
    staleTime: 60_000,
  })
  const run = useMutation({
    mutationFn: () => api.reviewIntradayCheckRun(),
    onSuccess: (data) => qc.setQueryData(QK.reviewIntradayCheck, data),
    onError: () => { /* request() 已 toast */ },
  })
  // 手动核验在非交易日/10:00 前不归档, 直接显示本次返回的原因
  const result = query.data?.result ?? null
  if (query.isLoading) return null
  return (
    <div className="flex items-center gap-2 rounded-card border border-border bg-surface/80 px-3.5 py-2 text-[12px]">
      <Timer className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="shrink-0 text-muted">盘中剧本核验</span>
      <div className="min-w-0 flex-1 truncate" title={result?.message}>
        {result ? <Summary r={result} /> : <span className="text-muted">今日尚未核验(10:00 后可用)</span>}
      </div>
      <button
        type="button"
        onClick={() => run.mutate()}
        disabled={run.isPending}
        className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-0.5 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
      >
        <RefreshCw className={cn('h-3 w-3', run.isPending && 'animate-spin')} />立即核验
      </button>
    </div>
  )
}
