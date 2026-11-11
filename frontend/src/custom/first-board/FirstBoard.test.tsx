// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardAttention, FirstBoardCandidate, FirstBoardComparison, FirstBoardConfig, FirstBoardEvent, FirstBoardEventFilters, FirstBoardFeedback, FirstBoardResearch, FirstBoardSnapshot, FirstBoardSummary } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { FirstBoard } from './FirstBoard'
import extension from './extension'
import { validateDraft } from './RulesPanel'
import { beijingDay } from './shared'

const calls = vi.hoisted(() => ({
  firstBoardConfig: vi.fn(), firstBoardSaveConfig: vi.fn(), firstBoardSnapshot: vi.fn(), firstBoardRefresh: vi.fn(),
  firstBoardEvents: vi.fn(), firstBoardFirstAlertRankings: vi.fn(), firstBoardVersions: vi.fn(), firstBoardResearch: vi.fn(), firstBoardCompare: vi.fn(),
  firstBoardResearchRuns: vi.fn(), firstBoardPaperOrder: vi.fn(), paperAccounts: vi.fn(),
  firstBoardTradeValidation: vi.fn(), firstBoardTradeValidationRuns: vi.fn(),
}))
const capabilityState = vi.hoisted(() => ({ available: true, failed: false, ready: true }))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilityMatrix: () => ({ data: capabilityState.ready ? { capabilities: [
  { id: 'daily', label: '日线', usable: true }, { id: 'realtime', label: '实时报价', usable: capabilityState.available },
  { id: 'minute', label: '分钟数据', usable: false },
] } : undefined, isError: capabilityState.failed }) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, onClose, navList }: { symbol: string | null; onClose: () => void; navList?: { symbol: string; name: string }[] }) => symbol ? <div role="dialog" data-symbol={symbol} data-nav-list={JSON.stringify(navList)}><button onClick={onClose}>关闭股票</button></div> : null }))

function config(): FirstBoardConfig {
  return { schema_version: 1, revision: 1, updated_at: '2026-09-30T09:30:00+08:00', enabled: true, notify: true, require_sector_confirmation: true,
    allowed_market_states: ['strong', 'lean_strong', 'range'], max_quote_age_seconds: 30, paper_account_id: 'test', max_positions: 4,
    max_stock_weight: .25, max_sector_weight: .5, total_exposure: .5, exit_time: '10:30', exit_loss_pct: .03,
    exit_profit_pct: null, exit_hold_limit_up: false, feedback_gate: false, feedback_min_mean_change: 0, feedback_min_up_ratio: .5,
    rules: { universe: 'main_board_non_st', lookback_days: 10, enabled_patterns: ['platform', 'trend', 'oversold'], min_history_days: 60,
      platform_window: 20, platform_max_range: .18, platform_near_high: .08, trend_window: 20, trend_min_return: .03,
      trend_max_return: .4, oversold_window: 60, oversold_min_drawdown: .25, approaching_distance: .03, approaching_distance_20cm: .03, min_change_pct: .05, min_change_pct_20cm: .13,
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
function yesterdayFeedback(): FirstBoardFeedback {
  const summary = { sample_count: 2, quote_count: 2, coverage: 1, mean_change_pct: .07, median_change_pct: .07, up_count: 1, up_ratio: .5,
    limit_up_count: 1, limit_known_count: 2, limit_up_ratio: .5, loss_count: 1, loss_ratio: .5, mean_open_premium: .03, mean_open_change: .04, open_count: 2, small_sample: true }
  return { status: 'ready', message: '完整行情', as_of: beijingDay(), sample_date: '2026-09-29', observed_at: '2026-09-30T10:00:00+08:00', quote_mode: 'live',
    membership_note: '当前成分等权涨幅', history_excluded_count: 0, groups: { all: summary, main: summary, growth: summary }, sectors: [],
    rows: [
      { symbol: '600100.SH', name: '昨日下跌样本', board: 'main', price: 9.4, change_pct: -.06, open_premium: 0, open_change: -.06, at_limit: false, quote_time: '2026-09-30T10:00:00+08:00', status: 'valid', reason: null },
      { symbol: '300100.SZ', name: '昨日上涨样本', board: 'growth', price: 12, change_pct: .2, open_premium: .06, open_change: .132, at_limit: true, quote_time: '2026-09-30T10:00:00+08:00', status: 'valid', reason: null },
    ] }
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
  localStorage.removeItem('first-board-stock-marks')
  localStorage.removeItem('first-board-feedback-expanded')
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.assign(capabilityState, { available: true, failed: false, ready: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.firstBoardConfig.mockResolvedValue(config())
  calls.firstBoardSnapshot.mockResolvedValue(snapshot())
  calls.firstBoardRefresh.mockResolvedValue(snapshot())
  calls.firstBoardVersions.mockResolvedValue({ versions: [config()] })
  calls.firstBoardEvents.mockResolvedValue({ events: [], total: 0, day: beijingDay() })
  calls.firstBoardFirstAlertRankings.mockResolvedValue(null)
  calls.firstBoardResearchRuns.mockResolvedValue({ runs: [] })
  calls.firstBoardTradeValidationRuns.mockResolvedValue({ runs: [] })
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
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.restoreAllMocks(); localStorage.removeItem('first-board-stock-marks'); localStorage.removeItem('first-board-feedback-expanded') })
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
function candidate(name: string, values: Partial<FirstBoardCandidate> = {}): FirstBoardCandidate {
  return { ...snapshot().rows[0], name, symbol: `60000${name.charCodeAt(0) - 65}.SH`, ...values }
}
function candidateRows() { return [...host.querySelectorAll('table[aria-label="首板实时候选"] tbody tr[data-candidate-row]')] }
function evidenceRows() { return [...host.querySelectorAll('table[aria-label="首板实时候选"] tbody tr[data-evidence-row]')] }
function candidateNames() {
  return candidateRows().map(row => row.querySelector('td button')!.firstChild!.textContent)
}
/** 评分分项、日线证据和联动依据位于行展开区。 */
async function expandEvidence() {
  for (const control of [...host.querySelectorAll<HTMLButtonElement>('button[aria-label^="展开依据"]')]) await act(async () => control.click())
  await settle()
}
function stateButton(label: string) {
  return [...host.querySelectorAll<HTMLButtonElement>('[role="group"][aria-label="候选状态"] button')].find(item => item.textContent?.startsWith(label))!
}
async function filterState(label: string) {
  expect(stateButton(label)).toBeDefined()
  await act(async () => stateButton(label).click())
  await settle()
}

it('keeps data coverage on deduplicated symbols when pattern filters change and labels retained evidence', async () => {
  const ready = { status: 'ready', known: 1, total: 1, detail: '已核验一只股票' } as const
  const data: FirstBoardSnapshot = { ...snapshot(), rows: [candidate('A'), candidate('A', { pattern: 'trend' })],
    data_status: { version: 1, trade_date: beijingDay(), observed_at: '2026-09-30T10:00:00+08:00', checked_at: '2026-09-30T10:00:05+08:00',
      phase: 'live', monitor_enabled: true, realtime_status: 'available', realtime_reason: null, in_session: true, same_day: true,
      quote_max_age_seconds: 30, symbol_count: 1, sampling: { status: 'recorded', sample_count: 3, last_sample_at: '2026-09-30T09:55:00+08:00' },
      metrics: { quotes: ready, industry: ready, concept: ready, minute: { status: 'missing', known: 0, total: 1, detail: '缺少当日原价分钟' },
        popularity: ready, theme: ready, ma: ready, macd: ready, priority: ready } } }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const panel = host.querySelector('[aria-label="盘中数据状态摘要"]')!
  expect(panel.textContent).toContain('1 只候选股')
  expect(panel.textContent).toContain('原价分钟')
  expect(panel.textContent).toContain('0 / 1')
  const pattern = [...host.querySelectorAll('label')].find(el => el.firstChild?.textContent === '形态')!.querySelector('select')!
  await act(async () => { pattern.value = 'oversold'; pattern.dispatchEvent(new Event('change', { bubbles: true })) }); await settle()
  expect(candidateNames()).toHaveLength(0)
  expect(panel.textContent).toContain('1 只候选股')
  expect(panel.textContent).toContain('缺少当日原价分钟')
  expect(panel.querySelector('a[href="/settings?tab=monitoring&highlight=minute-refresh"]')).not.toBeNull()
  calls.firstBoardRefresh.mockRejectedValue(new Error('刷新失败'))
  await click('刷新')
  expect(panel.textContent).toContain('刷新失败，显示上次摘要')
  calls.firstBoardRefresh.mockResolvedValue(snapshot())
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, status: 'closed', data_status: {
    ...data.data_status, trade_date: '2026-09-28', same_day: false, phase: 'closed' } })
  await click('刷新')
  expect(panel.querySelector('header')?.textContent).toContain('2026-09-28 历史快照')
})

const technical: NonNullable<FirstBoardCandidate['evidence']['technical_shadow']> = {
  version: 'ma-macd-shadow-v1', status: 'ready', pattern: 'platform', reference_date: '2026-09-29',
  close: 10.12, prev_close: 10, ma5: 10.1, prev_ma5: 10, ma10: 10, ma20: 9.9, prev_ma20: 9.8,
  ma_support: true, macd_support: true, macd_dif: .02, macd_dea: .01, macd_hist: .02, prev_macd_hist: .018,
  ma_reason: '收盘在20日均线之上', macd_reason: 'DIF 高于 DEA',
}

async function expandTechnical(node: Element) {
  const details = [...node.querySelectorAll('details')].find(el => el.querySelector('summary')?.textContent?.includes('上个交易日日线'))!
  await act(async () => { details.open = true; details.dispatchEvent(new Event('toggle')) })
}

it('adds previous-session evidence without promoting supported or incomplete candidates', async () => {
  const data = { ...snapshot(), as_of: '2026-09-30', rows: [
    candidate('A', { change_pct: .05, evidence: { technical_shadow: technical }, can_buy: false }),
    candidate('B', { change_pct: .07, evidence: { technical_shadow: { ...technical, status: 'partial',
      ma_support: false, macd_support: null, macd_dif: null, macd_dea: null, macd_hist: null, prev_macd_hist: null,
      ma_reason: '均线方向不符', macd_reason: '连续日线不足' } } }),
    candidate('C', { change_pct: .06 }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  expect(candidateNames()).toEqual(['B', 'C', 'A'])
  await expandEvidence()
  const rows = evidenceRows()
  expect(rows[0].textContent).toContain('均线不符／MACD未知')
  await expandTechnical(rows[0])
  expect(rows[0].textContent).toContain('DIF —')
  expect(rows[0].textContent).toContain('连续日线不足')
  expect(rows[1].textContent).toContain('未记录前一交易日技术证据')
  expect(rows[2].textContent).toContain('上个交易日日线 · 均线符合／MACD符合')
  await expandTechnical(rows[2])
  expect(rows[2].textContent).toContain('参考日 2026-09-29')
  expect(rows[2].textContent).toContain('柱值 0.0200')
  // 未达到提示条件的行不显示买入按钮。
  expect([...candidateRows()[2].querySelectorAll('button')].find(el => el.textContent === '模拟买入')).toBeUndefined()
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, status: 'stale' })
  await click('刷新')
  expect(host.textContent).toContain('上个交易日日线（历史）')
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, rows: [
    candidate('A', { evidence: { technical_shadow: { ...technical, reference_date: data.as_of } } }),
    candidate('B', { evidence: { technical_shadow: { ...technical, version: 'other' } } }),
  ] })
  await click('刷新')
  expect(host.textContent).toContain('上个交易日日线 · 证据不足')
  expect(host.textContent).toContain('技术证据版本待核验')
  expect(host.textContent).not.toContain('均线符合／MACD符合')
})

it('ranks actionable scores first and shows only historical evidence after close', async () => {
  const base: NonNullable<FirstBoardCandidate['priority_score']> = {
    version: 'priority-v1', status: 'partial', score: 58, known_max: 85, can_buy: true,
    role: 'leader', reason: '等待分时证据',
    primary_sector: { id: 'solar', name: '光伏', kind: 'concept', source_id: 'local', field: '概念',
      peer_rank: 2, peer_total: 35, peer_valid_count: 18, peer_count: 20, peer_mean_change_pct: .03,
      peer_up_ratio: .7, peer_coverage: .9, peer_persistent: true, peer_reference_at: '2026-09-30T09:55:00+08:00' },
    parts: { popularity: { points: 6, max_points: 10, reason: '当时热榜' } },
    radar: { popularity: { status: 'ready', rank: 38, rank_gain: 5, source_at: '2026-09-30T09:59:00+08:00', fetched_at: '2026-09-30T10:00:00+08:00' },
      theme: { status: 'ready', source: 'official_core', name: '电池', rank: 3 },
      plate: { status: 'ready', source: 'vendor_plate', name: '光伏设备', plate_type: 14, rank: 1, members_at: '2026-09-30T09:56:00+08:00' } },
  }
  const data = { ...snapshot(), rows: [
    candidate('A', { priority_score: { ...base, status: 'watch', score: 72, can_buy: false }, can_buy: false }),
    candidate('B', { priority_score: base }),
    candidate('C', { priority_score: { ...base, status: 'ready', score: 78, known_max: 100 } }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  expect(candidateNames()).toEqual(['C', 'B', 'A'])
  expect(candidateRows()[1].querySelector('[data-priority-summary]')?.textContent).toContain('58 分')
  await expandEvidence()
  const cell = host.querySelectorAll('[data-priority-cell]')[1]
  expect(cell.textContent).toContain('58 分')
  expect(cell.textContent).toContain('证据覆盖 85 / 100')
  expect(cell.textContent).toContain('同行持续强势')
  await act(async () => { const detail = cell.querySelector('details')!; detail.open = true; detail.dispatchEvent(new Event('toggle')) })
  expect(cell.textContent).toContain('当时人气第 38 名')
  expect(cell.textContent).toContain('上升 5 位')
  expect(cell.textContent).toContain('持续强势参考时点')
  expect(cell.textContent).toContain('官方核心股 · 电池')
  expect(cell.textContent).toContain('供应商行业榜 · 光伏设备')
  const select = [...host.querySelectorAll('label')].find(el => el.textContent?.includes('评分筛选'))!.querySelector('select')!
  async function filter(value: string) {
    await act(async () => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })) })
    await settle()
  }
  await filter('incomplete')
  expect(candidateNames()).toEqual(['B', 'A'])
  calls.firstBoardSnapshot.mockResolvedValue({
    ...data, status: 'closed', rows: [candidate('B', { priority_score: base, last_priority: {
      as_of: '2026-09-30T10:00:00+08:00', version: 'priority-v1', status: 'partial', score: 58, known_max: 85,
    } })],
  })
  await click('刷新')
  expect(candidateNames()).toEqual([])
  await filter('all')
  expect(host.querySelector('[data-priority-cell]')?.textContent).toContain('最近盘中评分')
  expect(host.querySelector('[data-priority-cell] strong')).toBeNull()
  expect(button('模拟买入').disabled).toBe(true)
  calls.firstBoardSnapshot.mockResolvedValue({
    ...data, status: 'closed', rows: [candidate('B', { priority_score: base, last_priority: {
      as_of: '2026-09-30T09:56:00+08:00', version: 'priority-v1', status: 'partial', score: 53, known_max: 70, source: 'event',
    } })],
  })
  await click('刷新')
  expect(host.querySelector('[data-priority-cell]')?.textContent).toContain('最近信号评分')
  expect(host.querySelector('[data-priority-cell] strong')).toBeNull()
})
it('ranks priority-v2 scores like earlier versions and leaves unknown versions unscored', async () => {
  const score: NonNullable<FirstBoardCandidate['priority_score']> = {
    version: 'priority-v2', status: 'ready', score: 76, known_max: 100, can_buy: true, role: 'unknown', reason: '当时行情', primary_sector: null,
    parts: { stock: { points: 30, max_points: 40, reason: '距涨停 1.00%（10/15），量比 3.00（15/15），首次临板为开盘后 15 分钟（5/10）' } },
  }
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [
    candidate('A', { priority_score: { ...score, version: 'priority-v9', score: 99 } }), candidate('B', { priority_score: score }),
  ] })
  await render()
  expect(candidateNames()).toEqual(['B', 'A'])
  expect(candidateRows()[1].textContent).toContain('暂无评分')
  await expandEvidence()
  const cell = evidenceRows()[0].querySelector('[data-priority-cell]')!
  expect(cell.textContent).toContain('76 分')
  await act(async () => { const detail = cell.querySelector('details')!; detail.open = true; detail.dispatchEvent(new Event('toggle')) })
  expect(cell.textContent).toContain('个股量价　30 / 40')
  expect(cell.textContent).toContain('量比 3.00（15/15）')
})
async function toggleFocus(name: string, symbol: string, focused = false) {
  const control = host.querySelector<HTMLButtonElement>(`button[aria-label="${focused ? '取消关注' : '特别关注'} ${name} ${symbol}"]`)
  expect(control).not.toBeNull()
  await act(async () => control!.click())
  await settle()
}

