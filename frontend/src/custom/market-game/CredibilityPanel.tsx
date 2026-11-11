import { useQuery } from '@tanstack/react-query'
import { api, type MarketGameCalibration, type MarketGameCalibrationRow, type MarketGameCalibrationSegment } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { InfoTip } from './InfoTip'

type Archived = Exclude<MarketGameCalibration, { status: 'missing' }>
// 结论是“样本内两段是否同向”，不是涨跌方向，因此不使用红涨绿跌配色。
const verdicts: Record<MarketGameCalibrationRow['verdict'], { text: string; tone: string }> = {
  consistent: { text: '方向一致', tone: 'border-accent/50 text-accent' },
  inconsistent: { text: '方向不一致', tone: 'border-warning/50 text-amber-700 dark:text-warning' },
  insufficient: { text: '样本不足', tone: 'border-border text-secondary' },
}
const ARCHIVE_COMMAND = 'uv run --frozen python -m scripts.calibrate_market_game --split 2023-01-01 --archive'
const diffFormatter = new Intl.NumberFormat('zh-CN', { style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' })
const diff = (value: number | null) => value == null ? '—' : diffFormatter.format(value)
const days = (segment: MarketGameCalibrationSegment) => `${segment.n}/${segment.control_n}`

/** 校准归档只读查询；总览的依据标签与复盘页共用同一缓存。 */
export function useCalibration() {
  return useQuery({ queryKey: QK.marketGameCalibration, queryFn: api.marketGameCalibration, retry: false, staleTime: 5 * 60_000 })
}

function archived(data: MarketGameCalibration | undefined): Archived | null {
  return data && data.status !== 'missing' && 'rows' in data ? data : null
}

/** 依据行的可信度标签：有归档时取对应分组结论，否则明示未校准。 */
export function calibrationTag(data: MarketGameCalibration | undefined, group: string) {
  const row = archived(data)?.rows.find(item => item.group === group)
  return row ? `样本内${verdicts[row.verdict].text}` : '未校准'
}

function Row({ row }: { row: MarketGameCalibrationRow }) {
  const verdict = verdicts[row.verdict]
  return <tr className="border-b border-border/60">
    <td className="py-2.5 pr-4">{row.label}{row.card && <span className="ml-2 text-xs text-secondary">{row.card}</span>}</td>
    <td className="py-2.5 pr-4 font-mono text-xs">{days(row.train)} · {days(row.test)}</td>
    <td className="whitespace-nowrap py-2.5 pr-4 font-mono">{diff(row.train.diff)} → {diff(row.test.diff)}</td>
    <td className="py-2.5"><span className={cn('inline-block rounded border px-1.5 py-0.5 text-xs', verdict.tone)}>{verdict.text}</span></td>
  </tr>
}

export function CredibilityPanel() {
  const query = useCalibration()
  const data = archived(query.data)
  return <section aria-label="规则可信度" className="min-w-0 rounded-card border border-border bg-surface/80 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-1 text-sm font-semibold">规则可信度<InfoTip title="规则可信度说明">
        <p>样本内全市场代理：比较信号组与对照组的次日等权涨幅之差。训练段与测试段各需至少 {data?.min_days ?? 30} 天，两段同号才记为方向一致。</p>
        <p>方向一致不是收益或胜率保证，也不会自动改变规则；多组同时检验存在多重比较风险。</p>
        {data && <>
          <p>天数列为“信号/对照”，先训练段后测试段。归档 {data.archive} · 生成 {data.created_at.replace('T', ' ').slice(0, 16)}{data.skipped_archives > 0 && `，跳过 ${data.skipped_archives} 份无法读取的较新归档`}</p>
          {data.rows.some(row => row.definition) && <ul className="space-y-1">{data.rows.filter(row => row.definition).map(row => <li key={row.group}>{row.label}：{row.definition}</li>)}</ul>}
          <ul className="space-y-1">{data.limitations.map(item => <li key={item}>{item}</li>)}</ul>
        </>}
      </InfoTip></h2>
      {data && <span className="text-xs text-secondary">{data.start} 至 {data.end} · 分段 {data.split}</span>}
    </div>
    {query.isLoading && <p role="status" className="mt-3 text-sm text-secondary">正在读取校准归档…</p>}
    {query.isError && <div role="alert" className="mt-3 flex flex-wrap items-center gap-2 text-sm"><p>校准归档读取失败，规则计划不受影响。</p><button type="button" className="min-h-9 rounded-btn border border-border px-3 text-sm hover:bg-elevated" onClick={() => void query.refetch()}>重试</button></div>}
    {query.data?.status === 'missing' && <div className="mt-3 text-sm text-secondary"><p>尚无校准归档。在 backend 目录运行离线校准后刷新：</p><code className="mt-2 block break-all rounded bg-elevated px-2 py-1.5 font-mono text-xs text-foreground">{ARCHIVE_COMMAND}</code></div>}
    {data && <div className="mt-2 overflow-x-auto"><table className="w-full min-w-[600px] text-left text-sm">
      <caption className="sr-only">规则与影子检验的样本内方向一致性</caption>
      <thead className="border-b border-border text-xs text-secondary"><tr>{['分组', '天数（训练 · 测试）', '次日等权差（训练 → 测试）', '结论'].map(label => <th key={label} scope="col" className="py-2 pr-4 font-medium">{label}</th>)}</tr></thead>
      <tbody>
        {data.rows.filter(row => row.kind === 'rule').map(row => <Row key={row.group} row={row} />)}
        {data.rows.some(row => row.kind === 'shadow') && <tr><th scope="rowgroup" colSpan={4} className="pt-3 pb-1 text-xs font-medium text-secondary">影子检验 · 不参与规则</th></tr>}
        {data.rows.filter(row => row.kind === 'shadow').map(row => <Row key={row.group} row={row} />)}
      </tbody>
    </table></div>}
  </section>
}
