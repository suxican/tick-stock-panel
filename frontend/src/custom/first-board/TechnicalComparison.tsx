import { useState } from 'react'
import type { FirstBoardPattern, FirstBoardReplayValidation } from '@/lib/api'
import { inputClass, patternLabels, pct } from './shared'

type Comparison = NonNullable<FirstBoardReplayValidation['technical_comparison']>
const variants = { baseline: '原优先分', ma: '加入均线', macd: '加入 MACD', combined: '均线与 MACD' }
const columns = ['排序方案', '入选股票日', '可换股天数', '换股天数', '换入股票日', '收盘可核验', '触板率', '封板率', '触板未封占比', '入场 / 闭环', '闭环平均净收益', '闭环最差收益', '次日闭环数', '次日平均净收益']

export function TechnicalComparison({ report }: { report?: Comparison }) {
  const [n, setN] = useState(3)
  const [window, setWindow] = useState<'all' | 'holdout'>('all')
  const [pattern, setPattern] = useState<'all' | FirstBoardPattern>('all')
  if (!report) return <p className="text-xs text-secondary">此报告未记录均线与 MACD 排序对照。</p>
  if (report.version !== 'ma-macd-shadow-v1' || report.baseline !== 'priority-v1') {
    return <p className="text-xs text-secondary">此报告的技术排序规则版本待核验。</p>
  }
  const rows = report.groups.filter(row => row.top_n === n && row.window === window)
  return <section className="min-w-0 space-y-2" aria-label="均线与 MACD 影子排序">
    <h3 className="text-sm font-semibold">均线与 MACD 影子排序</h3>
    <p className="text-xs leading-relaxed text-secondary">
      按每只股票每日首次买入候选事件统计。完整可比 {report.scored} / {report.eligible} 个股票日，证据不足 {report.unscored} 个。
      原优先分可核验 {report.priority_known} 个，其中均线可核验 {report.ma_known} 个、MACD 可核验 {report.macd_known} 个。
    </p>
    {report.scored > 0 && <>
      <div className="flex flex-wrap items-end gap-3 text-xs">
        <label>每日名额<select className={inputClass} aria-label="技术影子排序每日名额" value={n} onChange={event => setN(Number(event.target.value))}>
          <option value={3}>前 3 只</option><option value={5}>前 5 只</option>
        </select></label>
        {report.holdout_start && <label>样本区间<select className={inputClass} aria-label="技术影子排序样本区间" value={window} onChange={event => setWindow(event.target.value as 'all' | 'holdout')}>
          <option value="all">全部日期</option><option value="holdout">后段验证期（{report.holdout_start} 起）</option>
        </select></label>}
        {report.groups.some(group => group.by_pattern) && <label>入选形态<select className={inputClass} aria-label="技术影子排序入选形态" value={pattern} onChange={event => setPattern(event.target.value as typeof pattern)}>
          <option value="all">全部形态</option>
          {Object.entries(patternLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>}
      </div>
      {rows.length > 0 ? <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="技术排序对照表格，可横向滚动">
        <table className="w-full min-w-[1140px] border-collapse text-left text-xs tabular-nums" aria-label="均线与 MACD 影子排序结果">
          <thead><tr>{columns.map(label => <th key={label} scope="col" className="px-2 py-2 font-normal text-secondary first:sticky first:left-0 first:bg-surface">{label}</th>)}</tr></thead>
          <tbody>{rows.map(group => {
            const row = pattern === 'all' ? group : group.by_pattern?.[pattern]
            return <tr key={group.variant} className="border-t border-border [&>td]:px-2 [&>td]:py-2">
              <th scope="row" className="sticky left-0 bg-surface px-2 py-2 font-medium">{variants[group.variant]}</th>
              {row ? <>
                <td>{row.signals}</td><td>{row.opportunity_days}</td><td>{row.changed_days}</td><td>{row.replacements}</td><td>{row.outcomes}</td>
                <td>{pct(row.outcomes ? row.touched / row.outcomes : null)}</td><td>{pct(row.outcomes ? row.sealed / row.outcomes : null)}</td>
                <td>{pct(row.outcomes ? row.broken / row.outcomes : null)}</td>
                <td>{row.filled} / {row.closed}</td><td>{pct(row.closed ? row.net_return_mean : null)}</td><td>{pct(row.closed ? row.worst_net_return : null)}</td>
                <td>{row.t1_closed}</td><td>{pct(row.t1_closed ? row.t1_net_return_mean : null)}</td>
              </> : <td colSpan={columns.length - 1}>该组未记录所选形态</td>}
            </tr>
          })}</tbody>
        </table>
      </div> : <p className="text-xs text-secondary">该区间与名额暂无可比样本。</p>}
      {pattern !== 'all' && <p className="text-xs text-secondary">先按全体候选取每日前 {n} 只，再统计入选的{patternLabels[pattern]}股票日。</p>}
      <p className="text-xs leading-relaxed text-secondary">四组按信号出现先后使用每日名额。同批次、同形态且原分相差最多 {report.score_tolerance} 分时，才按技术证据调整顺序。可换股天数指名额边界存在这样的人选。换入股票日按各方案与原优先分的入选名单比较。</p>
      <p className="text-xs leading-relaxed text-secondary">触板、封板及触板未封均以收盘可核验数为分母。次日平均净收益仅统计下一交易日已退出的模拟成交，包含交易费用。</p>
    </>}
    {report.scored === 0 && <p className="text-xs text-secondary">尚无同时记录原优先分、均线与 MACD 的可比样本。</p>}
  </section>
}