it('pins every pattern of a focused stock while retaining sorting, filters and stock navigation', async () => {
  const data = { ...snapshot(), rows: [
    candidate('A', { change_pct: .1 }), candidate('B', { change_pct: .06 }),
    candidate('B', { pattern: 'trend', change_pct: .06 }), candidate('C', { change_pct: .08 }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  expect(candidateNames()).toEqual(['A', 'C', 'B', 'B'])
  await toggleFocus('B', '600001.SH')
  expect(candidateNames()).toEqual(['B', 'B', 'A', 'C'])
  expect(host.querySelectorAll('button[aria-label="取消关注 B 600001.SH"][aria-pressed="true"]')).toHaveLength(2)
  await sortBy('涨幅')
  await sortBy('涨幅')
  expect(candidateNames()).toEqual(['B', 'B', 'C', 'A'])
  await toggleFocus('A', '600000.SH')
  expect(candidateNames()).toEqual(['B', 'B', 'A', 'C'])
  await act(async () => host.querySelector<HTMLButtonElement>('table[aria-label="首板实时候选"] tbody td button')!.click())
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav-list')!).map((row: { name: string }) => row.name)).toEqual(['B', 'A', 'C'])
  await filterPattern('trend')
  expect(candidateNames()).toEqual(['B'])
  await filterPattern('all')
  await toggleFocus('B', '600001.SH', true)
  expect(candidateNames()).toEqual(['A', 'B', 'B', 'C'])
  await toggleFocus('A', '600000.SH', true)
  expect(candidateNames()).toEqual(['B', 'B', 'C', 'A'])
  expect(client.getQueryData<FirstBoardSnapshot>(QK.firstBoardSnapshot)?.rows).toEqual(data.rows)
  expect(calls.firstBoardPaperOrder).not.toHaveBeenCalled()
})

it('restores focus across remounts and refreshed quotes using its own persisted stock marks', async () => {
  const data = { ...snapshot(), rows: [candidate('A', { change_pct: .1 }), candidate('B', { change_pct: .06 })] }
  localStorage.setItem('huichun-stock-marks', JSON.stringify({ '600000.SH': { focused: true, pinned: true } }))
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  await toggleFocus('B', '600001.SH')
  expect(JSON.parse(localStorage.getItem('first-board-stock-marks')!)['600001.SH'].focused).toBe(true)
  await act(async () => root.unmount())
  root = createRoot(host)
  await render()
  expect(candidateNames()).toEqual(['B', 'A'])
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, rows: [{ ...data.rows[0], change_pct: .2 }, data.rows[1]] })
  await click('刷新')
  expect(candidateNames()).toEqual(['B', 'A'])
  await toggleFocus('B', '600001.SH', true)
  expect(JSON.parse(localStorage.getItem('first-board-stock-marks')!)).toEqual({})
  expect(JSON.parse(localStorage.getItem('huichun-stock-marks')!)).toEqual({ '600000.SH': { focused: true, pinned: true } })
  localStorage.removeItem('huichun-stock-marks')
})

it.each(['{', 'null', '[]', '{"600001.SH":{"focused":"yes","pinned":1}}'])('ignores invalid saved focus: %s', async raw => {
  localStorage.setItem('first-board-stock-marks', raw)
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { change_pct: .1 }), candidate('B', { change_pct: .06 })] })
  await render()
  expect(candidateNames()).toEqual(['A', 'B'])
  await toggleFocus('B', '600001.SH')
  expect(candidateNames()).toEqual(['B', 'A'])
})

