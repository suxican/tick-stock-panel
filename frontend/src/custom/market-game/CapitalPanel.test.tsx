// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MarketGameCapitalEnvelope, MarketGameCapitalObservation, MarketGameReport } from '@/lib/api'
import { CapitalPanel } from './CapitalPanel'
import { MarketGame } from './MarketGame'

const calls = vi.hoisted(() => ({ read: vi.fn(), refresh: vi.fn(), analyze: vi.fn(), reports: vi.fn(), report: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  marketGameCapital: calls.read, marketGameRefreshCapital: calls.refresh,
  marketGameAnalyze: calls.analyze, marketGameReports: calls.reports, marketGameReport: calls.report,
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('echarts-for-react', () => ({ default: ({ option }: { option: unknown }) => <div data-chart>{JSON.stringify(option)}</div> }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(mock => mock.mockReset())
  calls.read.mockImplementation(async (id: string) => envelope(id))
  calls.refresh.mockImplementation(async (id: string) => envelope(id, observation(id)))
  calls.reports.mockResolvedValue({ reports: [] })
  calls.analyze.mockResolvedValue(report())
  calls.report.mockImplementation(async (id: string) => report(id))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount()); client.clear(); host.remove(); vi.restoreAllMocks(); vi.useRealTimers()
})
async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => {
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1)
    else await new Promise(resolve => setTimeout(resolve, 0))
  })
}
async function render(id = 'plan-one', wholePage = false) {
  await act(async () => root.render(<QueryClientProvider client={client}>{wholePage ? <MarketGame /> : <CapitalPanel key={id} report={report(id)} />}</QueryClientProvider>)); await settle()
}
async function click(text: string) {
  const found = [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === text)
  expect(found, text).toBeDefined(); await act(async () => found!.click()); await settle()
}
function report(id = 'plan-one'): MarketGameReport {
  return { id, as_of: '2026-09-30', cutoff: '2026-09-30T15:30:00+08:00', created_at: '2026-09-30T16:00:00+08:00', quality: 'limited', research_only: false,
    summary: '冻结计划', input_version: 'snapshot', rule_version: '1.1', market_state: { trend: '上行', phase: '启动', emotion: '改善', crowding: '适中', change: '待观察' },
    allocation: { min: 0, max: .2, total_cap: .2, single_cap: .1, sector_cap: .2, risk_per_trade: .005, reason: '等待条件' }, hypotheses: [], scenarios: [], sectors: [], candidates: [], evidence: [], limitations: [],
  }
}
function envelope(id = 'plan-one', latest: MarketGameCapitalObservation | null = null): MarketGameCapitalEnvelope {
  return { report_id: id, latest, observations: latest ? [{ id: latest.id, created_at: latest.created_at, observed_at: latest.behavior.observed_at, data_date: latest.behavior.data_date, summary: latest.summary, changes: latest.changes }] : [], refresh_allowed: true, auto_refresh_allowed: true, refresh_reason: '可刷新当前观察', history_limit: 60 }
}
function observation(id = 'plan-one'): MarketGameCapitalObservation {
  return { id: `observation-${id}`, report_id: id, version: '1', created_at: '2026-10-09T10:00:00+08:00', background_date: '2026-09-30', background_usable: true, background_reason: '上一交易日冻结背景',
    behavior: { input_version: '1', status: 'limited', reason: '样本有限', data_date: '2026-10-09', observed_at: '2026-10-09T09:59:00+08:00',
      rows: [{ symbol: '600001.SH', name: '样本公司', sector: '机械', is_candidate: true, status: 'limited', reason: '等待足够历史', as_of: '2026-10-09T09:59:00+08:00', windows: [{ minutes: 5, return: .012, volume_ratio: 1.2 }, { minutes: 15, return: -.003, volume_ratio: .8 }, { minutes: 30, return: null, volume_ratio: null }], participation: { score: 0, raw_value: 1.2, sample_size: 20 }, support: { score: 70, raw_value: .7, sample_size: 22 }, distribution: { score: null, raw_value: .01, sample_size: 5 }, reference_price: 10.12, price_bias: .005, scenario: { id: 'panic_absorption', label: '恐慌中的承接增强', evidence: ['低点未继续下移'], alternative: '也可能是短时买盘', confirmation: '参考价上方持续承接', invalidation: '重新跌破低点' } }],
      sectors: [{ name: '机械', sample_size: 5, valid_count: 3, coverage: .6, breadth: .6667, participation: 50, support: 70, distribution: null, scenario: '样本承接改善' }],
      series: [{ time: '09:55', sample_size: 1, participation: 0, support: 50, distribution: null }, { time: '10:00', sample_size: 1, participation: 5, support: 70, distribution: null }], limitations: ['非全市场覆盖'],
    }, disclosures: { source: 'test/org', source_label: '龙虎榜已披露机构席位', requested_date: '2026-10-09', trade_date: '2026-09-30', fetched_at: '2026-10-09T10:00:00+08:00', available_at: null, point_in_time_verified: false, realtime: false, state: 'fallback_prev', status: '使用上一期机构榜单', rows: [{ symbol: '600001.SH', name: '样本公司', trade_date: '2026-09-30', range_days: 1, org_net_value: 123000000, org_net_rate: .03, org_buy_num: 2, org_sell_num: 1 }, { symbol: '600001.SH', name: '样本公司', trade_date: '2026-09-30', range_days: 3, org_net_value: -20000000, org_net_rate: -.01, org_buy_num: 1, org_sell_num: 3 }], omitted_count: 0, limitations: ['不代表全部持仓'] },
    plan_status: 'entry_window', plan_links: [{ symbol: '600001.SH', name: '样本公司', status: 'watch', original_max_position: .1, observation_cap: .1, reason: '维持原计划上限', unchecked_conditions: ['成交可行性未核验'] }], entry_authorized: false, summary: `观察 ${id}：修复待确认`, changes: ['首次记录，未重建此前事件'], limitations: ['不授权交易'],
  }
}

