// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardComparison, FirstBoardConfig, FirstBoardResearch, FirstBoardSnapshot, FirstBoardSummary } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { FirstBoard } from './FirstBoard'
import extension from './extension'
import { validateDraft } from './RulesPanel'
import { beijingDay } from './shared'

const calls = vi.hoisted(() => ({
  firstBoardConfig: vi.fn(), firstBoardSaveConfig: vi.fn(), firstBoardSnapshot: vi.fn(), firstBoardRefresh: vi.fn(),
  firstBoardEvents: vi.fn(), firstBoardVersions: vi.fn(), firstBoardResearch: vi.fn(), firstBoardCompare: vi.fn(),
  firstBoardResearchRuns: vi.fn(), firstBoardPaperOrder: vi.fn(), paperAccounts: vi.fn(),
}))
const capabilityState = vi.hoisted(() => ({ available: true, failed: false, ready: true }))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilityMatrix: () => ({ data: capabilityState.ready ? { capabilities: [
  { id: 'daily', label: '日线', usable: true }, { id: 'realtime', label: '实时报价', usable: capabilityState.available },
  { id: 'minute', label: '分钟数据', usable: false },
] } : undefined, isError: capabilityState.failed }) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, onClose }: { symbol: string | null; onClose: () => void }) => symbol ? <div role="dialog" data-symbol={symbol}><button onClick={onClose}>关闭股票</button></div> : null }))

function config(): FirstBoardConfig {
  return { schema_version: 1, revision: 1, updated_at: '2026-09-30T09:30:00+08:00', enabled: true, notify: true, require_sector_confirmation: true,
    allowed_market_states: ['strong', 'lean_strong', 'range'], max_quote_age_seconds: 30, paper_account_id: 'test', max_positions: 4,
    max_stock_weight: .25, max_sector_weight: .5, total_exposure: .5, exit_time: '10:30', exit_loss_pct: .03,
    rules: { universe: 'main_board_non_st', lookback_days: 10, enabled_patterns: ['platform', 'trend', 'oversold'], min_history_days: 60,
      platform_window: 20, platform_max_range: .18, platform_near_high: .08, trend_window: 20, trend_min_return: .03,
      trend_max_return: .4, oversold_window: 60, oversold_min_drawdown: .25, approaching_distance: .03, min_change_pct: .05,
      min_turnover_rate: 2, max_turnover_rate: 30, min_amount: 50_000_000 } }
}
function snapshot(): FirstBoardSnapshot {
  return { status: 'ready', message: '当前报价可用', as_of: beijingDay(), observed_at: '2026-09-30T10:00:00+08:00', revision: 1,
    rows: [{ symbol: '600000.SH', name: '测试股票', pattern: 'platform', pattern_label: '平台突破', reference_price: 10,
      breakout_price: 10.5, state: 'approaching', price: 10.8, change_pct: .08, distance_to_limit_pct: .01818, turnover_rate: 4,
      reasons: ['平台振幅满足条件'], evidence: { reference_date: '2026-09-29', breakout_price_raw: 10.5 }, can_buy: true,
      blocked_reasons: [], quote_time: '2026-09-30T10:00:00+08:00', sector: '测试行业', suggested_amount: 25000, event_id: 'event1' }],
    environment: { label: '强势', allowed: true }, coverage: { candidate_count: 1, symbol_count: 1, quote_count: 1, fresh_count: 1 },
    limitations: ['没有逐笔队列数据'], paper: { initialized: true, stats: { rounds: 0, win_rate: null, realized_pnl: 0 }, note: '统计覆盖整个关联模拟账户' } }
}
const summary: FirstBoardSummary = { candidates: 10, unique_stock_days: 8, touched: 3, touch_observed: 10, touch_rate: .3,
  sealed: 2, seal_observed: 10, seal_rate: .2, next_open_count: 9, next_open_mean: .012, next_close_count: 9, next_close_mean: -.01, next_positive_rate: .4 }