it('keeps focus usable and offers retry when persistence fails', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { change_pct: .1 }), candidate('B', { change_pct: .06 })] })
  await render()
  const failure = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage unavailable') })
  await toggleFocus('B', '600001.SH')
  expect(candidateNames()).toEqual(['B', 'A'])
  expect(host.textContent).toContain('标记未能保存')
  failure.mockRestore()
  await click('重试')
  expect(host.textContent).not.toContain('标记未能保存')
  expect(JSON.parse(localStorage.getItem('first-board-stock-marks')!)['600001.SH'].focused).toBe(true)
})
function sortButton(label: string) { return host.querySelector<HTMLButtonElement>(`button[aria-label="按${label}排序"]`)! }
async function sortBy(label: string) {
  expect(sortButton(label)).not.toBeNull()
  await act(async () => sortButton(label).click())
  await settle()
}
async function filterPattern(value: string) {
  const select = host.querySelector<HTMLSelectElement>('select')!
  await act(async () => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
}

function attention(values: Partial<FirstBoardAttention> = {}): FirstBoardAttention {
  return { schema_version: 1, rule_version: 'attention-v1', as_of: '2026-09-30T10:00:00+08:00', membership_version: 'members-v1',
    status: 'ready', strong_sector: true, leader_follow: true, intraday_rising: true, priority: 2, group: 'S1F1',
    groups: [{ id: 'concept:test', kind: 'concept', name: '测试概念', source_id: 'test-provider', field: 'concept', rank: 2, total: 20,
      member_count: 10, valid_count: 9, coverage: .9, mean_change_pct: .035, market_mean_change_pct: .012, up_ratio: .8, strong: true, follow: true, reason: '板块广度与强度达到实验门槛',
      leader: { symbol: '600100.SH', name: '测试领涨股', observed_limit_at: '2026-09-30T09:40:00+08:00', at_limit: true } }],
    intraday: { as_of: '2026-09-30T09:59:00+08:00', return_5m: .01, return_15m: .03, max_drawdown: .005, above_vwap: true, reason: '完整分钟窗口内持续走强' },
    reasons: [], ...values }
}
async function filterAttention(value: string) {
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="重点关注"]')
  expect(select).not.toBeNull()
  await act(async () => { select!.value = value; select!.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
}

it('filters every confirmed sector membership by source and excludes historical or official-only matches', async () => {
  const local = attention().groups[0]
  const boards = [
    { ...local, id: 'other', name: '主评分板块', peer_strong: false },
    { ...local, id: 'battery', name: '电池', peer_strong: true },
    { ...local, id: 'solar', name: '光伏设备', peer_strong: true, kind: 'industry' as const },
  ]
  const base: NonNullable<FirstBoardCandidate['priority_score']> = {
    status: 'watch', version: 'priority-v1', score: 53, known_max: 70, parts: {}, primary_sector: null,
    role: 'unknown', can_buy: false, reason: '等待临板',
  }
  const data = { ...snapshot(), rows: [
    candidate('A', { attention_context: attention({ groups: boards }), priority_score: base }),
    candidate('B', { priority_score: { ...base, radar: { plates: [
      { status: 'ready', source: 'vendor_plate', rank: 1, plate_type: 17, tid: 'B1', name: '电池' },
      { status: 'ready', source: 'vendor_plate', rank: 1, plate_type: 14, tid: 'B2', name: '光伏设备' },
    ] } } }),
    candidate('C', { attention_context: attention({ groups: boards, status: 'stale' }),
      priority_score: { ...base, radar: { theme: { status: 'ready', source: 'official_core', rank: 1, name: '电池' } } } }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  async function select(label: string, value: string) {
    const input = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!
    await act(async () => { input.value = value; input.dispatchEvent(new Event('change', { bubbles: true })) })
    await settle()
  }
  await select('评分筛选', 'concept')
  expect(candidateNames()).toEqual(['A', 'B'])
  await select('具体强势板块', 'battery')
  expect(candidateNames()).toEqual(['A'])
  await select('具体强势板块', 'zzshare:17:B1')
  expect(candidateNames()).toEqual(['B'])
  await select('评分筛选', 'industry')
  expect(candidateNames()).toEqual(['A', 'B'])
  await select('具体强势板块', 'zzshare:14:B2')
  expect(candidateNames()).toEqual(['B'])
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, status: 'stale' })
  await click('刷新')
  expect(candidateNames()).toEqual([])
})

it('shows attention evidence with explicit decimal units without granting a simulated buy', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { can_buy: false, attention_context: attention() })] })
  await render()
  expect(host.querySelector('[data-attention-summary]')?.textContent).toContain('同板块双条件')
  await expandEvidence()
  const cell = host.querySelector('[data-attention-cell]')!
  expect(cell.textContent).toContain('同板块双条件')
  expect(cell.textContent).toContain('概念 · 测试概念')
  expect(cell.textContent).toContain('测试领涨股 · 触板')
  expect(cell.textContent).toContain('分时走强')
  expect(cell.textContent).not.toContain('members-v1')
  await act(async () => { const detail = cell.querySelector('details')!; detail.open = true; detail.dispatchEvent(new Event('toggle')) })
  expect(cell.textContent).toContain('排名 2 / 20')
  expect(cell.textContent).toContain('覆盖 9 / 10（90.00%）')
  expect(cell.textContent).toContain('平均涨幅 3.50%')
  expect(cell.textContent).toContain('上涨占比 80.00%')
  expect(cell.textContent).toContain('同来源分类均值 1.20%')
  expect(cell.textContent).toContain('近 5 分钟 1.00%')
  expect(cell.textContent).toContain('5分钟收盘价回撤 0.50%')
  expect(cell.textContent).toContain('test-provider')
  expect(cell.textContent).toContain('members-v1')
  expect(host.textContent).not.toContain('板块联动与分时标签仅作关注依据')
  expect(host.textContent).not.toContain('首板优先分越高，盘中复核顺序越靠前')
  expect(button('模拟买入')).toBeUndefined()
  expect(calls.firstBoardPaperOrder).not.toHaveBeenCalled()
})

it('shows an opened leader and partial minute evidence without inventing follow-through', async () => {
  const group = attention().groups[0]
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention({
    status: 'partial', leader_follow: null, intraday_rising: null, group: null, priority: 1,
    groups: [{ ...group, follow: null, leader: { ...group.leader!, at_limit: false } }],
    intraday: { as_of: null, return_5m: null, return_15m: null, max_drawdown: null, above_vwap: null, reason: '缺少完整分钟窗口' },
  }) })] })
  await render()
  await expandEvidence()
  const cell = host.querySelector('[data-attention-cell]')!
  expect(cell.textContent).toContain('测试领涨股 · 已开板')
  expect(cell.textContent).toContain('分时数据不足')
  expect(cell.textContent).toContain('部分数据待确认')
  expect(cell.textContent).not.toContain('同板块双条件')
  await filterAttention('strong')
  expect(candidateNames()).toEqual(['A'])
  await filterAttention('follow')
  expect(candidateNames()).toEqual([])
})

it('does not reuse positive attention on a stale snapshot', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'stale', rows: [candidate('A', { attention_context: attention() })] })
  await render()
  expect(host.querySelector('[data-attention-positive="true"]')).toBeNull()
  await filterAttention('strong')
  expect(candidateNames()).toEqual([])
})

it('does not highlight or filter cached positive attention outside continuous trading', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'closed', rows: [candidate('A', { attention_context: attention() })] })
  await render()
  expect(host.querySelector('[data-attention-positive="true"]')).toBeNull()
  expect(host.querySelector('[data-attention-summary]')?.textContent).toContain('联动数据已过期')
  expect(button('模拟买入').disabled).toBe(true)
  await filterAttention('strong')
  expect(candidateNames()).toEqual([])
  await filterAttention('follow')
  expect(candidateNames()).toEqual([])
  await filterAttention('both')
  expect(candidateNames()).toEqual([])
  await filterAttention('all')
  expect(candidateNames()).toEqual(['A'])
})

it('does not show a source-universe mean when the optional evidence is absent', async () => {
  const group = { ...attention().groups[0], market_mean_change_pct: undefined }
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention({ groups: [group] }) })] })
  await render()
  await expandEvidence()
  const cell = host.querySelector('[data-attention-cell]')!
  await act(async () => { const detail = cell.querySelector('details')!; detail.open = true; detail.dispatchEvent(new Event('toggle')) })
  expect(cell.textContent).not.toContain('同来源分类均值')
  expect(cell.textContent).toContain('平均涨幅 3.50%')
})

it('distinguishes unknown leader quotes from an observed opened limit', async () => {
  const group = attention().groups[0]
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention({
    status: 'partial', leader_follow: null, group: null, priority: 1,
    groups: [{ ...group, follow: null, leader: { ...group.leader!, at_limit: null } }],
  }) })] })
  await render()
  await expandEvidence()
  const cell = host.querySelector('[data-attention-cell]')!
  expect(cell.textContent).toContain('测试领涨股 · 触板状态待确认')
  expect(cell.textContent).not.toContain('已开板')
  await filterAttention('follow')
  expect(candidateNames()).toEqual([])
})

