import { useState } from 'react'
import type { FirstBoardRankOutcomes, FirstBoardReplayValidation } from '@/lib/api'
import { inputClass, pct } from './shared'

type Comparison = NonNullable<FirstBoardReplayValidation['priority_comparison']>
const labels = { baseline: '原候选按涨幅', sector: '个股＋板块＋时机', popularity: '再加入个股人气' }

function OutcomeRow({ label, row }: { label: string; row: FirstBoardRankOutcomes }) {
  const rate = (count: number) => row.outcomes ? pct(count / row.outcomes) : '—'
  return <tr className="border-t border-border tabular-nums">
    <th className="sticky left-0 bg-surface px-2 py-2 text-left font-medium">{label}</th>
    <td>{row.signals}</td><td>{row.outcomes}</td><td>{rate(row.touched)}</td><td>{rate(row.sealed)}</td>
    <td>{rate(row.broken)}</td><td>{row.filled} / {row.closed}</td><td>{pct(row.net_return_mean)}</td>
    <td>{pct(row.worst_net_return)}</td>
  </tr>
}

export function PriorityComparison({ report }: { report: Comparison }) {
  const [n, setN] = useState(3)
  const [holdout, setHoldout] = useState(false)
  const [segment, setSegment] = useState('all')
  const rows = report.groups.filter(row => row.top_n === n && row.window === (holdout ? 'holdout' : 'all'))
  return <section className="space-y-2">
    <h3 className="text-sm font-semibold">首板优先分对照</h3>
    <p className="text-xs text-secondary">首次可提示时已有评分 {report.scored} 个股票日，其中人气可核验 {report.popularity_known} 个。另有 {report.unscored} 个缺少当时评分，单列在原规则成交回放中。</p>
    {report.scored > 0 && <div className="flex flex-wrap items-center gap-3 text-xs">
      <label>每日名额 <select className={inputClass} value={n} onChange={event => setN(Number(event.target.value))}>
        <option value={3}>前 3 只</option><option value={5}>前 5 只</option></select></label>
      <label>入选后分组 <select className={inputClass} value={segment} onChange={event => setSegment(event.target.value)}>
        {Object.entries({ all: '全部', main: '主板', growth: '创业板／科创板', leader: '先锋', follower: '跟随',
          hot_known: '有人气数据', hot_unknown: '人气未知' }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      {report.holdout_start && <label><input type="checkbox" checked={holdout} onChange={event => setHoldout(event.target.checked)} /> 仅后段验证期（{report.holdout_start} 起）</label>}
    </div>}
    {(report.scored > 0 || report.shadow.signals > 0) && <div className="overflow-x-auto">
      <table className="w-full min-w-[780px] text-left text-xs" aria-label="首板优先分对照">
        <thead><tr>{['排序方案', '股票日', '收盘可核验', '触板率', '封板率', '触板未封占比', '入场 / 闭环', '闭环平均净收益', '闭环最差收益'].map(c =>
          <th key={c} className="px-1 py-2 font-normal text-secondary first:sticky first:left-0 first:bg-surface">{c}</th>)}</tr></thead>
        <tbody>{report.scored > 0 && rows.map(row => {
          const group = segment === 'all' ? row : row.segments?.[segment]
          return group ? <OutcomeRow key={row.variant} label={labels[row.variant]} row={group} /> :
            <tr key={row.variant}><th className="px-2 py-2 text-left font-medium">{labels[row.variant]}</th><td colSpan={8}>该组无样本</td></tr>
        })}
          {!holdout && segment === 'all' && report.shadow.signals > 0 && <OutcomeRow label="强概念弱行业（全量观察）" row={report.shadow} />}</tbody>
      </table></div>}
    <p className="text-xs leading-relaxed text-secondary">三组使用相同的首次可提示样本，按出现时间占用当日名额，仅在同一采样时点比较排名。触板、封板和触板未封均以可核验收盘样本为分母。强概念弱行业组独立回放，不占名额。</p>
  </section>
}
