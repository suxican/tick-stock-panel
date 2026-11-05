import { useState } from 'react'
import { FileSearch } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import type { EtfDisclosureFund, EtfDisclosureReport, EtfObservationSnapshot } from '@/lib/api'
import { number, SourceLink } from './display'

function Report({ report, label }: { report: EtfDisclosureReport; label: string }) {
  return <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs leading-relaxed text-secondary">
    <span>{label}</span><span>报告期末 {report.period_end}</span><span>公告日 {report.published_on}</span>
    <SourceLink href={report.source_url} page={report.page}>{label}原文 · 第 {report.page} 页</SourceLink>
  </div>
}

function HoldingGroup({ fund, name }: { fund: EtfDisclosureFund; name: string }) {
  return <section aria-label={name + '持仓披露'} className="etf-holding-group">
    <header className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
      <h3 className="text-sm font-semibold">{name} <span className="num ml-1 text-xs font-normal text-secondary">{fund.symbol}</span></h3>
      <Report report={fund} label="年报" />
    </header>
    {fund.holders.length ? <table className="etf-table etf-holders">
      <thead><tr><th>报告列示持有人</th><th className="etf-right">持有份额（份）</th><th className="etf-right">占{fund.denominator.replace(/^占/, '').replace(/的?比例$/, '')}</th></tr></thead>
      <tbody>{fund.holders.map(holder => <tr key={holder.name}>
        <th scope="row">{holder.kind === 'named_plan' && <span className="etf-plan">资管计划</span>}{holder.name}</th>
        <td className="etf-right num">{number(holder.units)}</td>
        <td className="etf-right num">{number(holder.percent, 2)}%</td>
      </tr>)}</tbody>
    </table> : <p className="border-y border-border py-3 text-sm text-secondary">该年报前十名持有人名单中未列名。</p>}
    {(fund.interim_review || fund.latest_review) && <details className="mt-2 text-xs text-secondary">
      <summary className="w-fit cursor-pointer py-1">后续报告核查</summary>
      <div className="mt-1 space-y-2">
        {fund.interim_review && <Report report={fund.interim_review} label="中报" />}
        {fund.latest_review && <Report report={fund.latest_review} label="三季报" />}
      </div>
    </details>}
  </section>
}

export function Disclosures({ data }: { data: EtfObservationSnapshot }) {
  const [symbol, setSymbol] = useState('')
  const { funds, reviewed_through } = data.disclosures
  const names = new Map(data.funds.map(f => [f.symbol, f.name]))
  if (!funds.length) return <EmptyState icon={FileSearch} title="尚无已核验的汇金披露资料" hint="持仓资料按基金报告单独核验，日度份额可在份额观察中查看。" />
  const visible = symbol ? funds.filter(f => f.symbol === symbol) : funds
  return <section aria-label="汇金具名持仓">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><h2 className="text-[16px] font-semibold">汇金具名持仓披露</h2>
        <p className="mt-1 text-xs text-secondary">核查截至 {reviewed_through ?? '待核'} · 已查阅 {funds.length} 只 ETF 的基金报告</p></div>
      <label className="flex items-center gap-2 text-xs text-secondary">基金
        <select aria-label="筛选披露基金" className="etf-select" value={symbol} onChange={event => setSymbol(event.target.value)}>
          <option value="">全部 ETF</option>{funds.map(f => <option key={f.symbol} value={f.symbol}>{names.get(f.symbol) ?? f.symbol} · {f.symbol.split('.')[0]}</option>)}
        </select>
      </label>
    </div>
    <p className="mb-4 mt-3 text-sm leading-relaxed text-secondary">
      按年报前十名持有人名单列示，份额对应报告期末。
      {funds.every(f => f.interim_review && f.latest_review) && '已核查的后续中报及三季报均未披露具名持仓。'}
      {funds.some(f => f.holders.some(h => h.kind === 'named_plan')) && '直接持有与具名资管计划分行列示。'}
    </p>
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      {visible.map(fund => <HoldingGroup key={fund.symbol} fund={fund} name={names.get(fund.symbol) ?? fund.symbol} />)}
    </div>
  </section>
}