function study(): FirstBoardResearch {
  return { schema_version: 1, mode: 'daily_observation', methodology: '收盘价格变化，不是假设成交收益。', rules: config().rules,
    data_window: {}, coverage: { sessions: ['2026-09-28'], session_count: 1, candidate_sessions: 1, missing_days: [], missing_outcomes: 1,
      missing_observations: 0, history_gap_samples: 0, history_sessions_before_start: 60, history_sufficient: true },
    summary, by_pattern: { platform: summary }, samples: [], total_samples: 10, samples_truncated: false, limitations: ['无队列撮合'] }
}
function comparison(): FirstBoardComparison {
  return { schema_version: 1, mode: 'daily_observation', methodology: '', experiment_id: 'experiment1', data_window: {},
    train: { sessions: ['2026-09-01'], session_count: 1 }, validation: { sessions: ['2026-09-02'], session_count: 1 },
    variants: [{ id: 'baseline', label: '当前基线', rules: config().rules, train: summary, validation: summary, train_coverage: {}, validation_coverage: {} }],
    training_selected_id: null, recommendation: null, recommendation_reason: '样本不足', auto_applied: false, minimum_samples: { train: 30, validation: 10 }, limitations: [] }
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.assign(capabilityState, { available: true, failed: false, ready: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.firstBoardConfig.mockResolvedValue(config())
  calls.firstBoardSnapshot.mockResolvedValue(snapshot())
  calls.firstBoardRefresh.mockResolvedValue(snapshot())
  calls.firstBoardVersions.mockResolvedValue({ versions: [config()] })
  calls.firstBoardEvents.mockResolvedValue({ events: [], total: 0, day: beijingDay() })
  calls.firstBoardResearchRuns.mockResolvedValue({ runs: [] })
  calls.firstBoardResearch.mockResolvedValue(study())
  calls.firstBoardCompare.mockResolvedValue(comparison())
  calls.paperAccounts.mockResolvedValue({ accounts: [{ id: 'test', name: '测试账户', status: 'active' }] })
  calls.firstBoardSaveConfig.mockImplementation(async value => ({ ...value, revision: 2 }))
  calls.firstBoardPaperOrder.mockResolvedValue({ order: { status: 'pending' } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 6; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(tab = 'live') {
  await act(async () => root.render(<MemoryRouter initialEntries={[`/first-board?tab=${tab}`]}><QueryClientProvider client={client}><FirstBoard /></QueryClientProvider></MemoryRouter>))
  await settle()
}
function button(text: string) { return [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === text)! }
async function click(text: string) { expect(button(text)).toBeDefined(); await act(async () => button(text).click()); await settle() }
async function numberInput(label: string, value: string) {
  const input = [...host.querySelectorAll('label')].find(item => item.textContent?.includes(label))!.querySelector('input')!
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })) })
  await settle()
}

