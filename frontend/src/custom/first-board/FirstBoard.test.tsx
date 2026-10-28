// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardAttention, FirstBoardCandidate, FirstBoardComparison, FirstBoardConfig, FirstBoardFeedback, FirstBoardResearch, FirstBoardSnapshot, FirstBoardSummary } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { FirstBoard } from './FirstBoard'
import extension from './extension'
import { validateDraft } from './RulesPanel'
import { beijingDay } from './shared'

const calls = vi.hoisted(() => ({
  firstBoardConfig: vi.fn(), firstBoardSaveConfig: vi.fn(), firstBoardSnapshot: vi.fn(), firstBoardRefresh: vi.fn(),
  firstBoardEvents: vi.fn(), firstBoardVersions: vi.fn(), firstBoardResearch: vi.fn(), firstBoardCompare: vi.fn(),
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
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.assign(capabilityState, { available: true, failed: false, ready: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.firstBoardConfig.mockResolvedValue(config())
  calls.firstBoardSnapshot.mockResolvedValue(snapshot())
  calls.firstBoardRefresh.mockResolvedValue(snapshot())
  calls.firstBoardVersions.mockResolvedValue({ versions: [config()] })
  calls.firstBoardEvents.mockResolvedValue({ events: [], total: 0, day: beijingDay() })
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
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.restoreAllMocks(); localStorage.removeItem('first-board-stock-marks') })
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
function candidateNames() {
  return [...host.querySelectorAll('table[aria-label="首板实时候选"] tbody tr')].map(row => row.querySelector('td button')!.firstChild!.textContent)
}
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

it('shows attention evidence with explicit decimal units without granting a simulated buy', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { can_buy: false, attention_context: attention() })] })
  await render()
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
  expect(host.textContent).toContain('仅作关注依据，原买入门槛仍生效')
  expect(button('模拟买入').disabled).toBe(true)
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
  expect(host.querySelector('[data-attention-cell]')?.textContent).toContain('联动数据已过期')
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
  const cell = host.querySelector('[data-attention-cell]')!
  expect(cell.textContent).toContain('测试领涨股 · 触板状态待确认')
  expect(cell.textContent).not.toContain('已开板')
  await filterAttention('follow')
  expect(candidateNames()).toEqual([])
})

it.each([
  ['recorded', '研究采样已记录'], ['already_recorded', '本时点已采样'], ['not_due', '等待下一采样时点'],
  ['paused', '研究采样暂停'], ['error', '研究采样暂不可用'],
] as const)('shows sampling status %s without changing original buy availability', async (status, label) => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), attention_sampling: {
    status, sample_count: 20, last_sample_at: '2026-09-30T09:45:00+08:00', reason: status === 'error' ? '采样写入失败' : undefined,
  } })
  await render()
  expect(host.textContent).toContain(`${label} · 今日采样时点 20`)
  expect(host.textContent).toContain('最近采样 09/30 09:45:00')
  if (status === 'error') expect(host.textContent).toContain('采样写入失败')
  expect(button('模拟买入').disabled).toBe(false)
  expect(calls.firstBoardResearch).not.toHaveBeenCalled()
})

it('keeps legacy responses usable and labels missing attention data as unconfirmed', async () => {
  await render()
  expect(host.querySelector('[data-attention-cell]')?.textContent).toContain('待确认')
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
  expect(host.querySelector('[data-attention-cell]')?.textContent).toContain('联动数据待确认')
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
  const crossedRow = [...host.querySelectorAll('tbody tr')].find(row => row.textContent?.startsWith('D'))!
  expect(crossedRow.textContent).not.toContain('同板块双条件')
  expect(crossedRow.textContent).toContain('强势但未带动')
  expect(crossedRow.textContent).toContain('带动但不强势')
})

it.each(['stale', 'unavailable'] as const)('removes positive attention highlights and priority when data are %s', async status => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), rows: [candidate('A', { attention_context: attention({ status, reasons: ['分钟数据暂不可用'] }) })] })
  await render()
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
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), yesterday_feedback: yesterdayFeedback() })
  await render()
  expect(host.textContent).toContain('昨日首板反馈')
  await filterPattern('trend')
  expect(candidateNames()).toEqual([])
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
it('shows the current universe in the live monitor', async () => {
  calls.firstBoardConfig.mockResolvedValue({ ...config(), rules: { ...config().rules, universe: 'hs_a_non_st' } })
  await render()
  expect(host.textContent).toContain('沪深主板、创业板、科创板（非 ST）')
})
it('keeps legacy universe explicit and saves expanded scope with a 20 percent threshold', async () => {
  await render('rules')
  const scopeLabel = [...host.querySelectorAll('label')].find(item => item.textContent?.startsWith('候选市场范围'))!
  const select = scopeLabel.querySelector('select')!
  expect(select.value).toBe('main_board_non_st')
  expect([...host.querySelectorAll('li')].some(item => item.textContent?.includes('沪深主板（非 ST）'))).toBe(true)
  await act(async () => { select.value = 'hs_a_non_st'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await numberInput('盘中最低涨幅', '20')
  await click('保存为新版本')
  const saved = calls.firstBoardSaveConfig.mock.calls[0][0]
  expect(saved.rules.universe).toBe('hs_a_non_st')
  expect(saved.rules.min_change_pct).toBe(.2)
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
it('requests an ISO event date even when the browser falls back to another locale', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-08T16:00:00Z'))
  const DateTimeFormat = Intl.DateTimeFormat
  vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(function (_locales, options) {
    return new DateTimeFormat('en-US', options)
  })
  await render('events')
  expect(calls.firstBoardEvents).toHaveBeenCalledWith('2026-10-09')
  const input = host.querySelector<HTMLInputElement>('input[type="date"]')!
  expect(input.value).toBe('2026-10-09')
  expect(input.max).toBe('2026-10-09')
  expect(host.textContent).toContain('该日暂无首板信号')
  await numberInput('交易日期', '2026-09-30')
  expect(calls.firstBoardEvents).toHaveBeenLastCalledWith('2026-09-30')
  expect(client.getQueryData(QK.firstBoardEvents('2026-09-30'))).toBeDefined()
  calls.firstBoardEvents.mockClear()
  await numberInput('交易日期', '')
  expect(input.value).toBe('2026-09-30')
  expect(calls.firstBoardEvents).not.toHaveBeenCalled()
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
