import type { HoldingFundEvidence, PositionTFundEvidence } from '@/lib/api'
import { cn } from '@/lib/cn'
import { formatTime } from './positionTShared'

export const FUND_UNAVAILABLE = '资金数据不可用'

/** 元 → 亿元/万元; 不可用时调用方不得走到这里 (不显示 0) */
export function yuan(value: number, signed = true) {
  const abs = Math.abs(value)
  const sign = signed ? (value > 0 ? '+' : value < 0 ? '−' : '') : ''
  return abs >= 1e8 ? `${sign}${(abs / 1e8).toFixed(2)} 亿元` : `${sign}${(abs / 1e4).toFixed(0)} 万元`
}
// A 股惯例: 红色流入/上涨, 绿色流出/下跌; 同时保留正负号与文字
const tone = (value: number) => value > 0 ? 'text-bull' : value < 0 ? 'text-bear' : 'text-foreground'
const md = (day: string | null | undefined) => day ? day.slice(5) : '未知'
const hm = (value: string | null) => value ? formatTime(value) : '未知'

function Unavailable({ what }: { what: string }) {
  return <li>{what}：{FUND_UNAVAILABLE}，不能视为无资金流入或流出。</li>
}

/** 持仓分析展开区「资金」栏: 只展示, 不打分; 主列表不出现 */
export function HoldingFundDetails({ value }: { value?: HoldingFundEvidence }) {
  if (!value) return null
  const { main_net: m, dragon_tiger: d, margin: g, northbound: n } = value
  return <div data-testid="fund-evidence">
    <h5 className="font-medium text-foreground">资金</h5>
    <ul className="mt-2 list-disc space-y-1 pl-4 leading-relaxed text-secondary">
      {m.status === 'unavailable' ? <Unavailable what="主力净额" />
        : m.status === 'empty' ? <li>近 5 个交易日无主力净额记录</li>
        : <li>近 {m.days?.length ?? 0} 个交易日主力净额合计 <span className={cn('font-mono', tone(m.total ?? 0))}>{yuan(m.total ?? 0)}</span>（{m.direction === 'inflow' ? '净流入' : m.direction === 'outflow' ? '净流出' : '持平'}，截至 {md(m.data_through)}，{m.source ?? '来源未知'}，厂商模型口径）{!!m.missing_days?.length && <span>，缺 {m.missing_days.length} 日</span>}
          <details className="mt-1"><summary className="cursor-pointer text-accent">每日明细</summary><ul className="mt-1 grid grid-cols-2 gap-x-4 sm:grid-cols-5">{m.days?.map(day => <li key={day.date}>{md(day.date)} <span className={cn('font-mono', tone(day.main_net))}>{yuan(day.main_net)}</span></li>)}</ul></details></li>}
      {d.status === 'unavailable' ? <Unavailable what="龙虎榜" />
        : d.status === 'empty' ? <li>近 5 个交易日未上龙虎榜{!!d.missing_days?.length && `（${d.missing_days.length} 日榜单未取到）`}</li>
        : <li>近 5 个交易日上龙虎榜 {d.listings?.length ?? 0} 次：{d.listings?.map(l => `${md(l.date)} 净买 ${yuan(l.net_value)}${l.reasons.length ? `（${l.reasons.join('；')}）` : ''}`).join('；')}{!!d.hot_money?.length && <span>。游资席位：{d.hot_money.map(s => `${s.name ?? '未知席位'} ${md(s.date)}${s.net_value == null ? '' : ` ${yuan(s.net_value)}`}`).join('；')}（{d.hot_money_basis ?? '营业部简称近似匹配'}）</span>}{!!d.missing_days?.length && `。${d.missing_days.length} 日榜单未取到`}</li>}
      {g.status === 'unavailable' ? <Unavailable what="融资余额" />
        : g.status === 'empty' ? <li>融资余额：非两融标的或无记录</li>
        : <li>融资余额 <span className="font-mono text-foreground">{yuan(g.balance ?? 0, false)}</span>{g.change == null ? '，比较基准不足' : <span>，较 {md(g.compare_date)} <span className={cn('font-mono', tone(g.change))}>{yuan(g.change)}</span>{g.change_pct != null && `（${g.change_pct >= 0 ? '+' : ''}${(g.change_pct * 100).toFixed(1)}%）`}</span>}（数据日 {md(g.data_through)}，T-1）</li>}
      {n.status === 'unavailable' ? <Unavailable what="北向持股" />
        : n.status === 'empty' ? <li>北向持股：无记录</li>
        : <li>北向持股 {n.data_through}：{n.ratio == null ? '占比未知' : `占 A 股 ${(n.ratio * 100).toFixed(2)}%`}{n.ratio_change != null && <span>，较上期 <span className={cn('font-mono', tone(n.ratio_change))}>{n.ratio_change >= 0 ? '+' : ''}{(n.ratio_change * 100).toFixed(2)} 个百分点</span></span>}（季度披露，非实时）</li>}
    </ul>
    <p className="mt-1 text-secondary">单位元；取数时刻 {hm(value.available_at)}。主力净额为厂商模型口径，仅作参考，不打分、不进入情景与条件判定。</p>
  </div>
}

/** 做T 信号详情一行: 当日累计主力净额 + 取数时刻; 候选行不显示 */
export function SignalFundLine({ value }: { value?: PositionTFundEvidence | null }) {
  if (!value) return null
  return <div data-testid="signal-fund"><dt className="inline">当日主力净额 </dt><dd className="inline">{value.status === 'ok' && value.main_net != null
    ? <><span className={cn('font-mono', tone(value.main_net))}>{yuan(value.main_net)}</span>（当日累计，截至取数时刻 {hm(value.available_at)}；{value.refresh_hint ?? '约 4 分钟更新一次'}；厂商模型口径，仅作参考）</>
    : value.status === 'empty' ? '无记录' : `${FUND_UNAVAILABLE}，不能视为无资金流入或流出`}</dd></div>
}
