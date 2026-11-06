import type { HuichunCalendarStatus, HuichunCandidate, HuichunCoverage, HuichunDailyRun, HuichunDailyRuns, HuichunObservation, HuichunSnapshot } from '@/lib/api'
import { pct, stamp } from './shared'

const stages = [
  ['universe', '扫描股票'], ['valid_close', '有效收盘'], ['warmup_ready', '完成预热'],
  ['qualified_waiting_first_cross', '合格前浪待金叉'], ['pending_today', '进入观察池'],
  ['first_cross_today', '今日首次金叉'], ['first_cross_rejected_today', '首次金叉未通过'], ['confirmed_today', '确认形态'],
] as const

const publicationLabels = {
  ready: '行情可用于监控',
  historical: '历史回扫，已跳过监控推送',
  stale: '行情滞后，已暂停监控推送',
  incomplete: '收盘数据待核验，已暂停监控推送',
  calendar_unverified: '交易日历待核验，已暂停监控推送',
}

function runLabel(row: HuichunDailyRun) {
  if (row.status === 'missed') return '当日漏扫'
  if (row.status === 'waiting') return '等待收盘数据'
  return row.confirmed_count === 0 ? '已扫描，零信号' : `已扫描，${row.confirmed_count} 条信号`
}

export function DailyRuns({ data }: { data: HuichunDailyRuns }) {
  const latest = data.records[0]
  return <><CalendarNotice data={data} /><details className="border-t border-border pt-3 text-xs">
    <summary className="cursor-pointer leading-relaxed">
      <span className="font-medium">盘后扫描记录</span>
      {latest && <span className="ml-2 text-secondary">{latest.date} · {runLabel(latest)}</span>}
    </summary>
    {data.calendar_status === 'unavailable' && <p className="mt-2 text-secondary">交易日历待核验，暂时无法判断漏扫日期。</p>}
    {!latest ? <p className="mt-2 text-secondary">尚无盘后扫描记录。{data.tracking_since && `自 ${data.tracking_since} 起记录。`}</p> :
      <div className="mt-2 overflow-x-auto"><table aria-label="盘后扫描记录" className="w-full min-w-[480px] text-left tabular-nums">
        <thead><tr>{['交易日', '扫描结果', '规则版本', '观察 / 对照', '备注'].map(label =>
          <th key={label} className="py-1.5 pr-3 font-normal text-secondary">{label}</th>)}</tr></thead>
        <tbody>{data.records.map(row => <tr key={row.date} className="border-t border-border">
          <td className="whitespace-nowrap py-2 pr-3">{row.date}</td><td className="pr-3">{runLabel(row)}</td>
          <td className="pr-3">{row.rule_revision ?? '—'}</td>
          <td className="whitespace-nowrap pr-3">{row.status === 'completed' ? `${row.pending_count ?? '—'} / ${row.control_count ?? '—'}` : '—'}</td>
          <td className="text-secondary">{[
            row.reason, row.trigger === 'daily_sync' && '盘后自动扫描', row.trigger === 'manual' && '手动扫描',
            row.correction_count > 0 && `结果已修订 ${row.correction_count} 次`,
            row.backfilled_at && `历史补扫 ${stamp(row.backfilled_at)}`,
          ].filter(Boolean).join(' · ') || '—'}</td>
        </tr>)}</tbody>
      </table></div>}
  </details></>
}

function CalendarNotice({ data }: { data: HuichunCalendarStatus }) {
  const state = data.calendar_validity_status
  if (state !== 'expiring' && state !== 'expired') return null
  return <p role="status" aria-label="交易日历状态" className="my-3 text-xs leading-relaxed text-warning">
    {state === 'expired' ? `交易日历已于 ${data.calendar_coverage_end} 到期，监控与新收益计算等待日历更新。`
      : `交易日历覆盖至 ${data.calendar_coverage_end}，剩余 ${data.calendar_days_remaining} 天。请在到期前更新交易日历。`}
  </p>
}

