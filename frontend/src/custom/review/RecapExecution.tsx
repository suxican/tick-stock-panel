/**
 * 复盘「今日执行」—— 核验自己的交易而不是对市场的判断。
 * 数据来自批次提醒与结清留下的交易记录, 结论由后端程序给出(market_recap_execution);
 * 默认只显示当日三行摘要, 累计统计按需展开。
 */
import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import type { RecapExecution, TradeStats, TradeStatsGroup } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'

/** 样本少于此数时统计只作参考 */
const MIN_SAMPLE = 10

const signedPct = (v: number | null) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
const ratePct = (v: number | null) => (v == null ? '—' : `${v.toFixed(0)}%`)

function StockName({ symbol, name, onOpenStock, className }: {
  symbol: string
  name: string
  onOpenStock?: (symbol: string) => void
  className?: string
}) {
  if (!onOpenStock) return <span className={className}>{name}</span>
  return (
    <button onClick={() => onOpenStock(symbol)} title={`查看 ${symbol}`} className={cn('cursor-pointer hover:underline', className)}>{name}</button>
  )
}

function GroupTable({ title, groups }: { title: string; groups: TradeStatsGroup[] }) {
  if (groups.length === 0) return null
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr className="text-[11px] text-muted">
          <th className="py-1 pr-2 text-left font-normal">{title}</th>
          <th className="px-2 py-1 text-right font-normal">笔数</th>
          <th className="px-2 py-1 text-right font-normal">胜率</th>
          <th className="py-1 pl-2 text-right font-normal">平均盈亏</th>
        </tr>
      </thead>
      <tbody>
        {groups.map(g => (
          <tr key={g.key} className="border-t border-border/40">
            <td className="py-1 pr-2 text-foreground">{g.label}</td>
            <td className="px-2 py-1 text-right font-mono text-secondary">{g.total}</td>
            <td className="px-2 py-1 text-right font-mono text-secondary">{ratePct(g.win_rate)}</td>
            <td className={cn('py-1 pl-2 text-right font-mono', priceColorClass(g.avg_pnl_pct))}>{signedPct(g.avg_pnl_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Stats({ stats }: { stats: TradeStats }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="inline-flex cursor-pointer items-center gap-1 text-[11px] text-secondary hover:text-foreground"
      >
        <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
        累计 {stats.total} 笔
        {stats.total < MIN_SAMPLE && <span className="rounded-full bg-warning/15 px-1.5 text-[10px] text-warning" title={`少于 ${MIN_SAMPLE} 笔, 统计只作参考`}>样本不足</span>}
      </button>
      {open && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-x-3 text-[12px] text-secondary">
            <span>胜率 {ratePct(stats.win_rate)}</span>
            <span title="平均盈利幅度 ÷ 平均亏损幅度">盈亏比 {stats.payoff_ratio == null ? '—' : stats.payoff_ratio.toFixed(2)}</span>
            <span>平均持有 {stats.avg_hold_days == null ? '—' : `${stats.avg_hold_days.toFixed(1)} 天`}</span>
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <GroupTable title="按归因" groups={stats.by_reason} />
            <GroupTable title="按买入时阶段" groups={stats.by_phase} />
          </div>
        </div>
      )}
    </div>
  )
}

export function RecapExecutionPanel({ execution, onOpenStock }: {
  execution: RecapExecution
  onOpenStock?: (symbol: string) => void
}) {
  const { opened, closed, stop_breached: breached, stats } = execution
  const quiet = opened.length === 0 && closed.length === 0 && breached.length === 0
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="text-[11px] text-muted">今日执行(批次提醒 · 程序判定)</div>
      {quiet ? (
        <div className="text-[12px] text-secondary">今日没有新建或结清</div>
      ) : (
        <ul className="space-y-1 text-[12px]">
          {breached.length > 0 && (
            <li className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-warning">触及止损价仍持有 {breached.length} 只</span>
              {breached.map(b => (
                <span key={b.symbol} title={`止损价 ${b.stop_price.toFixed(2)} · 收盘 ${b.close.toFixed(2)}`}>
                  <StockName symbol={b.symbol} name={b.name} onOpenStock={onOpenStock} className="text-foreground" />
                </span>
              ))}
            </li>
          )}
          {closed.length > 0 && (
            <li className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-muted">结清 {closed.length} 笔</span>
              {closed.map((c, i) => (
                <span key={`${c.symbol}-${i}`}>
                  <StockName symbol={c.symbol} name={c.name} onOpenStock={onOpenStock} className="text-foreground" />{' '}
                  <span className={cn('font-mono', priceColorClass(c.pnl_pct))}>{signedPct(c.pnl_pct)}</span>
                  <span className="text-secondary"> · {c.reason_label}</span>
                </span>
              ))}
            </li>
          )}
          {opened.length > 0 && (
            <li className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-muted">新建 {opened.length} 笔</span>
              {opened.map(o => (
                <span key={o.symbol}>
                  <StockName symbol={o.symbol} name={o.name} onOpenStock={onOpenStock} className="text-foreground" />
                  {!o.has_invalidation && <span className="ml-1 text-warning">未登记失效条件</span>}
                </span>
              ))}
            </li>
          )}
        </ul>
      )}
      {stats && <Stats stats={stats} />}
      <div className="text-[10px] text-muted">盈亏为卖出价相对登记成本，不含费用</div>
    </div>
  )
}
