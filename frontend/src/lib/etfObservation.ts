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
