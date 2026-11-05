import { useState } from 'react'
import type { FirstBoardCandidate, FirstBoardPattern } from '@/lib/api'
import { money, patternLabels } from './shared'

const technicalVersion = 'ma-macd-shadow-v1'

function supportLabel(value: boolean | null) {
  if (value === true) return '符合'
  if (value === false) return '不符'
  return '未知'
}

function indicator(value: number | null) {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(4)
}

/** 只读取候选或信号当时保存的日线证据；展示状态与实时可买条件分开。 */
export function TechnicalEvidence({ facts, pattern, day, historical = false }: {
  facts: FirstBoardCandidate['evidence']; pattern: FirstBoardPattern; day: string; historical?: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const evidence = facts.technical_shadow
  if (!evidence) return <p className="mt-2 text-xs text-secondary">未记录前一交易日技术证据</p>
  if (evidence.version !== technicalVersion) return <p className="mt-2 text-xs text-secondary">技术证据版本待核验</p>
  if (evidence.pattern !== pattern) return <p className="mt-2 text-xs text-secondary">技术证据与候选形态不一致</p>
  const ref = evidence.reference_date
  const dated = !!ref && /^\d{4}-\d{2}-\d{2}$/.test(ref) && ref < day
    && (facts.reference_date == null || facts.reference_date === ref)
  const available = dated && (evidence.status === 'ready' || evidence.status === 'partial')
  return <details onToggle={event => setExpanded(event.currentTarget.open)} className="mt-2 max-w-prose break-words text-xs leading-relaxed text-secondary">
    <summary className="cursor-pointer focus-visible:outline focus-visible:outline-accent">
      {historical ? '上个交易日日线（历史）' : '上个交易日日线'} · {available
        ? `均线${supportLabel(evidence.ma_support)}／MACD${supportLabel(evidence.macd_support)}`
        : '证据不足'}
    </summary>
    {expanded && <div className="mt-2 space-y-1.5 border-l border-border pl-2">
      <p>{patternLabels[evidence.pattern]} · 参考日 {ref ?? '未知'}。仅供影子排序验证。</p>
      {!dated && <p>缺少可核验的前一交易日日期。</p>}
      {available && <>
        <p>复权日线已折算至参考日价格。收盘 {money(evidence.close)}，前收 {money(evidence.prev_close)}。</p>
        <p>MA5 {money(evidence.ma5)}，MA10 {money(evidence.ma10)}，MA20 {money(evidence.ma20)}。前一日 MA5 {money(evidence.prev_ma5)}，MA20 {money(evidence.prev_ma20)}。</p>
      </>}
      {(dated || evidence.status === 'unavailable') && evidence.ma_reason && <p>均线依据 · {evidence.ma_reason}</p>}
      {available && <p>MACD（12、26、9）DIF {indicator(evidence.macd_dif)}，DEA {indicator(evidence.macd_dea)}，柱值 {indicator(evidence.macd_hist)}，前柱 {indicator(evidence.prev_macd_hist)}。</p>}
      {(dated || evidence.status === 'unavailable') && evidence.macd_reason && <p>MACD 依据 · {evidence.macd_reason}</p>}
    </div>}
  </details>
}
