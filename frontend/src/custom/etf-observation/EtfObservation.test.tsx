// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EtfObservationDay, EtfObservationSnapshot, EtfTurnoverSnapshot } from '@/lib/api'
import EtfObservation from './EtfObservation'

const calls = vi.hoisted(() => ({ snapshot: vi.fn(), detail: vi.fn(), turnover: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { etfObservation: calls.snapshot, etfObservationDetail: calls.detail, etfTurnover: calls.turnover } }))

const day: EtfObservationDay = {
  symbol: '510330.SH', date: '2026-11-04', previous_date: '2026-11-03',
  previous_listed_units: 1000, listed_units: 1200, listed_change_units: 200, unit_nav_cny: 1.2345,
  scope: 'unverified', listed_change_value_cny: 246.9, fund_net_units: null, fund_net_estimate_cny: null,
  status: 'ok', shares_source: 'https://example.org/shares', previous_shares_source: 'https://example.org/previous',
  nav_source: 'https://example.org/nav',
}
const snapshot: EtfObservationSnapshot = {
  dates: ['2026-11-03', day.date], as_of: day.date, latest_observed_date: day.date,
  window: 1, start_date: day.date, session_count: 1,
  latest_complete_date: day.date, fund_pool_size: 7, listed_pool_size: 10,
  totals: { fund_net_estimate_cny: 0, listed_change_value_cny: 123456789 },
  trend: [], funds: [{
    symbol: day.symbol, name: '华夏沪深300', scope: day.scope, reason: '场外申赎的登记范围待核。',
    source_url: 'https://example.org/scope', days_covered: 1,
    period_fund_net_estimate_cny: null, period_listed_change_value_cny: 246.9, latest: day,
  }],
  disclosures: { reviewed_through: '2026-11-05', funds: [{
    symbol: day.symbol, report_title: '年度报告', period_end: '2025-12-31', published_on: '2026-03-31',
    source_url: 'https://example.org/report.pdf', page: 37, denominator: '上市总份额',
    holders: [{ kind: 'direct', name: '中央汇金投资有限责任公司', units: 16000000, percent: 0.72 },
      { kind: 'named_plan', name: '具名资产管理计划', units: 100000, percent: 0.01 }],
    interim_review: { report_title: '中期报告', period_end: '2026-06-30', published_on: '2026-08-31',
      source_url: 'https://example.org/interim.pdf', page: 39 },
    latest_review: { report_title: '三季报', period_end: '2026-09-30', published_on: '2026-10-27',
      source_url: 'https://example.org/q3.pdf', page: 12 },
  }] },
}
const turnover: EtfTurnoverSnapshot = {
  as_of: '2026-11-06', previous_date: '2026-11-05', baseline_start: '2026-10-09', baseline_end: '2026-11-05',
  live: false, elapsed_minutes: null, data_time: null, stale: false, conclusion: 'strong',
  thresholds: { strong: 1.8, mild: 1.3 },
  totals: { amount_cny: 95e9, avg_amount_cny: 50e9, ratio: 1.9, progress: 1.9, weighted_change_pct: 0.0112, tail_share: 0.123, tail_share_avg: 0.101 },
  recent_dates: ['2026-11-05', '2026-11-06'], huijin_period_end: '2025-12-31',
  funds: [
    { symbol: '159922.SZ', name: '嘉实中证500', amount_cny: 319e6, avg_amount_cny: null, progress: null, ratio: null, change_pct: -0.0103,
      size_cny: 99.5e8, listed_units: 36.4e8, listed_change_units: 2.13e8, huijin_units: 23.1e8, recent_amounts_cny: [300e6, 319e6] },
    { symbol: '588000.SH', name: '华夏科创50', amount_cny: 9276e6, avg_amount_cny: 9000e6, progress: 1.03, ratio: 1.03, change_pct: 0.0284,
      size_cny: 1479.9e8, listed_units: 1076.3e8, listed_change_units: -4.5e8, huijin_units: 0, recent_amounts_cny: [null, 9276e6] },
    { symbol: '510300.SH', name: '华泰柏瑞沪深300', amount_cny: 4372e6, avg_amount_cny: 2300e6, progress: 1.9, ratio: 1.9, change_pct: 0.0036,
      size_cny: 1923.2e8, listed_units: 463.4e8, listed_change_units: -0.86e8, huijin_units: 735.1e8, recent_amounts_cny: [4000e6, 4372e6] },
  ],
  shares: { units_as_of: '2026-11-04', series: [
    { date: '2026-08-05', listed_change_value_cny: -13e9, index: 100 },
    { date: '2026-08-06', listed_change_value_cny: null, index: null },
    { date: '2026-11-04', listed_change_value_cny: 37.6e9, index: 197.16 },
  ] },
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.snapshot.mockReset().mockResolvedValue(snapshot)
  calls.turnover.mockReset().mockResolvedValue(turnover)
  calls.detail.mockReset().mockResolvedValue({ symbol: day.symbol, as_of: day.date, window: 1, rows: [day] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
async function render() { await act(async () => root.render(<QueryClientProvider client={client}><EtfObservation /></QueryClientProvider>)); await settle() }
async function click(text: string) {
  const el = [...host.querySelectorAll('button')].find(el => el.textContent?.includes(text))
  expect(el, text).toBeDefined()
  await act(async () => el!.click()); await settle()
}

it('converts yuan to 亿元 with sign and opens the sources behind a fund', async () => {
  await render()
  expect(host.textContent).toContain('+1.23')
  expect(host.textContent).toContain('0.00')
  expect(host.textContent).toContain('全基金待核')
  await click('华夏沪深300')
  expect(host.textContent).toContain('1,200')
  expect(host.textContent).toContain('246.90')
  expect(host.querySelector('a[href="https://example.org/previous"]')).not.toBeNull()
  expect(host.querySelector('a[href="https://example.org/nav"]')).not.toBeNull()
})

it('uses the new window, withholds missing totals and clears prior drill-down', async () => {
  calls.snapshot.mockImplementation(async (window: number) => window === 1 ? snapshot : {
    ...snapshot, window, totals: { fund_net_estimate_cny: null, listed_change_value_cny: null },
  })
  await render()
  await click('华夏沪深300')
  await click('近20日')
  expect(calls.snapshot).toHaveBeenCalledWith(20, undefined)
  expect(host.textContent).toContain('数据未齐')
  expect(host.textContent).not.toContain('+1.23')
  expect(host.querySelector('a[href="https://example.org/previous"]')).toBeNull()
})

it('shows the holding with its reported denominator and both publication and period dates', async () => {
  await render()
  await click('汇金披露')
  expect(host.textContent).toContain('中央汇金投资有限责任公司')
  expect(host.textContent).toContain('0.72%')
  expect(host.textContent).toContain('资管计划')
  expect(host.textContent).toContain('2025-12-31')
  expect(host.textContent).toContain('2026-03-31')
  expect(host.textContent).toContain('上市总份额')
  expect(host.textContent).toContain('2026-06-30')
  expect(host.textContent).toContain('2026-08-31')
  expect(host.querySelector('a[href="https://example.org/report.pdf#page=37"]')).not.toBeNull()
  expect(host.querySelector('a[href="https://example.org/q3.pdf#page=12"]')).not.toBeNull()
})

it('keeps a reviewed report without a named holder distinct from missing coverage', async () => {
  calls.snapshot.mockResolvedValue({ ...snapshot, disclosures: {
    ...snapshot.disclosures, funds: [{ ...snapshot.disclosures.funds[0], holders: [] }],
  } })
  await render(); await click('汇金披露')
  expect(host.textContent).toContain('该年报前十名持有人名单中未列名')
  expect(host.textContent).not.toContain('0.00%')
})

it('offers a retry after a request failure', async () => {
  calls.snapshot.mockRejectedValueOnce(new Error('读取失败'))
  await render()
  await click('重试')
  expect(host.textContent).toContain('华夏沪深300')
})

it('reads a single fund’s window on demand and changes the inspected valuation date', async () => {
  calls.snapshot.mockImplementation(async (window: number) => ({ ...snapshot, window }))
  calls.detail.mockResolvedValue({
    symbol: day.symbol, as_of: day.date, window: 20, rows: [
      { ...day, date: '2026-11-03', unit_nav_cny: 1.1111, nav_source: 'https://example.org/older' }, day,
    ],
  })
  await render(); await click('近20日')
  expect(calls.detail).not.toHaveBeenCalled()
  await click('华夏沪深300')
  expect(calls.detail).toHaveBeenCalledWith(day.symbol, 20, day.date)
  const select = [...host.querySelectorAll('select')].find(s => s.parentElement?.textContent?.includes('核对交易日'))!
  await act(async () => { select.value = '2026-11-03'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.textContent).toContain('1.1111')
  expect(host.querySelector('a[href="https://example.org/older"]')).not.toBeNull()
  expect(host.querySelector('a[href="https://example.org/nav"]')).toBeNull()
})

it('keeps dated disclosure records available when there is no local daily dataset', async () => {
  calls.snapshot.mockResolvedValue({ ...snapshot, dates: [], as_of: null, latest_complete_date: null, latest_observed_date: null,
    start_date: null, session_count: 0, funds: [], totals: { fund_net_estimate_cny: null, listed_change_value_cny: null } })
  await render()
  expect(host.textContent).toContain('暂无可汇总的份额数据')
  await click('汇金披露')
  expect(host.textContent).toContain('中央汇金投资有限责任公司')
})

it('reads turnover independently of the share ledger and states the settled verdict', async () => {
  calls.snapshot.mockRejectedValue(new Error('份额读取失败'))
  await render(); await click('成交与资金')
  expect(host.textContent).toContain('成交显著放大')
  expect(host.textContent).toContain('1.90×')
  expect(host.textContent).toContain('950.0')
  expect(host.textContent).toContain('+1.12%')
  expect(host.textContent).toContain('-1.03%')
  expect(host.textContent).toContain('缺 20 日成交额')
  expect(host.textContent).toContain('197.2')
  expect([...host.querySelectorAll('dt')].map(dt => dt.textContent)).not.toContain('已达成日均')
  expect(host.textContent).not.toContain('份额读取失败')
})

it('withholds the intraday multiple without a minute baseline and flags a stopped feed', async () => {
  const live = { ...turnover, live: true, data_time: '09:47', conclusion: null,
    totals: { ...turnover.totals!, ratio: null, progress: 0.09 } }
  calls.turnover.mockResolvedValue(live)
  await render(); await click('成交与资金')
  expect(host.textContent).toContain('截至 09:47')
  expect(host.textContent).toContain('暂无法判断')
  expect(host.textContent).toContain('已达成日均')
  expect(host.textContent).toContain('9%')
  expect(host.textContent).toContain('盘中暂不计算同时点倍数')
  await act(async () => root.unmount()); client.clear()
  root = createRoot(host)
  calls.turnover.mockResolvedValue({ ...live, stale: true })
  await render(); await click('成交与资金')
  expect(host.textContent).toContain('ETF 实时行情自 09:47 起未再更新')
})

it('guides to ETF daily sync when there is no turnover data', async () => {
  calls.turnover.mockResolvedValue({ ...turnover, as_of: null, funds: [], totals: null, conclusion: null })
  await render(); await click('成交与资金')
  expect(host.textContent).toContain('暂无 ETF 日 K 数据')
})

function fundOrder() {
  return [...host.querySelectorAll('tbody th[scope="row"] strong')].map(el => el.textContent)
}

it('orders by historical Huijin holdings, then re-sorts and keeps unlisted holders distinct', async () => {
  await render(); await click('成交与资金')
  expect(fundOrder()).toEqual(['华泰柏瑞沪深300', '嘉实中证500', '华夏科创50'])
  expect(host.textContent).toContain('735.1')
  expect(host.textContent).toContain('未列名')
  expect(host.textContent).toContain('1,923.2')
  expect(host.textContent).toContain('-0.86')
  expect(host.textContent).toContain('2025 年报')
  expect(host.textContent).toContain('12.3%')
  expect(host.textContent).toContain('均值 10.1%')
  expect(host.querySelectorAll('svg.etf-spark')).toHaveLength(2) // one point is not a trend
  await click('成交额')
  expect(fundOrder()).toEqual(['华夏科创50', '华泰柏瑞沪深300', '嘉实中证500'])
  await click('倍数')
  expect(fundOrder()).toEqual(['华泰柏瑞沪深300', '华夏科创50', '嘉实中证500'])
})

it('refreshes on demand', async () => {
  await render(); await click('成交与资金')
  expect(calls.turnover).toHaveBeenCalledTimes(1)
  await click('立即刷新')
  expect(calls.turnover).toHaveBeenCalledTimes(2)
})