it('keeps the market context in the monitor header and omits the old notes', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), attention_sampling: {
    status: 'recorded', sample_count: 20, last_sample_at: '2026-09-30T09:45:00+08:00',
  } })
  await render()
  const gate = host.querySelector('[aria-label="盯盘状态"]')!
  expect(gate.textContent).toContain('市场环境：强势')
  expect(gate.textContent).toContain('环境允许提示')
  expect(gate.textContent).toContain('规则版本 1')
  // 盯盘中且没有全局阻断时不显示说明文字。
  expect(gate.querySelector('[role="status"]')).toBeNull()
  expect(candidateRows()[0].textContent).toContain('达到提示条件')
  expect(host.textContent).not.toContain('今日采样时点')
  expect(host.textContent).not.toContain('报价覆盖')
  expect(host.textContent).not.toContain('当前报价可用')
  expect(button('模拟买入').disabled).toBe(false)
  expect(calls.firstBoardResearch).not.toHaveBeenCalled()
})

it('keeps legacy responses usable and labels missing attention data as unconfirmed', async () => {
  await render()
  expect(host.querySelector('[data-attention-summary]')?.textContent).toContain('待确认')
  expect(button('模拟买入').disabled).toBe(false)
  await filterAttention('strong')
  expect(candidateNames()).toEqual([])
  expect(host.textContent).toContain('数据不足或过期的候选不计入重点筛选')
  await filterAttention('all')
  expect(candidateNames()).toEqual(['测试股票'])
})

it('treats an unsupported attention schema as unknown without breaking candidate actions', async () => {
  const unsupported = { schema_version: 2, status: 'ready' } as unknown as FirstBoardAttention
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: unsupported })] })
  await render()
  expect(host.querySelector('[data-attention-summary]')?.textContent).toContain('联动数据待确认')
  expect(button('模拟买入').disabled).toBe(false)
  await filterAttention('strong')
  expect(candidateNames()).toEqual([])
})

it('filters authoritative same-group attention and never combines separate concepts into both', async () => {
  const first = attention().groups[0]
  const crossed = attention({ group: null, priority: 1, groups: [
    { ...first, id: 'concept:strong', name: '强势但未带动', follow: false },
    { ...first, id: 'concept:follow', name: '带动但不强势', strong: false },
  ] })
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [
    candidate('A', { attention_context: attention() }),
    candidate('B', { attention_context: attention({ group: 'S1F0', priority: 1, leader_follow: false }) }),
    candidate('C', { attention_context: attention({ group: 'S0F1', priority: 1, strong_sector: false }) }),
    candidate('D', { attention_context: crossed }),
    candidate('E', { attention_context: attention({ status: 'stale' }) }),
    candidate('F'),
  ] })
  await render()
  await filterAttention('strong')
  expect(candidateNames()).toEqual(['A', 'B', 'D'])
  await filterAttention('follow')
  expect(candidateNames()).toEqual(['A', 'C', 'D'])
  await filterAttention('both')
  expect(candidateNames()).toEqual(['A'])
  await filterAttention('all')
  const crossedRow = candidateRows().find(row => row.textContent?.startsWith('D'))!
  expect(crossedRow.textContent).not.toContain('同板块双条件')
  expect(crossedRow.textContent).toContain('强势板块')
  expect(crossedRow.textContent).toContain('领涨带动')
  await act(async () => crossedRow.querySelector<HTMLButtonElement>('button[aria-label^="展开依据"]')!.click())
  const evidence = crossedRow.nextElementSibling!
  expect(evidence.textContent).not.toContain('同板块双条件')
  expect(evidence.textContent).toContain('强势但未带动')
  expect(evidence.textContent).toContain('带动但不强势')
  expect(evidence.textContent).toContain('不同板块条件，未合并')
})

it.each(['stale', 'unavailable'] as const)('removes positive attention highlights and priority when data are %s', async status => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention({ status, reasons: ['分钟数据暂不可用'] }) })] })
  await render()
  expect(host.querySelector('[data-attention-positive="true"]')).toBeNull()
  await expandEvidence()
  const cell = host.querySelector('[data-attention-cell]')!
  expect(cell.textContent).toContain(status === 'stale' ? '联动数据已过期' : '联动数据待确认')
  expect(cell.querySelector('[data-attention-positive="true"]')).toBeNull()
  expect(cell.textContent).toContain('分钟数据暂不可用')
  await filterAttention('both')
  expect(candidateNames()).toEqual([])
})

it('sorts attention priorities in both directions with unknowns last and retains filters on refresh', async () => {
  const data = { ...snapshot(), rows: [
    candidate('A'), candidate('B', { attention_context: attention() }),
    candidate('C', { attention_context: attention({ priority: 0, group: 'S0F0', strong_sector: false, leader_follow: false }) }),
    candidate('D', { attention_context: attention({ priority: 1, group: 'S1F0', leader_follow: false }) }),
    candidate('E', { attention_context: attention({ status: 'stale' }) }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  await sortBy('板块联动关注优先级')
  expect(candidateNames()).toEqual(['C', 'D', 'B', 'A', 'E'])
  await sortBy('板块联动关注优先级')
  expect(candidateNames()).toEqual(['B', 'D', 'C', 'A', 'E'])
  await filterAttention('strong')
  expect(candidateNames()).toEqual(['B', 'D'])
  await act(async () => host.querySelector<HTMLButtonElement>('table[aria-label="首板实时候选"] tbody td button')!.click())
  const navigation = () => JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav-list')!) as { name: string }[]
  expect(navigation().map(row => row.name)).toEqual(['B', 'D'])
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, rows: [data.rows[0], { ...data.rows[1], attention_context: attention({ status: 'stale' }) }, data.rows[2], data.rows[3], data.rows[4]] })
  await click('刷新')
  expect(candidateNames()).toEqual(['D'])
  expect(sortButton('板块联动关注优先级').closest('th')?.getAttribute('aria-sort')).toBe('descending')
  await filterAttention('all')
  await sortBy('板块联动关注优先级')
  expect(candidateNames()).toEqual(['A', 'B', 'C', 'D', 'E'])
  expect(client.getQueryData<FirstBoardSnapshot>(QK.firstBoardSnapshot)?.rows.map(row => row.name)).toEqual(['A', 'B', 'C', 'D', 'E'])
})

it('keeps candidate rows on refresh failure but stops treating cached attention as current', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention() })] })
  await render()
  await filterAttention('both')
  expect(candidateNames()).toEqual(['A'])
  calls.firstBoardSnapshot.mockRejectedValue(new Error('报价读取失败'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.firstBoardSnapshot }) })
  await settle()
  expect(candidateNames()).toEqual([])
  await filterAttention('all')
  expect(candidateNames()).toEqual(['A'])
  expect(host.querySelector('[data-attention-positive="true"]')).toBeNull()
  expect(button('模拟买入').disabled).toBe(true)
})

it('keeps yesterday feedback independent of candidate filters and opens its own stock navigation', async () => {
  const feedback = yesterdayFeedback()
  feedback.opening_distribution = { sample_count: 9, history_excluded_count: 0, open_count: 0,
    up_count: 0, up_ratio: null, mean_open_premium: null, bins: [] }
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), yesterday_feedback: feedback })
  await render()
  expect(host.textContent).toContain('昨日首板反馈')
  await click('展开分布')
  await filterPattern('trend')
  expect(candidateNames()).toEqual([])
  expect(host.textContent).toContain('昨日非一字涨停 9 只')
  expect(host.textContent).toContain('等待有效开盘价。')
  await click('查看明细')
  const table = host.querySelector('table[aria-label="昨日首板个股表现"]')!
  expect(table.textContent).toContain('昨日下跌样本')
  expect(table.textContent).toContain('-6.00%')
  const memberButton = [...table.querySelectorAll('button')].find(item => item.textContent?.startsWith('昨日下跌样本'))!
  await act(async () => memberButton.click())
  const dialog = host.querySelector('[role="dialog"]')!
  expect(dialog.getAttribute('data-symbol')).toBe('600100.SH')
  expect(JSON.parse(dialog.getAttribute('data-nav-list')!)).toEqual([
    { symbol: '300100.SZ', name: '昨日上涨样本' }, { symbol: '600100.SH', name: '昨日下跌样本' },
  ])
  expect(calls.firstBoardPaperOrder).not.toHaveBeenCalled()
})

it('marks yesterday feedback as a retained snapshot when a manual refresh fails', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), yesterday_feedback: yesterdayFeedback() })
  calls.firstBoardRefresh.mockRejectedValue(new Error('刷新请求失败'))
  await render()
  await click('刷新')
  const feedback = host.querySelector('[data-yesterday-feedback]')!
  expect(feedback.textContent).toContain('刷新失败，保留上次快照')
  const mean = [...feedback.querySelectorAll('dt')].find(item => item.textContent === '平均溢价')!.nextElementSibling!
  expect(mean.textContent).toBe('+7.00%')
  expect(mean.classList.contains('text-bull')).toBe(false)
})

