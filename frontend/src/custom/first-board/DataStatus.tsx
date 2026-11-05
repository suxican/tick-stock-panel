import { Link } from 'react-router-dom'
import type { FirstBoardDataMetric, FirstBoardDataStatus } from '@/lib/api'
import { stamp } from './shared'

const metrics: Record<FirstBoardDataMetric, string> = {
  quotes: '新鲜报价', industry: '行业排名', concept: '概念排名', minute: '原价分钟', popularity: '个股人气',
  theme: '题材旁证', ma: '上日均线', macd: '上日 MACD', priority: '首板优先分',
}
const phases = { live: '盘中观测', closed: '非连续竞价', stale: '快照过期', paused: '盯盘暂停', waiting: '等待数据', error: '读取失败' }
const statuses = { ready: '完整', partial: '部分覆盖', missing: '缺证据', inactive: '未实时评估', empty: '无候选' }
const samplingLabels: Record<string, string> = { recorded: '已采样', already_recorded: '已采样', not_due: '等待采样时点', waiting: '等待采样', paused: '暂停采样', error: '采样失败' }

export function DataStatus({ data, retained }: { data?: FirstBoardDataStatus; retained: boolean }) {
  if (!data || data.version !== 1) return <p className="text-xs text-secondary">当前快照未提供盘中数据状态摘要。</p>
  const entries = Object.keys(metrics) as FirstBoardDataMetric[]
  const realtime = { available: '可用', blocked: '受阻', unknown: '待确认' }[data.realtime_status]
  return <section aria-label="盘中数据状态摘要" className="space-y-2 border-t border-border pt-3 text-xs">
    <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h3 className="font-semibold">盘中数据状态</h3>
      <span>{retained ? '刷新失败，显示上次摘要' : phases[data.phase]}{!data.same_day && data.trade_date && ` · ${data.trade_date} 历史快照`} · {data.symbol_count} 只候选股</span>
      <span className="text-secondary">检查 {stamp(data.checked_at)}</span>
    </header>
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3 xl:grid-cols-5">
      {entries.map(key => {
        const item = data.metrics[key]
        return <div className="flex flex-wrap items-baseline justify-between gap-x-2" key={key}>
          <dt className="text-secondary">{metrics[key]}</dt>
          <dd className="tabular-nums">{!item ? '未记录' : item.status === 'empty' || item.status === 'inactive' ? statuses[item.status] : `${item.known} / ${item.total}`}</dd>
        </div>
      })}
    </dl>
    <details className="text-secondary">
      <summary className="cursor-pointer py-1 focus-visible:outline focus-visible:outline-accent">覆盖口径与缺失原因</summary>
      <div className="space-y-2 pt-1 leading-relaxed">
        <p>按候选股票去重。交易日 {data.trade_date ?? '未知'} · 快照 {stamp(data.observed_at)} · 报价有效期 {data.quote_max_age_seconds} 秒。</p>
        <p>监控{data.monitor_enabled ? '已启用' : '已暂停'} · 全局实时开关{data.realtime_enabled == null ? '未知' : data.realtime_enabled ? '已开启' : '已关闭'} · 实时行情{realtime}{data.realtime_reason && ` · ${data.realtime_reason}`}</p>
        <ul className="space-y-1">{entries.map(key => {
          const item = data.metrics[key]
          return item && <li key={key}><span className="text-foreground">{metrics[key]} · {statuses[item.status]}</span>{item.detail && `　${item.detail}`}</li>
        })}</ul>
        <p>前向采样 · {samplingLabels[data.sampling.status] ?? '待确认'} · {data.sampling.sample_count} 个采样点 · 最近 {stamp(data.sampling.last_sample_at)}{data.sampling.reason && ` · ${data.sampling.reason}`}</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {data.realtime_status === 'blocked' && <Link className="underline" to="/settings?tab=data-sources">查看实时行情数据源</Link>}
          {['partial', 'missing'].includes(data.metrics.minute?.status) && <Link className="underline" to="/settings?tab=monitoring&highlight=minute-refresh">查看全量分钟落盘</Link>}
          {(data.metrics.theme?.status === 'missing' || data.metrics.popularity?.status === 'missing') && <Link className="underline" to="/zzshare-radar">查看题材与人气榜更新</Link>}
        </div>
      </div>
    </details>
  </section>
}