export function StageCounts({ coverage, day }: { coverage: HuichunCoverage; day: string }) {
  const counts = coverage.stage_counts
  if (!counts) return <p className="mt-3 text-xs text-secondary">此快照尚无截止日分项统计，可重新筛选生成。</p>
  const checks = coverage.publication_checks
  const rejectionLabels: Record<string, string> = { zero_distance_exceeded: '零轴距离超限',
    close_not_above_ma: '收盘未高于均线', ma_not_rising: '均线未抬升', indicator_history_insufficient: '指标历史不足' }
  const reasons = Object.entries(coverage.first_cross_rejection_reasons ?? {})
    .filter(([key, count]) => key in rejectionLabels && count > 0)
  return <section aria-label="截止日筛选统计" className="my-4 border-y border-border py-3">
    {coverage.history_truncated && <p className="mb-3 text-xs text-secondary">可核验计算区间自 {coverage.calculation_start_date} 起，较早行情未参与指标计算，预热从此重新累计。</p>}
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-xs font-medium">{day} 筛选统计</h3>
      {coverage.publication_status && <span className="text-xs text-secondary">{publicationLabels[coverage.publication_status]}
        {coverage.publication_status === 'stale' && coverage.expected_daily_date && ` · 应到 ${coverage.expected_daily_date}`}</span>}
    </div>
    <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
      {stages.map(([key, label]) => <div key={key} className="flex items-baseline justify-between gap-2 text-xs">
        <dt className="text-secondary">{label}</dt><dd className="font-medium tabular-nums">{counts[key].toLocaleString('zh-CN')}</dd>
      </div>)}
    </dl>
    {reasons.length > 0 && <p className="mt-3 text-xs text-secondary">首次金叉未通过的条件（同一股票可占多项） · {reasons.map(([key, count]) => `${rejectionLabels[key]} ${count}`).join('、')}</p>}
    {!!coverage.factor_issue_dates?.length && <details className="mt-3 text-xs text-secondary">
      <summary className="cursor-pointer">复权异常最集中的日期</summary>
      <p className="mt-1">按每只股票首次异常日统计。{coverage.factor_issue_dates.slice(0, 5).map(item => `${item.date}（${item.count} 只）`).join('、')}</p>
    </details>}
    {checks && coverage.publication_status === 'incomplete' && <p className="mt-3 text-xs text-secondary">
      当日有成交 {checks.active_symbols} 只，上个交易日 {checks.previous_active_symbols ?? '—'} 只，最低覆盖 {pct(checks.min_coverage_ratio)}。
      {checks.finalized_symbols != null && `已核准 ${checks.finalized_symbols} 只，`}待定版 {checks.unfinalized_symbols} 只。{checks.known_raw_issue && '原始日线待修复。'}
    </p>}
  </section>
}

export function ShapeDetails({ row }: { row: HuichunCandidate }) {
  if (row.pullback_bars == null) return null
  return <details className="mt-1 whitespace-nowrap text-secondary"><summary className="cursor-pointer">回调结构</summary>
    <p className="mt-1" title="D 收盘到 D～R 最低复权收盘的跌幅金额，占 G～D 收盘上涨金额的比例">
      前浪回吐 {pct(row.pullback_fraction)}</p>
    <p>死叉至确认 {row.pullback_bars} 根</p><p>低点至确认 {pct(row.recovery_return, true)}</p>
    {row.first_watch_date && <p>首次观察 {row.first_watch_date} · 提前 {row.watch_lead_bars ?? '—'} 根</p>}
  </details>
}

export function ThresholdCell({ row }: { row: HuichunObservation }) {
  const value = row.cross_threshold_return
  if (value == null || !Number.isFinite(value)) return <td className="px-3 py-3 text-secondary">—</td>
  const check = (pass: boolean | null | undefined) => pass == null ? '待核验' : pass ? '通过' : '未通过'
  return <td className="whitespace-nowrap px-3 py-3 tabular-nums">
    <span>{value < 0 ? '平收可金叉' : value === 0 ? '需高于平收' : `涨幅须超过约 ${pct(value)}`}</span>
    <details className="mt-1 text-secondary"><summary className="cursor-pointer">临界价约 {row.cross_threshold_price?.toFixed(4) ?? '—'} 元</summary>
      <p>临界处：零轴{check(row.threshold_zero_pass)}，趋势{check(row.threshold_trend_pass)}</p>
      {row.first_watch_date && <p>首次观察 {row.first_watch_date}</p>}
    </details>
  </td>
}

export function WatchConversion({ scan }: { scan: NonNullable<HuichunSnapshot['scan']> }) {
  if (!scan.watch_conversion) return null
  return <details className="mt-4 border-t border-border pt-3 text-xs">
    <summary className="cursor-pointer font-medium">首次进入观察池后的确认率</summary>
    <p className="my-2 text-secondary">取所选区间首次进入观察池的周期，每轮只计一次。按后续有效日线计时，确认率仅使用截至 {scan.observation_date ?? scan.end_date} 能判定的周期。</p>
    <div className="overflow-x-auto"><table className="min-w-[520px] text-left tabular-nums" aria-label="观察池转化">
      <thead><tr>{['观察窗口', '全部周期', '可判定', '确认', '确认率', '缺口未知', '待到期'].map(label => <th key={label} className="py-1 pr-4 font-normal text-secondary">{label}</th>)}</tr></thead>
      <tbody>{scan.watch_conversion.map(row => <tr key={row.horizon}>
        {[row.horizon + ' 根内', row.total, row.resolved, row.confirmed, pct(row.resolved ? row.confirmed / row.resolved : null), row.gap_unknown, row.pending].map((v, i) => <td key={i} className="py-1 pr-4">{v}</td>)}
      </tr>)}</tbody>
    </table></div>
  </details>
}