it('registers one isolated route and 首板模式 menu', () => {
  expect(extension.routes?.map(route => route.path)).toEqual(['/first-board'])
  expect(extension.navigation?.[0].label).toBe('首板模式')
})
it.each([
  ['300001.SZ', '创'], ['301001.SZ', '创'], ['688001.SH', '科'], ['689009.SH', '科'],
])('shows the shared board badge beside the name for %s', async (symbol, label) => {
  const data = snapshot()
  data.rows[0] = { ...data.rows[0], symbol }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const nameButton = host.querySelector<HTMLButtonElement>('table[aria-label="首板实时候选"] tbody button')!
  const badge = [...nameButton.querySelectorAll('span')].find(span => span.textContent === label)
  expect(badge).toBeDefined()
  expect(badge?.classList.contains('w-[18px]')).toBe(true)
  expect(badge?.classList.contains(label === '创' ? 'text-[#f97316]' : 'text-cyan-400')).toBe(true)
  await act(async () => nameButton.click())
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe(symbol)
})
it.each([
  [.08, 10.8, '+8.00%', 'text-bull'], [-.025, 9.75, '-2.50%', 'text-bear'],
  [0, 10, '0.00%', 'text-muted'], [null, 10, '—', 'text-muted'],
])('colors candidate price and change consistently for change %s', async (change, price, formatted, color) => {
  const data = snapshot()
  data.rows[0] = { ...data.rows[0], price: price as number, change_pct: change as number | null }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const cells = host.querySelectorAll('table[aria-label="首板实时候选"] tbody tr td')
  expect(cells[3].classList.contains(color as string)).toBe(true)
  expect(cells[4].classList.contains(color as string)).toBe(true)
  expect(cells[4].textContent).toBe(formatted)
  expect(cells[0].textContent).not.toContain('创')
  expect(cells[0].textContent).not.toContain('科')
})
it('keeps a missing price neutral even when change is positive', async () => {
  const data = snapshot()
  data.rows[0] = { ...data.rows[0], price: null }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const cells = host.querySelectorAll('table[aria-label="首板实时候选"] tbody tr td')
  expect(cells[3].textContent).toBe('—')
  expect(cells[3].classList.contains('text-muted')).toBe(true)
  expect(cells[4].classList.contains('text-bull')).toBe(true)
})
it('defaults to descending change, keeps missing values last and reorders refreshed quotes', async () => {
  const data = { ...snapshot(), rows: [
    candidate('A', { change_pct: -.02 }), candidate('B', { change_pct: .05 }),
    candidate('C', { change_pct: null }), candidate('D', { change_pct: 0 }),
    candidate('E', { change_pct: .05 }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  expect(candidateNames()).toEqual(['B', 'E', 'D', 'A', 'C'])
  expect(sortButton('涨幅').closest('th')?.getAttribute('aria-sort')).toBe('descending')
  calls.firstBoardSnapshot.mockResolvedValue({ ...data, rows: data.rows.map(row => row.name === 'A' ? { ...row, change_pct: .1 } : row) })
  await click('刷新')
  expect(candidateNames()).toEqual(['A', 'B', 'E', 'D', 'C'])
  expect(sortButton('涨幅').closest('th')?.getAttribute('aria-sort')).toBe('descending')
})
it.each([
  ['形态', ['B', 'E', 'D', 'A', 'C'], ['A', 'C', 'D', 'B', 'E']],
  ['状态', ['C', 'E', 'B', 'D', 'A'], ['A', 'D', 'B', 'E', 'C']],
  ['现价', ['D', 'B', 'E', 'A', 'C'], ['C', 'A', 'E', 'B', 'D']],
  ['距涨停', ['C', 'A', 'E', 'B', 'D'], ['D', 'B', 'E', 'A', 'C']],
] as const)('sorts %s ascending, descending and back to snapshot order', async (label, ascending, descending) => {
  const data = { ...snapshot(), rows: [
    candidate('A', { pattern: 'oversold', state: 'invalid', price: 12, change_pct: .02, distance_to_limit_pct: .02 }),
    candidate('B', { pattern: 'platform', state: 'sealed', price: 2, change_pct: -.02, distance_to_limit_pct: .08 }),
    candidate('C', { pattern: 'oversold', state: 'watch', price: 30, change_pct: .11, distance_to_limit_pct: 0 }),
    candidate('D', { pattern: 'trend', state: 'broken', price: -1, change_pct: 0, distance_to_limit_pct: .1 }),
    candidate('E', { pattern: 'platform', state: 'approaching', price: 10, change_pct: .05, distance_to_limit_pct: .04 }),
  ] }
  Object.freeze(data.rows)
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const cached = client.getQueryData<FirstBoardSnapshot>(QK.firstBoardSnapshot)!
  const originalRows = [...cached.rows]
  expect(candidateNames()).toEqual(['C', 'E', 'A', 'D', 'B'])
  expect(sortButton(label)?.closest('th')?.getAttribute('aria-sort')).toBe('none')
  await sortBy(label)
  expect(candidateNames()).toEqual(ascending)
  expect(sortButton(label).closest('th')?.getAttribute('aria-sort')).toBe('ascending')
  await sortBy(label)
  expect(candidateNames()).toEqual(descending)
  expect(sortButton(label).closest('th')?.getAttribute('aria-sort')).toBe('descending')
  await sortBy(label)
  expect(candidateNames()).toEqual(['A', 'B', 'C', 'D', 'E'])
  expect(sortButton(label).closest('th')?.getAttribute('aria-sort')).toBe('none')
  expect(client.getQueryData(QK.firstBoardSnapshot)).toBe(cached)
  expect(cached.rows).toEqual(originalRows)
})
it.each([
  ['现价', 'price'], ['涨幅', 'change_pct'], ['距涨停', 'distance_to_limit_pct'],
] as const)('keeps missing %s last in both directions, accepts zero and preserves ties', async (label, field) => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [null, 2, NaN, 0, 2, Infinity, -1, -Infinity].map((value, index) => candidate(String.fromCharCode(65 + index), { [field]: value })) })
  await render()
  if (label === '涨幅') await sortBy(label)
  await sortBy(label)
  expect(candidateNames()).toEqual(['G', 'D', 'B', 'E', 'A', 'C', 'F', 'H'])
  await sortBy(label)
  expect(candidateNames()).toEqual(['B', 'E', 'D', 'G', 'A', 'C', 'F', 'H'])
})
it('starts a newly selected column in ascending order and clears the previous indicator', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { price: 20, change_pct: -.02 }), candidate('B', { price: 10, change_pct: .05 })] })
  await render()
  await sortBy('现价')
  await sortBy('现价')
  expect(candidateNames()).toEqual(['A', 'B'])
  await sortBy('涨幅')
  expect(candidateNames()).toEqual(['A', 'B'])
  expect(sortButton('现价').closest('th')?.getAttribute('aria-sort')).toBe('none')
  expect(sortButton('涨幅').closest('th')?.getAttribute('aria-sort')).toBe('ascending')
})
it('filters each candidate state using the displayed state labels', async () => {
  const states = ['watch', 'approaching', 'sealed', 'broken', 'invalid'] as const
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: states.map(state => candidate(state, { state })) })
  await render()
  const group = host.querySelector('[role="group"][aria-label="候选状态"]')!
  expect([...group.querySelectorAll('button')].map(item => item.textContent)).toEqual(['全部5', '观察中1', '临近涨停1', '涨停价观察1', '已开板1', '已失效1'])
  const labels = { watch: '观察中', approaching: '临近涨停', sealed: '涨停价观察', broken: '已开板', invalid: '已失效' }
  for (const state of states) {
    await filterState(labels[state])
    expect(candidateNames()).toEqual([state])
    expect(stateButton(labels[state]).getAttribute('aria-pressed')).toBe('true')
    expect([...host.querySelectorAll('h2')].find(item => item.textContent?.startsWith('今日候选'))?.textContent).toBe('今日候选 1 条')
  }
  await filterState('全部')
  expect(candidateNames()).toEqual([...states])
})
it('combines candidate state with other filters and retains sorting and state across refresh and tabs', async () => {
  const data = { ...snapshot(), rows: [
    candidate('A', { state: 'broken', price: 30 }), candidate('B', { state: 'approaching', price: 10 }),
    candidate('C', { state: 'broken', price: 20, pattern: 'oversold', can_buy: false }),
    candidate('D', { state: 'broken', price: 40 }),
  ] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  await sortBy('现价')
  await filterState('已开板')
  expect(candidateNames()).toEqual(['C', 'A', 'D'])
  // 状态计数按其余筛选统计，切换状态时保持不变。
  expect(stateButton('已开板').textContent).toBe('已开板3')
  expect(stateButton('临近涨停').textContent).toBe('临近涨停1')
  await filterPattern('trend')
  expect(candidateNames()).toEqual([])
  expect(host.textContent).toContain('当前筛选条件下暂无候选')
  await filterPattern('all')
  const actionable = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  await act(async () => actionable.click())
  expect(candidateNames()).toEqual(['A', 'D'])
  await act(async () => host.querySelector<HTMLButtonElement>('table[aria-label="首板实时候选"] tbody td button')!.click())
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav-list')!).map((row: { name: string }) => row.name)).toEqual(['A', 'D'])
  await click('关闭股票')
  const refreshed = { ...data, rows: [data.rows[0], data.rows[1], { ...data.rows[2], can_buy: true, price: 50 }, { ...data.rows[3], state: 'sealed' as const }] }
  calls.firstBoardSnapshot.mockResolvedValue(refreshed)
  await click('刷新')
  expect(candidateNames()).toEqual(['A', 'C'])
  expect(sortButton('现价').closest('th')?.getAttribute('aria-sort')).toBe('ascending')
  expect(client.getQueryData(QK.firstBoardSnapshot)).toEqual(refreshed)
  await click('信号记录')
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="信号状态"]')?.value).toBe('all')
  await click('今日盯盘')
  expect(stateButton('已开板').getAttribute('aria-pressed')).toBe('true')
  expect(candidateNames()).toEqual(['A', 'C'])
})
it('preserves sorting across filters, an empty result and a refreshed snapshot', async () => {
  const data = { ...snapshot(), rows: [candidate('A', { price: 30 }), candidate('B', { price: 10, can_buy: false }), candidate('C', { price: 20 }), candidate('D', { price: 5, pattern: 'oversold' })] }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  await sortBy('现价')
  expect(candidateNames()).toEqual(['D', 'B', 'C', 'A'])
  await filterPattern('trend')
  expect(candidateNames()).toEqual([])
  await filterPattern('platform')
  expect(candidateNames()).toEqual(['B', 'C', 'A'])
  const actionable = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  await act(async () => actionable.click())
  expect(candidateNames()).toEqual(['C', 'A'])
  const refreshed = { ...data, observed_at: '2026-09-30T10:01:00+08:00', rows: [candidate('E', { price: 40 }), data.rows[3], { ...data.rows[2], price: 50 }, data.rows[1], data.rows[0]] }
  calls.firstBoardSnapshot.mockResolvedValue(refreshed)
  await click('刷新')
  expect(candidateNames()).toEqual(['A', 'E', 'C'])
  expect(sortButton('现价').closest('th')?.getAttribute('aria-sort')).toBe('ascending')
  expect(client.getQueryData<FirstBoardSnapshot>(QK.firstBoardSnapshot)?.rows.map(row => row.name)).toEqual(['E', 'D', 'C', 'B', 'A'])
  await act(async () => actionable.click())
  expect(candidateNames()).toEqual(['B', 'A', 'E', 'C'])
  await filterPattern('all')
  expect(candidateNames()).toEqual(['D', 'B', 'A', 'E', 'C'])
  await sortBy('现价')
  expect(candidateNames()).toEqual(['C', 'E', 'A', 'B', 'D'])
  await sortBy('现价')
  expect(candidateNames()).toEqual(['E', 'D', 'C', 'B', 'A'])
})
it('navigates stock details in sorted visible order with duplicate symbols removed', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [
    candidate('A', { price: 30, pattern: 'platform' }), candidate('B', { price: 10, pattern: 'platform' }),
    candidate('A', { price: 30, pattern: 'trend' }), candidate('C', { price: 20, pattern: 'oversold', can_buy: false }),
  ] })
  await render()
  await sortBy('现价')
  await act(async () => host.querySelector<HTMLButtonElement>('table[aria-label="首板实时候选"] tbody td button')!.click())
  const navigation = () => JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav-list')!) as { name: string; symbol: string }[]
  expect(navigation().map(row => row.name)).toEqual(['B', 'C', 'A'])
  await act(async () => host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click())
  expect(navigation().map(row => row.name)).toEqual(['B', 'A'])
  await sortBy('现价')
  expect(navigation().map(row => row.name)).toEqual(['A', 'B'])
})
it('keeps the market temperature collapsed by default, remembers expansion and preserves scope', async () => {
  const data = { ...snapshot(), yesterday_feedback: yesterdayFeedback() }
  calls.firstBoardSnapshot.mockResolvedValue(data)
  await render()
  const feedback = host.querySelector('[data-yesterday-feedback]')!
  expect(button('展开分布').getAttribute('aria-expanded')).toBe('false')
  // 收起时只显示关键指标，不渲染开盘分布、完整指标与明细。
  expect([...feedback.querySelectorAll('dt')].find(item => item.textContent === '平均溢价')?.nextElementSibling?.textContent).toBe('+7.00%')
  expect(feedback.textContent).not.toContain('中位数')
  expect(feedback.textContent).not.toContain('今日开盘涨幅分布')
  expect(button('查看明细')).toBeUndefined()
  await click('双创')
  await click('展开分布')
  expect(localStorage.getItem('first-board-feedback-expanded')).toBe('true')
  expect(feedback.textContent).toContain('今日开盘涨幅分布')
  expect(feedback.textContent).toContain('中位数')
  expect(button('双创').getAttribute('aria-pressed')).toBe('true')
  await click('查看明细')
  expect(host.querySelector('table[aria-label="昨日首板个股表现"]')).not.toBeNull()
  const refreshed = { ...data, status: 'stale' as const, observed_at: '2026-09-30T10:05:00+08:00', revision: 2, environment: { ...data.environment, label: '偏弱', allowed: false } }
  calls.firstBoardSnapshot.mockResolvedValue(refreshed)
  await click('刷新')
  expect(host.querySelector('table[aria-label="昨日首板个股表现"]')).not.toBeNull()
  const gate = host.querySelector('[aria-label="盯盘状态"]')!
  expect(gate.textContent).toContain('10:05:00')
  expect(gate.textContent).toContain('规则版本 2')
  expect(gate.textContent).toContain('市场环境：偏弱')
  expect(gate.textContent).toContain('环境暂不允许提示')
  expect(host.textContent).toContain('数据已过期')
  await click('信号记录')
  await click('今日盯盘')
  expect(button('收起分布').getAttribute('aria-expanded')).toBe('true')
  await click('收起分布')
  expect(localStorage.getItem('first-board-feedback-expanded')).toBe('false')
  await click('信号记录')
  await click('今日盯盘')
  expect(button('展开分布').getAttribute('aria-expanded')).toBe('false')
  expect(candidateNames()).toEqual(['测试股票'])
})
it('shows global blocking reasons once in the status gate and keeps only stock reasons in rows', async () => {
  const realtime = '全局实时行情未启用, 请在实时行情设置中开启后继续盯盘'
  const session = '当前不在有效盘中时段'
  const coverage = '剔除本股后行业同行行情覆盖率不足 80%（同行有效行情 0/84，覆盖率 0.0%）'
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'closed', message: '当前为非交易时段, 显示最近行情观察结果, 不触发买卖提示。',
    global_blocked_reasons: [realtime, session], rows: [
      candidate('A', { can_buy: false, change_pct: .09, blocked_reasons: [realtime, session, coverage] }),
      candidate('B', { can_buy: false, change_pct: .07, blocked_reasons: [realtime, session] }),
    ] })
  await render()
  const gate = host.querySelector('[aria-label="盯盘状态"]')!
  expect(gate.textContent).toContain('当前为非交易时段')
  expect(gate.textContent).toContain(realtime)
  expect(gate.textContent).toContain(session)
  const [first, second] = candidateRows()
  expect(first.textContent).toContain(coverage)
  expect(first.textContent).not.toContain(realtime)
  expect(second.textContent).toContain('受全局条件暂停')
  expect(second.textContent).toContain('—')
  await expandEvidence()
  expect(evidenceRows()[0].textContent).toContain(coverage)
  expect(host.textContent!.split(realtime)).toHaveLength(2)
  expect(host.textContent!.split(session)).toHaveLength(2)
})
it('keeps every reason in the row when an older backend omits global reasons', async () => {
  const reason = '全局实时行情未启用, 请在实时行情设置中开启后继续盯盘'
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'closed', rows: [candidate('A', { can_buy: false, blocked_reasons: [reason, '同行覆盖不足'] })] })
  await render()
  expect(candidateRows()[0].textContent).toContain(reason)
  expect(candidateRows()[0].textContent).toContain('另有 1 项')
})
it('hides the introductory rule notice in the live monitor', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), rules: { ...config().rules, universe: 'hs_a_non_st' } })
  await render()
  expect(host.textContent).not.toContain('个交易日无收盘涨停')
  expect(host.textContent).not.toContain('临近涨停是买入候选提示')
  expect(host.querySelector('table[aria-label="首板实时候选"]')).not.toBeNull()
})
it('keeps legacy universe explicit and saves expanded scope with a 20 percent threshold', async () => {
  await render('rules')
  const scopeLabel = [...host.querySelectorAll('label')].find(item => item.textContent?.startsWith('候选市场范围'))!
  const select = scopeLabel.querySelector('select')!
  expect(select.value).toBe('main_board_non_st')
  expect([...host.querySelectorAll('li')].some(item => item.textContent?.includes('沪深主板（非 ST）'))).toBe(true)
  await act(async () => { select.value = 'hs_a_non_st'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await numberInput('10% 涨跌幅股票盘中最低涨幅', '20')
  await click('保存为新版本')
  const saved = calls.firstBoardSaveConfig.mock.calls[0][0]
  expect(saved.rules.universe).toBe('hs_a_non_st')
  expect(saved.rules.min_change_pct).toBe(.2)
})
it('shows decimal percentages correctly, permits quote-based alerts without minute capability and reuses stock preview', async () => {
  await render()
  expect(host.textContent).toContain('8.00%')
  expect(host.textContent).not.toContain('换手率 4.00%')
  await expandEvidence()
  expect(host.textContent).toContain('换手率 4.00%')
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
  await click('规则版本')
  expect(host.textContent).toContain('保存为新版本')
})
it('converts displayed percentages and 万元 to exact backend units before saving', async () => {
  await render('rules')
  await numberInput('10% 涨跌幅股票盘中最低涨幅', '6')
  await numberInput('最低成交额', '6000')
  await click('保存为新版本')
  const saved = calls.firstBoardSaveConfig.mock.calls[0][0]
  expect(saved.rules.min_change_pct).toBe(.06)
  expect(saved.rules.min_amount).toBe(60_000_000)
  expect(saved.rules.min_turnover_rate).toBe(2)
  expect(saved.revision).toBe(1)
})
async function toggle(label: string) {
  const input = [...host.querySelectorAll('label')].find(item => item.textContent?.includes(label))!.querySelector('input')!
  await act(async () => input.click())
  await settle()
}
it('saves the 20% threshold, feedback gate and optional exits in backend units', async () => {
  await render('rules')
  expect(host.textContent).not.toContain('昨日首板今日平均涨幅下限')
  await numberInput('20% 涨跌幅股票距涨停价最多', '5')
  await numberInput('20% 涨跌幅股票盘中最低涨幅', '12')
  await toggle('要求昨日首板反馈达标')
  expect(host.textContent).toContain('样本少于 10 只')
  await numberInput('昨日首板今日平均涨幅下限', '-1')
  await numberInput('昨日首板今日上涨占比下限', '60')
  await numberInput('盈利退出提示阈值', '5')
  await toggle('涨停时暂缓定时与盈利退出')
  await click('保存为新版本')
  const saved = calls.firstBoardSaveConfig.mock.calls[0][0]
  expect(saved.rules).toMatchObject({ approaching_distance: .03, approaching_distance_20cm: .05, min_change_pct: .05, min_change_pct_20cm: .12 })
  expect(saved).toMatchObject({ feedback_gate: true, feedback_min_mean_change: -.01, feedback_min_up_ratio: .6, exit_profit_pct: .05, exit_hold_limit_up: true })
})
it('clears an existing take-profit exit and hides feedback thresholds once the gate is off', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), feedback_gate: true, feedback_min_mean_change: .01, exit_profit_pct: .05 })
  await render('rules')
  const takeProfit = [...host.querySelectorAll('label')].find(item => item.textContent?.includes('盈利退出提示阈值'))!.querySelector('input')!
  expect(takeProfit.value).toBe('5')
  await numberInput('盈利退出提示阈值', '')
  expect(takeProfit.value).toBe('')
  await toggle('要求昨日首板反馈达标')
  expect(host.textContent).not.toContain('昨日首板今日平均涨幅下限')
  await click('保存为新版本')
  expect(calls.firstBoardSaveConfig.mock.calls[0][0]).toMatchObject({ exit_profit_pct: null, feedback_gate: false, feedback_min_mean_change: .01 })
})
async function toggleExact(label: string) {
  const input = [...host.querySelectorAll('label')].find(item => item.textContent?.trim() === label)!.querySelector('input')!
  await act(async () => input.click())
  await settle()
}
it('saves the observed stock roles and returns to all when the last role is cleared', async () => {
  await render('rules')
  expect(host.textContent).not.toContain('中军取前')
  await toggleExact('龙头')
  await toggleExact('中军')
  await numberInput('中军取前', '5')
  expect(host.textContent).toContain('历史验证不包含此过滤')
  await click('保存为新版本')
  expect(calls.firstBoardSaveConfig.mock.calls[0][0]).toMatchObject({ stock_roles: ['leader', 'core'], core_top_n: 5 })
})
it('treats clearing every role as observing all stocks', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), stock_roles: ['core'], core_top_n: 3 })
  await render('rules')
  await toggleExact('中军')
  expect(host.textContent).not.toContain('中军取前')
  await click('保存为新版本')
  expect(calls.firstBoardSaveConfig.mock.calls[0][0]).toMatchObject({ stock_roles: null })
})
it('labels matched roles and reports stocks hidden by the role filter', async () => {
  const base = snapshot()
  calls.firstBoardSnapshot.mockResolvedValue({ ...base, coverage: { ...base.coverage, role_hidden_count: 2 },
    rows: base.rows.map(row => ({ ...row, stock_role: { leader: true, core: null, reason: '', matches: [{ id: 'g', kind: 'concept', name: '概念A', change_rank: 1, cap_rank: null, amount_rank: null, roles: ['leader'] }] } })) })
  await render()
  expect(host.textContent).toContain('非所选角色未列出 2 只')
  const tag = [...host.querySelectorAll('span')].find(item => item.textContent === '龙头')!
  expect(tag.title).toBe('龙头：概念A')
})
it('shows the reference-day sentiment phase and mainlines, and filters candidates by mainline', async () => {
  const base = snapshot()
  const item = { kind: 'concept' as const, name: '机器人', rank: 1, limit_up_count: 9, max_boards: 4 }
  calls.firstBoardSnapshot.mockResolvedValue({ ...base,
    environment: { ...base.environment, phase: 'rally', phase_label: '主升', ladder: { max_consecutive: 7, promo_rate: .312, seal_rate: null, first_board: 60, ge2_count: 18, limit_up: 90, limit_down: 2, broken_limit: 20 } },
    mainlines: { date: '2026-09-29', status: 'ready', top_n: 5, items: [item, { ...item, kind: 'industry', name: '自动化设备', rank: 2 }] },
    rows: [
      candidate('主线股票', { symbol: '600001.SH', mainline: { hit: true, date: '2026-09-29', matches: [item], reason: '属于参考交易日主线：机器人' } }),
      candidate('非主线股票', { symbol: '600002.SH', mainline: { hit: false, date: '2026-09-29', matches: [], reason: '不属于参考交易日概念或行业主线前 5' } }),
      candidate('待确认股票', { symbol: '600003.SH', mainline: { hit: null, date: null, matches: [], reason: '主线数据暂不可用' } }),
    ] })
  await render()
  const status = host.querySelector('section[aria-label="盯盘状态"]')!
  // 缺失的封板率显示为占位符，不显示为 0%。
  // 没有盘中梯队（旧后端）时，阶段标签附带昨日收盘梯队；缺失的封板率显示占位符而非 0%。
  expect(status.textContent).toContain('情绪阶段（昨日）：主升高度 7 板 · 晋级 31% · 封板 —')
  expect(status.textContent).toContain('昨日主线：机器人')
  const tags = [...host.querySelectorAll('span')].filter(node => node.textContent === '主线')
  expect(tags).toHaveLength(1)
  expect(tags[0].title).toBe('2026-09-29 主线：机器人（第 1）')
  await toggleExact('只看主线')
  expect(candidateNames()).toEqual(['主线股票'])
})
it('shows seal quality only for candidates at the limit price and keeps unknown amounts blank', async () => {
  const base = snapshot()
  const seal = { vol_lots: 12000, sealed_amount: 132_000_000, seal_ratio: .2, age_seconds: 5.4, reason: '涨停价上卖一无挂单' }
  calls.firstBoardSnapshot.mockResolvedValue({ ...base, rows: [
    candidate('真封股票', { symbol: '600001.SH', state: 'sealed', can_buy: false, seal_quality: { ...seal, status: 'real' } }),
    candidate('假封股票', { symbol: '600002.SH', state: 'sealed', can_buy: false, seal_quality: { ...seal, status: 'fake', sealed_amount: null, seal_ratio: null, reason: '涨停价上卖一仍有挂单, 封单不稳' } }),
    candidate('无数据股票', { symbol: '600003.SH', state: 'sealed', can_buy: false, seal_quality: { status: 'unavailable', vol_lots: null, sealed_amount: null, seal_ratio: null, age_seconds: null, reason: '五档盘口封单数据未就绪' } }),
    candidate('临板股票', { symbol: '600004.SH' }),
  ] })
  await render()
  const stateCell = (name: string) => candidateRows().find(row => row.textContent?.includes(name))!.children[2].textContent
  expect(stateCell('真封股票')).toBe('涨停价观察真封 · 1.32亿')
  expect(stateCell('假封股票')).toBe('涨停价观察假封')
  expect(stateCell('无数据股票')).toBe('涨停价观察封单数据不可用')
  expect(stateCell('临板股票')).toBe('临近涨停')
  const fake = [...host.querySelectorAll('span')].find(node => node.textContent === '假封')!
  expect(fake.className).toContain('amber')
  expect(fake.title).toContain('卖一仍有挂单')
  await expandEvidence()
  // 封成比为小数制 0.2，须显示为 20%。
  expect(host.textContent).toContain('封板质量：真封，涨停价上卖一无挂单；封单 1.32亿 元（12,000 手），封成比 20.00%，5 秒前更新')
  expect(host.textContent).toContain('封板质量：封单数据不可用，五档盘口封单数据未就绪')
})
it('shows the live ladder beside the closed-day phase, using the dashboard counts', async () => {
  const base = snapshot()
  calls.firstBoardSnapshot.mockResolvedValue({ ...base,
    environment: { ...base.environment, date: '2026-11-10', phase: 'repair', phase_label: '修复', ladder: { max_consecutive: 7, promo_rate: .36, seal_rate: .82 } },
    market_ladder: { as_of: '2026-11-11', observed_at: '2026-11-11T10:00:00+08:00', live: true, sealed_corrected: false, limit_up: 51, broken: 23, limit_down: 1,
      max_consecutive: 4, first_board: 35, ge2_count: 16, seal_rate: .6892, promo_rate: .25, promo_pool: 48 } })
  await render()
  const status = host.querySelector('section[aria-label="盯盘状态"]')!
  expect(status.textContent).toContain('情绪阶段（昨日）：修复')
  // 有盘中数据时，昨日梯队只在悬停说明里，不与当日数字并列。
  expect(status.textContent).not.toContain('高度 7 板')
  expect(status.textContent).toContain('盘中梯队：高度 4 板 · 晋级 25% · 封板 69% · 涨停 51')
  const chip = [...status.querySelectorAll<HTMLElement>('span[title]')].find(node => node.textContent?.startsWith('盘中梯队'))!
  expect(chip.title).toContain('炸板 23 家，跌停 1 家')
})
it('hides sentiment and mainline chips for snapshots from an older backend', async () => {
  await render()
  const status = host.querySelector('section[aria-label="盯盘状态"]')!
  expect(status.textContent).not.toContain('情绪阶段')
  expect(status.textContent).not.toContain('昨日主线')
})
it('saves the sentiment phase and mainline gates and returns to unrestricted when cleared', async () => {
  await render('rules')
  expect(host.textContent).not.toContain('历史验证不包含这两项过滤')
  await toggleExact('不限阶段')
  await toggleExact('只提示主线内个股')
  expect(host.textContent).toContain('历史验证不包含这两项过滤')
  await click('保存为新版本')
  expect(calls.firstBoardSaveConfig.mock.calls[0][0]).toMatchObject({ allowed_market_phases: ['ignite', 'rally'], require_mainline: true })
})
it('treats clearing every sentiment phase as unrestricted', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), allowed_market_phases: ['rally'] })
  await render('rules')
  await toggleExact('主升')
  await click('保存为新版本')
  expect(calls.firstBoardSaveConfig.mock.calls[0][0]).toMatchObject({ allowed_market_phases: null })
})
it('polls the snapshot every 5 seconds only for live monitoring or same-day signals', async () => {
  const intervals = () => client.getQueryCache().find({ queryKey: QK.firstBoardSnapshot })?.observers.map(item => item.options.refetchInterval)
  await render('rules')
  expect(intervals()).toEqual([30000])
  await click('今日盯盘')
  expect(intervals()).toEqual([5000])
  await click('信号记录')
  expect(intervals()).toEqual([5000])
  await numberInput('交易日期', '2020-01-02')
  expect(intervals()).toEqual([30000])
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
it('requests an ISO event date even when the browser falls back to another locale', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-08T16:00:00Z'))
  const DateTimeFormat = Intl.DateTimeFormat
  vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(function (_locales, options) {
    return new DateTimeFormat('en-US', options)
  })
  await render('events')
  expect(calls.firstBoardEvents).toHaveBeenCalledWith('2026-10-09', {})
  const input = host.querySelector<HTMLInputElement>('input[type="date"]')!
  expect(input.value).toBe('2026-10-09')
  expect(input.max).toBe('2026-10-09')
  expect(host.textContent).toContain('该日暂无首板信号')
  await numberInput('交易日期', '2026-09-30')
  expect(calls.firstBoardEvents).toHaveBeenLastCalledWith('2026-09-30', {})
  expect(client.getQueryData(QK.firstBoardEvents('2026-09-30'))).toBeDefined()
  calls.firstBoardEvents.mockClear()
  await numberInput('交易日期', '')
  expect(input.value).toBe('2026-09-30')
  expect(calls.firstBoardEvents).not.toHaveBeenCalled()
})
it('opens signal records on the snapshot trading day until a date is chosen', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'closed', as_of: '2026-09-25' })
  await render('events')
  expect(calls.firstBoardEvents).toHaveBeenCalledTimes(1)
  expect(calls.firstBoardEvents).toHaveBeenCalledWith('2026-09-25', {})
  expect(host.querySelector<HTMLInputElement>('input[type="date"]')!.value).toBe('2026-09-25')
  await numberInput('交易日期', '2026-09-24')
  expect(calls.firstBoardEvents).toHaveBeenLastCalledWith('2026-09-24', {})
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), status: 'closed', as_of: '2026-09-26' })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.firstBoardSnapshot }) })
  await settle()
  expect(host.querySelector<HTMLInputElement>('input[type="date"]')!.value).toBe('2026-09-24')
})
it('renders historical events by selected date without turning them into live buys', async () => {
  calls.firstBoardEvents.mockResolvedValue({ events: [{ id: 'old1', ts: 1790720400000, date: '2026-09-30', symbol: '600000.SH', name: '历史股票',
    pattern: 'platform', pattern_label: '平台突破', state: 'approaching', event_type: 'buy_candidate', message: '历史候选信号', price: 10.8,
    revision: 1, evidence: { technical_shadow: technical, priority_score: { version: 'priority-v1', status: 'partial', score: 53, known_max: 70,
      parts: {}, primary_sector: null, role: 'unknown', can_buy: true, reason: '当时行情' } },
    blocked_reasons: [], quote_time: '2026-09-30T09:00:00+08:00' }], total: 1, day: beijingDay() })
  await render('events')
  expect(calls.firstBoardEvents).toHaveBeenCalledWith(beijingDay(), {})
  expect(host.textContent).toContain('历史候选信号')
  expect(host.textContent).toContain('信号时优先分 53 分')
  expect(host.textContent).toContain('上个交易日日线（历史） · 均线符合／MACD符合')
  await expandTechnical(host)
  expect(host.textContent).toContain('参考日 2026-09-29')
  expect(button('模拟买入')).toBeUndefined()
})
it('requests unfiltered first alerts for the chosen date, version and top seats, and keeps ranks separate from buys', async () => {
  const rank = { batch: 1, batch_total: 2, day: 4 }
  const report = { date: beijingDay(), revision: 4, available_revisions: [4, 3], source: 'first_buy_candidate', reference_date: '2026-09-29',
    offset: 0, limit: 100, matched: 1, total: 123, coverage: { batches: 12, change_known: 120, priority_known: 99, popularity_known: 65, technical_known: 19 },
    rows: [{ symbol: '600001.SH', name: '排名样本', pattern: 'platform', patterns: ['platform', 'trend'], signal_at: '2026-09-30T10:00:00+08:00',
      change_pct: .08, base_score: 53, priority_score: 61, popularity_known: true, popularity: { status: 'ready', rank: 7, rank_gain: 2, points: 8, known_max: 10 },
      ma_support: true, macd_support: null, ranks: { change: rank, base: rank, priority: { ...rank, day: 2 } } }] }
  calls.firstBoardFirstAlertRankings.mockImplementation(async (day, revision, top, offset) => ({ ...report,
    date: day, revision: revision ?? 4, top_n: top, offset, matched: top == null ? 123 : 1,
    rows: revision === 3 ? [{ ...report.rows[0], patterns: ['platform'] }] : report.rows }))
  await render('events')
  const panel = host.querySelector('[aria-label="首次提示排名对照"]')!
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenCalledWith(beijingDay(), null, 3, 0)
  expect(panel.textContent).toContain('匹配 1 / 当日 123 只')
  expect(panel.textContent).toContain('纳入人气后换入前 3')
  expect(panel.textContent).toContain('排名样本')
  expect(panel.querySelector('h3')?.textContent).toBe('首次提示排名对照')
  expect(panel.querySelector('tbody details')?.textContent).toContain('两种方案各取当时得分最高的形态')
  expect(panel.textContent).toContain('个股人气可核验 65')
  expect(panel.textContent).toContain('第 7 名')
  expect(panel.textContent).toContain('MACD 未知')
  expect(panel.querySelector('table')?.parentElement?.className).toContain('overflow-x-auto')
  async function select(label: string, value: string) {
    const el = panel.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!
    await act(async () => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })) }); await settle()
  }
  await select('排名规则版本', '3')
  expect(panel.textContent).not.toContain('两种方案各取当时得分最高的形态')
  await select('排名每日名额', '5')
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenLastCalledWith(beijingDay(), 3, 5, 0)
  const previousReads = calls.firstBoardFirstAlertRankings.mock.calls.length
  await click('刷新')
  expect(calls.firstBoardFirstAlertRankings.mock.calls.length).toBeGreaterThan(previousReads)
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenLastCalledWith(beijingDay(), 3, 5, 0)
  const rankingButton = [...panel.querySelectorAll('button')].find(el => el.textContent?.includes('排名样本'))!
  await act(async () => rankingButton.click())
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('600001.SH')
  expect(button('模拟买入')).toBeUndefined()
  await click('关闭股票')
  await select('排名每日名额', '')
  await click('下一页')
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenLastCalledWith(beijingDay(), 3, null, 100)
  await select('排名每日名额', '3')
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenLastCalledWith(beijingDay(), 3, 3, 0)
  await numberInput('交易日期', '2026-09-30')
  expect(calls.firstBoardFirstAlertRankings).toHaveBeenLastCalledWith('2026-09-30', null, 3, 0)
})
it('keeps the event log usable when a previous backend has no ranking endpoint', async () => {
  calls.firstBoardFirstAlertRankings.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
  await render('events')
  expect(host.textContent).toContain('当前服务未提供首次提示排名')
  expect(host.textContent).toContain('该日暂无首板信号')
  expect(host.textContent).not.toContain('Not Found')
})
it('combines event filters, keeps them across dates and tabs, and handles empty results and refresh errors', async () => {
  const records: FirstBoardEvent[] = [
    { id: 'near', pattern: 'oversold', pattern_label: '超跌反弹', state: 'approaching', event_type: 'approaching' },
    { id: 'buy', pattern: 'oversold', pattern_label: '超跌反弹', state: 'approaching', event_type: 'buy_candidate' },
    { id: 'exit', pattern: 'exit', pattern_label: '次日退出', state: 'exit_candidate', event_type: 'exit_candidate' },
  ].map(item => ({ ...item, ts: 1790720400000, date: beijingDay(), symbol: '600000.SH', name: item.id,
    message: `信号-${item.id}`, price: 10, revision: 1, evidence: {}, blocked_reasons: [], quote_time: null } as FirstBoardEvent))
  calls.firstBoardEvents.mockImplementation(async (day: string, filters: FirstBoardEventFilters) => {
    const events = records.filter(row => (!filters.pattern || row.pattern === filters.pattern) && (!filters.event_type || row.event_type === filters.event_type))
    return { events, day, total: events.length }
  })
  const select = async (label: string, value: string) => {
    const input = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!
    expect(input).not.toBeNull()
    await act(async () => { input.value = value; input.dispatchEvent(new Event('change', { bubbles: true })) })
    await settle()
  }
  const messages = () => [...host.querySelectorAll('ul.divide-y > li')].map(row => row.querySelector('p')?.textContent)
  await render('events')
  expect(messages()).toEqual(['信号-near', '信号-buy', '信号-exit'])
  await select('信号形态', 'oversold')
  expect(messages()).toEqual(['信号-near', '信号-buy'])
  await select('信号状态', 'approaching')
  expect(messages()).toEqual(['信号-near'])
  expect(host.querySelector('h2')?.textContent).toBe('信号记录 1 条')
  await numberInput('交易日期', '2026-09-30')
  expect(calls.firstBoardEvents).toHaveBeenLastCalledWith('2026-09-30', { pattern: 'oversold', event_type: 'approaching' })
  await click('今日盯盘')
  expect(host.querySelector('select')?.value).toBe('all')
  await click('信号记录')
  expect(messages()).toEqual(['信号-near'])
  await select('信号状态', 'exit_candidate')
  expect(messages()).toEqual([])
  expect(host.textContent).toContain('当前筛选条件下暂无信号')
  expect(host.textContent).not.toContain('该日暂无首板信号')
  await select('信号形态', 'exit')
  expect(messages()).toEqual(['信号-exit'])
  calls.firstBoardEvents.mockRejectedValueOnce(new Error('信号读取失败'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.firstBoard }) })
  await settle()
  expect(host.textContent).toContain('信号读取失败')
  expect(messages()).toEqual(['信号-exit'])
  await click('重试')
  expect(host.textContent).not.toContain('信号读取失败')
  await select('信号形态', 'all')
  await select('信号状态', 'all')
  expect(messages()).toEqual(['信号-near', '信号-buy', '信号-exit'])
  expect(client.getQueryData(QK.firstBoardEvents('2026-09-30', { pattern: 'oversold', event_type: 'approaching' }))).toBeDefined()
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
it('opens the separate trade validation entry and returns to the existing daily study', async () => {
  await render('research')
  expect(calls.firstBoardTradeValidationRuns).not.toHaveBeenCalled()
  await click('交易链验证')
  expect(host.textContent).toContain('完整交易链验证')
  expect(host.textContent).toContain('已记录信号成交回放（模型估计）')
  expect(calls.firstBoardTradeValidationRuns).toHaveBeenCalled()
  expect(calls.firstBoardTradeValidation).not.toHaveBeenCalled()
  await click('日线形态观察')
  await click('运行日线研究')
  expect(host.textContent).toContain('触板观察率')
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