it('registers one isolated route and 首板模式 menu', () => {
  expect(extension.routes?.map(route => route.path)).toEqual(['/first-board'])
  expect(extension.navigation?.[0].label).toBe('首板模式')
})
it('shows decimal percentages correctly, permits quote-based alerts without minute capability and reuses stock preview', async () => {
  await render()
  expect(host.textContent).toContain('8.00%')
  expect(host.textContent).toContain('4.00%')
  expect(button('模拟买入').disabled).toBe(false)
  const stockButton = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.startsWith('测试股票'))!
  await act(async () => stockButton.click())
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('600000.SH')
  await click('模拟买入')
  expect(calls.firstBoardPaperOrder).toHaveBeenCalledWith({ event_id: 'event1', side: 'buy', amount: 25000 }, expect.anything())
  expect(host.textContent).toContain('模拟委托已提交')
})
it('fails closed on stale snapshot despite an actionable cached row', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'stale' })
  await render()
  expect(button('模拟买入').disabled).toBe(true)
  expect(host.textContent).toContain('数据已过期')
})
it.each(['missing', 'failed', 'loading'])('disables simulated orders when capability matrix is %s', async state => {
  Object.assign(capabilityState, { available: state !== 'missing', failed: state === 'failed', ready: state !== 'loading' })
  await render()
  expect(button('模拟买入').disabled).toBe(true)
  if (state === 'missing') expect(host.textContent).toContain('当前数据源缺少可用的实时报价')
  if (state === 'failed') expect(host.textContent).toContain('行情能力状态读取失败')
})
it('retains last valid rows but disables orders when refresh fails', async () => {
  await render()
  calls.firstBoardSnapshot.mockRejectedValue(new Error('报价读取失败'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.firstBoardSnapshot }) })
  await settle()
  expect(host.textContent).toContain('测试股票')
  expect(host.textContent).toContain('报价读取失败')
  expect(button('模拟买入').disabled).toBe(true)
})
it('forces a server refresh and preserves null account win rate', async () => {
  await render()
  await click('刷新')
  expect(calls.firstBoardRefresh).toHaveBeenCalledTimes(1)
  const winRate = [...host.querySelectorAll('dt')].find(item => item.textContent === '账户胜率')!
  expect(winRate.nextElementSibling?.textContent).toBe('—')
  expect(host.textContent).toContain('统计覆盖整个关联模拟账户')
})
it('explains disabled and empty monitor states', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), enabled: false })
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [], status: 'disabled' })
  await render()
  expect(host.textContent).toContain('监控未开启')
  expect(host.textContent).toContain('暂无匹配的首板候选')
  await click('前往规则版本开启盯盘')
  expect(host.textContent).toContain('保存为新版本')
})
it('converts displayed percentages and 万元 to exact backend units before saving', async () => {
  await render('rules')
  await numberInput('盘中最低涨幅', '6')
  await numberInput('最低成交额', '6000')
  await click('保存为新版本')
  const saved = calls.firstBoardSaveConfig.mock.calls[0][0]
  expect(saved.rules.min_change_pct).toBe(.06)
  expect(saved.rules.min_amount).toBe(60_000_000)
  expect(saved.rules.min_turnover_rate).toBe(2)
  expect(saved.revision).toBe(1)
})
it('preserves a rejected draft and offers reload after optimistic-concurrency conflict', async () => {
  await render('rules')
  await numberInput('最近无收盘涨停', '12')
  calls.firstBoardSaveConfig.mockRejectedValue(new Error('版本已变化，请重新载入'))
  calls.firstBoardConfig.mockResolvedValue({ ...config(), revision: 2 })
  await click('保存为新版本')
  expect(host.textContent).toContain('草稿基于版本 1')
  expect(host.textContent).toContain('版本已变化，请重新载入')
  expect(button('保存为新版本').disabled).toBe(true)
  const lookback = [...host.querySelectorAll('label')].find(item => item.textContent?.includes('最近无收盘涨停'))!.querySelector('input')!
  expect(lookback.value).toBe('12')
  await click('重新载入当前版')
  expect(lookback.value).toBe('10')
})
it('renders historical events by selected date without turning them into live buys', async () => {
  calls.firstBoardEvents.mockResolvedValue({ events: [{ id: 'old1', ts: 1790720400000, date: '2026-09-30', symbol: '600000.SH', name: '历史股票',
    pattern: 'platform', pattern_label: '平台突破', state: 'approaching', event_type: 'buy_candidate', message: '历史候选信号', price: 10.8,
    revision: 1, evidence: {}, blocked_reasons: [], quote_time: '2026-09-30T09:00:00+08:00' }], total: 1, day: beijingDay() })
  await render('events')
  expect(calls.firstBoardEvents).toHaveBeenCalledWith(beijingDay())
  expect(host.textContent).toContain('历史候选信号')
  expect(button('模拟买入')).toBeUndefined()
})
it('shows research observations and denominators without claiming execution win rate', async () => {
  await render('research')
  await click('运行日线研究')
  expect(host.textContent).toContain('不能作为交易胜率')
  expect(host.textContent).toContain('触板观察率')
  expect(host.textContent).toContain('30.00%')
  expect(host.textContent).toContain('3 / 10')
  expect(host.textContent).toContain('1.20%')
  expect(calls.firstBoardResearch.mock.calls[0][0].rules.lookback_days).toBe(10)
})
it('loads comparison parameters for review and never auto-saves the selected experiment', async () => {
  await render('research')
  await click('比较实验参数')
  expect(host.textContent).toContain('样本不足')
  expect(host.textContent).toContain('规则尚未自动应用')
  await click('载入草稿审阅')
  expect(host.textContent).toContain('请审阅参数后保存为新版本')
  expect(calls.firstBoardSaveConfig).not.toHaveBeenCalled()
})
it('validates meaningful cross-field bounds without imposing an extra sector cap', () => {
  expect(validateDraft({ ...config(), rules: { ...config().rules, min_turnover_rate: 31 } })).toContain('最低换手率')
  expect(validateDraft({ ...config(), max_stock_weight: .6 })).toContain('单股仓位')
  expect(validateDraft({ ...config(), max_sector_weight: .8 })).toBeNull()
})
