// 异动监控「交易所口径」: 数据源盘后留档的严重异常波动预测与交易所重点监控名单。
// 与「偏移异动」(本地按日线推算的偏离值) 口径不同, 只列事实, 不生成交易动作。
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type AbnormalRegulatory } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toNavItems, type NavItem } from '@/lib/listNav'

const STATUS_LABEL: Record<string, string> = { reachable: '涨跌停内可触及', unreachable: '涨跌停内不可触及' }
const REASON_PREVIEW = 60  // 监控原因常有数百字: 默认只显示开头, 点开看全文
const signedPct = (v: number | null) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`)

export function RegulatoryView({ onOpenStock }: { onOpenStock: (symbol: string, name?: string, navList?: NavItem[]) => void }) {
  const q = useQuery<AbnormalRegulatory>({
    queryKey: QK.abnormalRegulatory(),
    queryFn: () => api.abnormalRegulatory(),
    staleTime: 5 * 60 * 1000,
  })
  const data = q.data
  const navList = useMemo(() => toNavItems([...(data?.anomaly ?? []), ...(data?.monitor ?? [])]), [data])

  if (q.isLoading) return <p className="py-8 text-center text-xs text-muted">正在读取交易所口径名单…</p>
  if (q.isError || !data) return <p className="py-8 text-center text-xs text-danger">读取失败，请稍后重试。</p>
  if (!data.target_day) return <p className="py-8 text-center text-xs text-muted">交易日历未覆盖，无法确定适用交易日。</p>

  const nameButton = (symbol: string, name: string | null) => (
    <button type="button" className="text-left text-foreground hover:text-accent" onClick={() => onOpenStock(symbol, name ?? undefined, navList)}>
      {name || symbol} <span className="font-mono text-[11px] text-muted">{symbol}</span>
    </button>
  )

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-auto text-xs">
      <p className="text-[11px] text-muted">
        适用于 {data.target_day} · 数据源盘后留档（严重异动预测截至 {data.anomaly_as_of ?? '—'}，监管名单截至 {data.monitor_as_of ?? '—'}）·
        与「偏移异动」的本地推算口径不同，只列事实
      </p>

      <section>
        <h3 className="mb-2 text-sm font-medium text-foreground">严重异动预测{data.anomaly ? `（${data.anomaly.length}）` : ''}</h3>
        {data.anomaly == null ? <p className="text-muted">严重异动预测留档不可用，不能视为无预测。</p>
          : !data.anomaly.length ? <p className="text-muted">当日无严重异动预测。</p>
          : (
            <table className="w-full table-auto">
              <thead className="text-left text-[11px] text-muted"><tr>
                <th className="py-1 pr-3 font-normal">股票</th><th className="py-1 pr-3 font-normal">规则</th>
                <th className="py-1 pr-3 text-right font-normal">触发价</th><th className="py-1 pr-3 text-right font-normal">需涨跌</th>
                <th className="py-1 pr-3 text-right font-normal">当前偏离</th><th className="py-1 pr-3 text-right font-normal">已用天数</th>
                <th className="py-1 font-normal">状态</th>
              </tr></thead>
              <tbody className="divide-y divide-border/50">
                {data.anomaly.map(r => (
                  <tr key={r.code}>
                    <td className="py-1.5 pr-3">{nameButton(r.symbol, r.name)}</td>
                    <td className="py-1.5 pr-3">{r.rule ?? '—'}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{r.trigger_price == null ? '—' : r.trigger_price.toFixed(2)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{signedPct(r.required_pct)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{signedPct(r.deviation_pct)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">{r.elapsed_days ?? '—'}</td>
                    <td className="py-1.5 text-secondary">{r.status ? STATUS_LABEL[r.status] ?? r.status : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </section>

      <section>
        <h3 className="mb-2 text-sm font-medium text-foreground">交易所重点监控{data.monitor ? `（${data.monitor.length}）` : ''}</h3>
        {data.monitor == null ? <p className="text-muted">监管名单留档不可用，不能视为无监控。</p>
          : !data.monitor.length ? <p className="text-muted">当日无处于监控期的股票。</p>
          : (
            <table className="w-full table-auto">
              <thead className="text-left text-[11px] text-muted"><tr>
                <th className="py-1 pr-3 font-normal">股票</th><th className="py-1 pr-3 font-normal">类型</th>
                <th className="py-1 pr-3 font-normal">监控期</th><th className="py-1 font-normal">原因</th>
              </tr></thead>
              <tbody className="divide-y divide-border/50">
                {data.monitor.map((r, i) => (
                  <tr key={`${r.code}:${i}`}>
                    <td className="py-1.5 pr-3 align-top">{nameButton(r.symbol, r.name)}</td>
                    <td className="py-1.5 pr-3 align-top">{r.label}</td>
                    <td className="whitespace-nowrap py-1.5 pr-3 align-top">{r.start} 至 {r.end}</td>
                    <td className="py-1.5 align-top leading-relaxed text-secondary">
                      {!r.reason ? '—' : r.reason.length <= REASON_PREVIEW ? r.reason : (
                        <details>
                          <summary className="cursor-pointer list-none">{r.reason.slice(0, REASON_PREVIEW)}… <span className="text-accent">展开</span></summary>
                          <p className="mt-1">{r.reason}</p>
                        </details>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </section>
    </div>
  )
}
