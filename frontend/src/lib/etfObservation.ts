export type EtfFlowWindow = 1 | 20 | 60
export type EtfObservationScope = 'fund_total_verified' | 'listed_only' | 'unverified'

export interface EtfFlowAmounts {
  fund_net_estimate_cny: number | null
  listed_change_value_cny: number | null
}

export interface EtfObservationDay extends EtfFlowAmounts {
  symbol: string
  date: string
  previous_date: string | null
  previous_listed_units: number | null
  listed_units: number | null
  unit_nav_cny: number | null
  listed_change_units: number | null
  fund_net_units: number | null
  scope: EtfObservationScope
  status: string
  shares_source: string | null
  previous_shares_source: string | null
  nav_source: string | null
}

export interface EtfObservationFund {
  symbol: string
  name: string
  scope: EtfObservationScope
  reason: string
  source_url: string
  period_fund_net_estimate_cny: number | null
  period_listed_change_value_cny: number | null
  days_covered: number
  latest: EtfObservationDay | null
}

export interface EtfDisclosureReport {
  report_title: string
  period_end: string
  published_on: string
  source_url: string
  page: number
}

export interface EtfDisclosureFund extends EtfDisclosureReport {
  symbol: string
  denominator: string
  holders: { name: string; units: number; percent: number; kind: 'direct' | 'named_plan' }[]
  interim_review?: EtfDisclosureReport
  latest_review?: EtfDisclosureReport
}

export interface EtfObservationSnapshot {
  dates: string[]
  as_of: string | null
  latest_observed_date: string | null
  latest_complete_date: string | null
  fund_pool_size: number
  listed_pool_size: number
  window: EtfFlowWindow
  start_date: string | null
  session_count: number
  totals: EtfFlowAmounts
  trend: (EtfFlowAmounts & { date: string })[]
  funds: EtfObservationFund[]
  disclosures: { reviewed_through: string | null; funds: EtfDisclosureFund[] }
}

export interface EtfObservationDetail {
  symbol: string
  as_of: string | null
  window: EtfFlowWindow
  rows: EtfObservationDay[]
}

export type EtfTurnoverLevel = 'strong' | 'mild' | 'calm'

export interface EtfTurnoverFund {
  symbol: string
  name: string
  amount_cny: number | null
  avg_amount_cny: number | null
  /** 已达成 20 日均额的比例，不做时点对齐 */
  progress: number | null
  /** 收盘后为全天倍数；盘中为同时点倍数，缺分钟基准时为 null */
  ratio: number | null
  /** 小数比例，0.01 表示 1% */
  change_pct: number | null
  /** 最近核实的场内份额 × 现价（元） */
  size_cny?: number | null
  listed_units?: number | null
  listed_change_units?: number | null
  /** 2025 年报前十大持有人中汇金两家主体合计份额；0 表示未列名，null 表示无披露记录 */
  huijin_units?: number | null
  /** 与 recent_dates 对齐，最早到最近 */
  recent_amounts_cny?: (number | null)[]
}

export interface EtfTurnoverSnapshot {
  as_of: string | null
  previous_date?: string | null
  baseline_start?: string | null
  baseline_end?: string | null
  live?: boolean
  elapsed_minutes?: number | null
  data_time?: string | null
  stale?: boolean
  funds: EtfTurnoverFund[]
  totals: {
    amount_cny: number | null
    avg_amount_cny: number | null
    ratio: number | null
    progress: number | null
    weighted_change_pct: number | null
    /** 收盘后 14:30–15:00 成交额占全天比例，小数 */
    tail_share?: number | null
    tail_share_avg?: number | null
  } | null
  recent_dates?: string[]
  huijin_period_end?: string | null
  conclusion: EtfTurnoverLevel | null
  thresholds: { strong: number; mild: number }
  shares: {
    series: { date: string; listed_change_value_cny: number | null; index: number | null }[]
    units_as_of: string | null
  }
}