it('loads only archived observations and waits for explicit refresh', async () => {
  await render()
  expect(calls.read).toHaveBeenCalledWith('plan-one')
  expect(calls.refresh).not.toHaveBeenCalled()
  expect(host.textContent).toContain('尚未记录资金观察')
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false)
  await click('刷新并记录观察')
  expect(calls.refresh).toHaveBeenCalledWith('plan-one')
  expect(host.textContent).toContain('观察 plan-one：修复待确认')
  expect(host.textContent).toContain('首次记录，未重建此前事件')
})

it('renders units, missing scores, date fallback, independent disclosure ranges and unresolved conditions honestly', async () => {
  calls.read.mockResolvedValue(envelope('plan-one', observation()))
  await render()
  const stock = host.querySelector('section[aria-label="个股资金行为"]')!
  expect(stock.textContent).toContain('1.20%')
  expect(stock.textContent).toContain('-0.30%')
  expect(stock.textContent).not.toContain('120.00%')
  expect(stock.querySelector('[aria-label="参与强度 0.0 分，历史样本 20"]')).not.toBeNull()
  expect(stock.querySelector('[aria-label="兑现压力 无法评分，历史样本 5"]')).not.toBeNull()
  expect(stock.textContent).toContain('原计划单股上限 10.00%')
  expect(stock.textContent).toContain('成交可行性未核验')
  expect(stock.textContent).toContain('不授权入场或增加仓位')
  expect(host.textContent).toContain('记录时行为证据')
  expect(host.textContent).toContain('最晚分钟时点')
  expect(host.textContent).toContain('已保存观察，不代表当前状态')
  expect(host.textContent).toContain('量化身份 未验证')
  const disclosure = host.querySelector('section[aria-label="已披露机构交易"]')!
  expect(disclosure.textContent).toContain('实际榜单 2026-09-30')
  expect(disclosure.textContent).toContain('历史可得时间 未知')
  expect(disclosure.textContent).toContain('1.23 亿')
  expect(disclosure.textContent).toContain('-2000.00 万')
  expect(disclosure.querySelectorAll('tbody tr')).toHaveLength(2)
  expect(disclosure.textContent).toContain('单日榜')
  expect(disclosure.textContent).toContain('三日榜')
})

