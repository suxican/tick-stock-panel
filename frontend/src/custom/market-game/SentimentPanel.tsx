import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import type { MarketGameSentiment } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { cn } from '@/lib/cn'
import { HistoryChart } from './HistoryChart'
import { InfoTip } from './InfoTip'

type Metric = MarketGameSentiment['metrics'][number]

const panel = 'min-w-0 rounded-card border border-border bg-surface/80 p-4'
const caution = 'text-amber-700 dark:text-warning'
// 亏钱门是风险提示而非涨跌方向，统一用警示色，不复用红涨绿跌。
const gateLabels: Record<MarketGameSentiment['loss_gate']['level'], { text: string; tone: string }> = {
  veto: { text: '暂停新增', tone: caution },
  caution: { text: '上限减半', tone: caution },
  none: { text: '未触发', tone: 'text-secondary' },
  unavailable: { text: '未生效', tone: 'text-secondary' },
}
const crossLabels = { matched: '双源一致', mismatched: '双源不一致', unavailable: '无法对账' }
const STRIP_IDS = ['earn_effect', 'lose_effect', 'loss_count'] as const

function formatValue(metric: Metric) {
  if (metric.value == null) return '—'
  if (metric.unit === 'ratio' || metric.unit === 'return') return `${(metric.value * 100).toFixed(2)}%`
  return metric.value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}
const rank = (value: number | null) => value == null ? '分位不足' : `分位 ${value.toFixed(0)}`
const stamp = (value: string | null) => value ? value.replace('T', ' ').slice(0, 16) : '未取得'

function Basis({ sentiment }: { sentiment: MarketGameSentiment }) {
  const check = sentiment.cross_check
  return <InfoTip title="短线情绪依据">
    <p>来源 zzshare 盘后统计 · 行情日 {sentiment.as_of ?? '—'} · 取得 {stamp(sentiment.fetched_at)}{sentiment.point_in_time ? '' : '（晚于信息截止，仅供研究）'}</p>
    <p>{crossLabels[check.status]}：本地涨停 {check.local_limit_up ?? '—'} / zzshare {check.vendor_limit_up ?? '—'}，本地跌停 {check.local_limit_down ?? '—'} / zzshare {check.vendor_limit_down ?? '—'}。{check.reason}</p>
    <p>亏钱门：面+大面+天地板、亏钱效应、跌超5%占比三项历史分位均值；≥{sentiment.loss_gate.veto} 暂停新增，≥{sentiment.loss_gate.caution} 上限减半。</p>
    {sentiment.sync && <p>本次同步：{sentiment.sync}</p>}
    {sentiment.revisions > 0 && <p>归档记录到 {sentiment.revisions} 次供应商历史修订，报告使用首次取得的数值。</p>}
    <ul className="space-y-1">{sentiment.limitations.map(text => <li key={text}>{text}</li>)}</ul>
  </InfoTip>
}

/** 结论依据中的一行：只放赚钱/亏钱效应、面类计数和亏钱门结论，细节进浮窗；外框由所在行提供。 */
export function SentimentStrip({ sentiment }: { sentiment?: MarketGameSentiment | null }) {
  if (!sentiment) return null
  if (sentiment.status === 'unavailable') return <p role="status" aria-label="短线情绪" className="flex flex-wrap items-center gap-x-2 text-xs text-secondary">
    暂不可用：{sentiment.reason}<Basis sentiment={sentiment} />
  </p>
  const byId = new Map(sentiment.metrics.map(metric => [metric.id, metric]))
  const gate = gateLabels[sentiment.loss_gate.level]
  return <div role="group" aria-label="短线情绪" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
    {STRIP_IDS.map(id => byId.get(id)).filter((metric): metric is Metric => !!metric).map(metric => <span key={metric.id} className="text-secondary">{metric.label} <span className="font-mono text-foreground">{formatValue(metric)}</span>（{rank(metric.percentile)}）</span>)}
    <span className={cn('font-medium', gate.tone)}>亏钱门{sentiment.loss_gate.score == null ? '' : ` ${sentiment.loss_gate.score.toFixed(0)} 分位`}：{gate.text}</span>
    <Basis sentiment={sentiment} />
  </div>
}

/** 情绪页签：完整指标与亏钱门近 60 日走势；不改写冻结报告。 */
export function SentimentPanel({ sentiment }: { sentiment?: MarketGameSentiment | null }) {
  const theme = useChartTheme()
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    tooltip: { trigger: 'axis', backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder, textStyle: { color: theme.tooltipText } },
    legend: { top: 0, textStyle: { color: theme.text, fontSize: 11 } },
    grid: { left: 35, right: 15, top: 40, bottom: 30 },
    xAxis: { type: 'category', boundaryGap: false, data: sentiment?.history.map(row => row.date), axisLabel: { color: theme.text, fontSize: 10 }, axisLine: { lineStyle: { color: theme.border } } },
    yAxis: { type: 'value', min: 0, max: 100, interval: 20, axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { lineStyle: { color: theme.grid } } },
    series: [
      { name: '亏钱门分位', type: 'line', showSymbol: false, connectNulls: false, color: '#f59e0b', data: sentiment?.history.map(row => row.loss_gate),
        markLine: { silent: true, symbol: 'none', lineStyle: { type: 'dashed', color: theme.border }, data: sentiment ? [{ yAxis: sentiment.loss_gate.caution }, { yAxis: sentiment.loss_gate.veto }] : [] } },
      { name: '赚钱效应', type: 'line', showSymbol: false, connectNulls: false, color: '#3b82f6', data: sentiment?.history.map(row => row.earn_effect) },
    ],
  }), [sentiment, theme])
  if (!sentiment) return <section aria-label="短线情绪" className={panel}><h2 className="text-sm font-semibold">短线情绪未记录</h2><p className="mt-2 text-sm text-secondary">这份报告生成于 1.3.0 之前，没有冻结 zzshare 情绪数据；重新生成计划可获取。</p></section>
  const gate = gateLabels[sentiment.loss_gate.level]
  return <section aria-label="短线情绪" className={panel}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-1 text-sm font-semibold">短线情绪 · 亏钱效应门<Basis sentiment={sentiment} /></h2>
      <span className={cn('text-sm font-semibold', gate.tone)}>{sentiment.loss_gate.score == null ? '分位不足' : `${sentiment.loss_gate.score.toFixed(0)} 分位`} · {gate.text}</span>
    </div>
    {sentiment.status === 'unavailable' ? <p className="mt-3 text-sm text-secondary">{sentiment.reason}</p> : <>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-xs sm:grid-cols-3 xl:grid-cols-5">
        {sentiment.metrics.map(metric => <div key={metric.id} className="min-w-0"><dt className="text-secondary">{metric.label}</dt><dd className="mt-1"><span className="font-mono text-sm">{formatValue(metric)}</span><span className="ml-2 text-secondary">{rank(metric.percentile)}</span></dd></div>)}
      </dl>
      {sentiment.history.some(row => row.loss_gate != null) ? <div className="mt-3"><HistoryChart option={option} height={220} label="亏钱门分位与赚钱效应近期走势" /></div> : <p className="mt-3 text-xs text-secondary">历史样本不足 60 个交易日，暂不绘制亏钱门走势。</p>}
    </>}
  </section>
}