it('keeps frozen psychology separate from minute series and preserves null chart gaps', async () => {
  calls.read.mockResolvedValue(envelope('plan-one', observation()))
  await render()
  expect(host.textContent).toContain('原报告没有心理评分，不补算历史情绪')
  const option = JSON.parse(host.querySelector('[data-chart]')!.textContent!)
  expect(option.series).toHaveLength(3)
  expect(option.series[0].data).toEqual([0, 5])
  expect(option.series[2].data).toEqual([null, null])
  expect(option.series[2].connectNulls).toBe(false)
  expect(host.textContent).toContain('不代表当时已经触发信号')
})

it('keeps an earlier tightened cap visible while current conditions are unverified', async () => {
  const latest = observation()
  latest.plan_links[0] = { ...latest.plan_links[0], status: 'unverified', observation_cap: 0, retained_cap: .05 }
  calls.read.mockResolvedValue(envelope('plan-one', latest))
  await render()
  expect(host.textContent).toContain('本日已保留收紧上限 5.00%，当前仍待核验')
  expect(host.textContent).toContain('观察约束上限 0.00%')
})

it('shows missing minute and disclosure capabilities without manufacturing zero scores', async () => {
  const latest = observation()
  latest.behavior = { ...latest.behavior, status: 'unavailable', reason: '分钟数据源未提供能力', rows: [], sectors: [], series: [] }
  latest.disclosures = { ...latest.disclosures, state: 'source_unavailable', status: '数据源不支持机构榜单', rows: [] }
  calls.read.mockResolvedValue({ ...envelope('plan-one', latest), auto_refresh_allowed: false, refresh_reason: '自动观察不可用' })
  await render()
  expect(host.textContent).toContain('当前没有可核验的分钟样本')
  expect(host.textContent).toContain('暂无可核验的机构披露记录')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/settings?tab=data-sources')
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.disabled).toBe(true)
  expect(host.querySelector('[data-chart]')).toBeNull()
})

it('retries archive load failures and retains a valid snapshot after refresh failure', async () => {
  calls.read.mockRejectedValueOnce(new Error('读取权限不足'))
  await render()
  expect(host.textContent).toContain('观察读取失败：读取权限不足')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '刷新并记录观察')!.disabled).toBe(true)
  calls.read.mockResolvedValue(envelope('plan-one', observation()))
  await click('重试读取观察')
  calls.refresh.mockRejectedValueOnce(new Error('没有分钟能力权限'))
  await click('刷新并记录观察')
  expect(host.textContent).toContain('观察 plan-one：修复待确认')
  expect(host.textContent).toContain('没有分钟能力权限')
  expect(host.textContent).toContain('保留上一份有效观察')
})

it('isolates a late refresh result to its original report and resets the auto-observation switch', async () => {
  calls.read.mockImplementation(async (id: string) => envelope(id, observation(id)))
  let resolve!: (value: MarketGameCapitalEnvelope) => void
  calls.refresh.mockReturnValueOnce(new Promise(done => { resolve = done }))
  await render()
  await click('刷新并记录观察')
  await render('plan-two')
  await act(async () => resolve(envelope('plan-one', observation())))
  await settle()
  expect(host.textContent).toContain('观察 plan-two：修复待确认')
  expect(host.textContent).not.toContain('观察 plan-one：修复待确认')
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false)
})

it('polls only after explicit opt-in, skips hidden pages, and stops when the server closes the observation window', async () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  await render()
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(calls.refresh).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click())
  visibility.mockReturnValue('hidden')
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(calls.refresh).not.toHaveBeenCalled()
  visibility.mockReturnValue('visible')
  calls.refresh.mockResolvedValueOnce({ ...envelope('plan-one', observation()), auto_refresh_allowed: false, refresh_reason: '观察窗口已结束' })
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) }); await settle()
  expect(calls.refresh).toHaveBeenCalledTimes(1)
  expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false)
  expect(host.textContent).toContain('观察窗口已结束')
  await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
  expect(calls.refresh).toHaveBeenCalledTimes(1)
})

it('mounts the new tab on demand and stops its timer when leaving the tab', async () => {
  vi.useFakeTimers()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  await render('plan-one', true)
  await click('生成盘后计划')
  expect(calls.read).not.toHaveBeenCalled()
  await click('资金行为与情绪博弈')
  expect(calls.read).toHaveBeenCalledWith('plan-one')
  await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click())
  await click('博弈总览')
  await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
  expect(calls.refresh).not.toHaveBeenCalled()
})
