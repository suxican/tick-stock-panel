// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardConfig, FirstBoardPaperValidation, FirstBoardReplayValidation } from '@/lib/api'
import { TradeValidationPanel } from './TradeValidationPanel'
import { beijingDay } from './shared'

const calls = vi.hoisted(() => ({ firstBoardTradeValidation: vi.fn(), firstBoardTradeValidationRuns: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: calls }))
const config = { revision: 3, paper_account_id: 'current-account' } as FirstBoardConfig
const previous = { revision: 2, paper_account_id: 'previous-account' } as FirstBoardConfig
const options = { amount: 10000, commission_pct: .0003, stamp_tax_pct: .0005, slippage_bps: 5, participation_rate: .01, entry_window_minutes: 5, max_exit_sessions: 5 }
const base = { id: 'run1', created_at: '2026-10-21T15:30:00+08:00', request: { start: '2026-10-01', end: '2026-10-20', revision: 3, account_id: null, options, through: '2026-10-21' }, limitations: ['分钟回放不模拟涨停委托队列。'] }
function replay(): FirstBoardReplayValidation {
  return { ...base, kind: 'replay', schema_version: 1, mode: 'recorded_execution', status: 'modelled',
    model: { id: 'conservative-minute-close-v1', label: '保守分钟模型', price_proxy: 'later_complete_minute_close', signal_source: 'recorded_can_buy', options },
    coverage: { sample_count: 8, stock_days: 12, eligible_stock_days: 5, negative_stock_days: 4, unknown_stock_days: 3, signal_count: 5, calendar_available: true, minute_days_read: 3 },
    summary: { signals: 5, closed: 1, no_fill: 1, unverifiable: 2, open_position: 1, win_count: 1, net_return_mean: .012, gross_return_mean: .02, win_rate: 1, net_pnl_sum: 120 },
    by_group: {}, trades: [{ signal_id: 'signal-replay', symbol: '600000.SH', name: '模型样本', date: '2026-10-20', revision: 3, patterns: ['platform'], group: 'unknown', signal_at: '2026-10-20T10:00:00+08:00', order_at: '2026-10-20T10:00:00+08:00', entry_at: '2026-10-20T10:01:00+08:00', exit_signal_at: '2026-10-21T10:30:00+08:00', exit_order_at: '2026-10-21T10:30:00+08:00', exit_at: '2026-10-21T10:31:00+08:00', entry_bar_at: '2026-10-20T10:00:00+08:00', exit_signal_bar_at: '2026-10-21T10:29:00+08:00', exit_bar_at: '2026-10-21T10:30:00+08:00', qty: 1000, entry_raw_price: 10, entry_price: 10.005, exit_raw_price: 10.2, exit_price: 10.1949, buy_fee: 5, sell_fee: 10.1, entry_cost: 10010, exit_proceeds: 10184.8, gross_return: .02, net_return: .012, net_pnl: 120, status: 'closed', reasons: [] }],
    total_trades: 5, trades_truncated: true, fingerprint: 'fixture' }
}
function paper(): FirstBoardPaperValidation {
  return { ...base, request: { ...base.request, account_id: 'current-account', options: null }, kind: 'paper', status: 'incomplete', reason: '部分信号缺少账本证据', account_id: 'current-account',
    coverage: { signal_count: 6, displayed_count: 2, detail_limit: 500, ledger_order_count: 8, ledger_fill_count: 6, attributable_count: 5, unknown_count: 1, legacy_fee_default_count: 1, truncated: false },
    summary: { submitted_signals: 4, filled_signals: 3, closed_signals: 1, open_signals: 1, partially_closed_signals: 1, unfilled_signals: 1, unsubmitted_signals: 1, unknown_signals: 1, realized_net_pnl: 999, closed_net_pnl: 25, closed_win_rate: 1, closed_mean_net_return: .025 },
    trades: [
      { signal_id: 'signal-paper', symbol: '600001.SH', signal_at: '2026-10-20T10:00:00+08:00', revision: 3, status: 'partially_closed', reason: '仍持有100股', orders: [{ id: 'order-entry', side: 'buy', qty: 200, status: 'filled', created_at: '2026-10-20T10:00:01+08:00' }], fills: [{ order_id: 'order-entry', qty: 200, price: 10, fee: 5, ts: '2026-10-20T10:00:02+08:00' }], exits: [{ sell_order_id: 'order-exit', source: 'manual', manual_exit: true, qty: 100, price: 11, allocated_buy_cost: 1002.5, allocated_sell_fee: 5, ts: '2026-10-21T10:30:00+08:00', net_pnl: 92.5 }], remaining_qty: 100, realized_net_pnl: 92.5, closed_net_return: null },
      { signal_id: 'signal-missing', symbol: '600002.SH', signal_at: null, revision: 3, status: 'evidence_missing', reason: '成交记录缺失', orders: [], fills: [], exits: [], remaining_qty: null, realized_net_pnl: null, closed_net_return: null },
    ], fingerprint: 'paper-fixture' }
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
const openStock = vi.fn()
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.firstBoardTradeValidationRuns.mockResolvedValue({ runs: [] })
  calls.firstBoardTradeValidation.mockResolvedValue(replay())
  openStock.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(value = config) {
  await act(async () => root.render(<QueryClientProvider client={client}><TradeValidationPanel config={value} versions={[value, previous]} onOpenStock={openStock} /></QueryClientProvider>)); await settle()
}
function button(text: string) { return [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === text)! }
async function click(text: string) { await act(async () => button(text).click()); await settle() }
async function change(label: string, value: string) {
  const input = host.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!
  await act(async () => {
    const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
  }); await settle()
}
it('submits exact model costs and shows complete denominators with a traceable chain', async () => {
  await render(); await click('运行交易验证')
  expect(calls.firstBoardTradeValidation.mock.calls[0][0]).toMatchObject({ mode: 'replay', revision: 3, options })
  expect(host.textContent).toContain('模型估计')
  expect(host.textContent).toContain('闭环 1 / 5')
  expect(host.textContent).toContain('无法验证 2')
  expect(host.textContent).toContain('未成交 1')
  expect(host.textContent).toContain('持有中 1')
  expect(host.textContent).toContain('1.20%')
  expect(host.textContent).toContain('未知股票日 3')
  expect(host.textContent).toContain('signal-replay')
  expect(host.textContent).toContain('成交价已包含滑点')
  expect(host.textContent).toContain('此报告未记录均线与 MACD 排序对照')
  expect(host.querySelector('table[aria-label="交易验证明细"]')?.parentElement?.className).toContain('overflow-x-auto')
  await click('模型样本'); expect(openStock).toHaveBeenCalledWith('600000.SH', '模型样本')
})
it('shows phase and mainline groups frozen at the signal and hides them for older reports', async () => {
  const group = (signals: number, closed: number, net: number | null) => ({ signals, closed, no_fill: 0, unverifiable: signals - closed, open_position: 0, win_count: 0, net_return_mean: net, gross_return_mean: net, win_rate: null, net_pnl_sum: null })
  calls.firstBoardTradeValidation.mockResolvedValue({ ...replay(), by_group: { unknown: group(5, 1, .012) },
    by_phase: { rally: group(3, 1, .012), ebb: group(0, 0, null), unknown: group(2, 0, null) },
    by_mainline: { hit: group(1, 1, .012), miss: group(0, 0, null), unknown: group(4, 0, null) } })
  await render()
  await click('运行交易验证')
  const cells = (label: string) => [...host.querySelectorAll('table[aria-label="' + label + '"] tbody tr')].map(row => [...row.children].map(cell => cell.textContent))
  // 没有信号的阶段不列出；没有闭环的分组收益保持未知，不显示为 0。
  expect(cells('情绪阶段分组')).toEqual([['主升', '3', '1', '0', '2', '0', '1.20%'], ['阶段未知', '2', '0', '0', '2', '0', '—']])
  expect(cells('主线归属分组')).toEqual([['主线内', '1', '1', '0', '0', '0', '1.20%'], ['主线未知', '4', '0', '0', '4', '0', '—']])
  expect(host.textContent).toContain('被拦下的个股不会成为信号')
  calls.firstBoardTradeValidation.mockResolvedValue({ ...replay(), by_group: { unknown: group(5, 1, .012) } })
  await click('运行交易验证')
  expect(host.querySelector('table[aria-label="交易验证分组"]')).not.toBeNull()
  expect(host.querySelector('table[aria-label="情绪阶段分组"]')).toBeNull()
  expect(host.textContent).not.toContain('被拦下的个股不会成为信号')
})
it('separates paper closed returns from partial realizations and preserves unknown states', async () => {
  calls.firstBoardTradeValidation.mockResolvedValue(paper())
  await render(); await click('模拟盘成交归因'); await click('运行交易验证')
  expect(calls.firstBoardTradeValidation.mock.calls[0][0]).toMatchObject({ mode: 'paper', account_id: 'current-account' })
  expect(calls.firstBoardTradeValidation.mock.calls[0][0]).not.toHaveProperty('options')
  expect(host.textContent).toContain('闭环净收益合计：25.00 元')
  expect(host.textContent).toContain('已实现净收益（含部分退出）：999.00 元')
  expect(host.textContent).toContain('部分退出 1')
  expect(host.textContent).toContain('无法归属 1')
  expect(host.textContent).toContain('未发现委托不代表未尝试下单')
  expect(host.textContent).toContain('order-entry')
  expect(host.textContent).toContain('人工退出')
  const partial = host.querySelector('table[aria-label="交易验证明细"] tbody tr')!
  expect(partial.textContent).toContain('部分退出')
  expect(partial.querySelectorAll('td')[2].textContent).toBe('—')
  expect(partial.textContent).toContain('已退出部分的已实现净收益：92.50 元')
})
it('renders only frozen ranking samples and keeps unavailable outcomes unknown', async () => {
  const result = replay()
  const outcome = { ...result.summary, outcomes: 0, touched: 0, sealed: 0, broken: 0, filled: 1, worst_net_return: .012 }
  result.priority_comparison = { version: 'priority-v1', scored: 5, unscored: 2, popularity_known: 0,
    holdout_start: null, shadow: { ...outcome, signals: 0 },
    groups: [{ ...outcome, variant: 'popularity', window: 'all', top_n: 3 }] }
  calls.firstBoardTradeValidation.mockResolvedValue(result)
  await render(); await click('运行交易验证')
  const table = host.querySelector('table[aria-label="首板优先分对照"]')!
  expect(table.textContent).toContain('再加入个股人气')
  expect(table.querySelectorAll('tbody td')[2].textContent).toBe('—')
  expect(host.textContent).toContain('人气可核验 0 个')
})

it('compares technical variants on their own frozen event coverage and preserves missing T+1 outcomes', async () => {
  const result = replay()
  result.summary.signals = 0
  const outcome = { ...result.summary, signals: 3, outcomes: 2, touched: 1, sealed: 1, broken: 0, filled: 1,
    worst_net_return: .012, opportunity_days: 1, changed_days: 0, replacements: 0, t1_closed: 0, t1_net_return_mean: null }
  result.technical_comparison = {
    version: 'ma-macd-shadow-v1', baseline: 'priority-v1', eligible: 12, scored: 8, unscored: 4,
    priority_known: 10, ma_known: 9, macd_known: 8, score_tolerance: 2, holdout_start: '2026-10-19',
    groups: [
      { ...outcome, variant: 'baseline', top_n: 3, window: 'all' },
      { ...outcome, variant: 'ma', top_n: 3, window: 'all', changed_days: 1, replacements: 1 },
      { ...outcome, variant: 'macd', top_n: 5, window: 'all' },
      { ...outcome, variant: 'combined', top_n: 5, window: 'holdout', t1_closed: 1, t1_net_return_mean: -.0132,
        by_pattern: { platform: { ...outcome, signals: 1, outcomes: 1 } } },
    ],
  }
  calls.firstBoardTradeValidation.mockResolvedValue(result)
  await render(); await click('运行交易验证')
  expect(host.textContent).toContain('完整可比 8 / 12 个股票日')
  expect(host.textContent).toContain('原优先分可核验 10 个')
  expect(host.textContent).toContain('五分钟采样成交回放没有可验证的已记录信号')
  const table = () => host.querySelector('table[aria-label="均线与 MACD 影子排序结果"]')!
  expect(table().parentElement?.className).toContain('overflow-x-auto')
  expect(table().querySelectorAll('tbody tr')).toHaveLength(2)
  const cells = table().querySelectorAll('tbody tr:nth-child(2) td')
  expect([...cells].map(cell => cell.textContent)).toEqual(['3', '1', '1', '1', '2', '50.00%', '50.00%', '0.00%', '1 / 1', '1.20%', '1.20%', '0', '—'])
  await change('技术影子排序每日名额', '5')
  expect(table().textContent).toContain('加入 MACD')
  await change('技术影子排序样本区间', 'holdout')
  expect(table().textContent).toContain('均线与 MACD')
  expect(table().textContent).toContain('-1.32%')
  await change('技术影子排序入选形态', 'platform')
  expect(table().querySelector('tbody td')?.textContent).toBe('1')
  expect(table().querySelector('tbody td:last-child')?.textContent).toBe('—')
  expect(host.textContent).toContain('先按全体候选取每日前 5 只，再统计入选的平台突破股票日')
  await change('技术影子排序入选形态', 'oversold')
  expect(table().textContent).toContain('该组未记录所选形态')
  await change('技术影子排序每日名额', '3')
  expect(host.textContent).toContain('该区间与名额暂无可比样本')
})

it('shows how many technical samples were excluded while there is no common cohort', async () => {
  const result = replay()
  result.technical_comparison = { version: 'ma-macd-shadow-v1', baseline: 'priority-v1', eligible: 4,
    scored: 0, unscored: 4, priority_known: 2, ma_known: 1, macd_known: 0, score_tolerance: 2,
    holdout_start: null, groups: [] }
  calls.firstBoardTradeValidation.mockResolvedValue(result)
  await render(); await click('运行交易验证')
  expect(host.textContent).toContain('完整可比 0 / 4 个股票日，证据不足 4 个')
  expect(host.textContent).toContain('尚无同时记录原优先分、均线与 MACD 的可比样本')
  expect(host.querySelector('[aria-label="技术影子排序每日名额"]')).toBeNull()
})
it.each([['priority-v2', '完整可比 0 / 4 个股票日'], ['priority-v9', '技术排序规则版本待核验']])('reads technical comparisons against the %s baseline', async (baseline, text) => {
  const result = replay()
  result.technical_comparison = { version: 'ma-macd-shadow-v1', baseline, eligible: 4,
    scored: 0, unscored: 4, priority_known: 2, ma_known: 1, macd_known: 0, score_tolerance: 2,
    holdout_start: null, groups: [] }
  calls.firstBoardTradeValidation.mockResolvedValue(result)
  await render(); await click('运行交易验证')
  expect(host.textContent).toContain(text)
})

it('translates known execution reasons while retaining evidence suffixes and unknown codes', async () => {
  const result = replay()
  result.trades[0].reasons = ['entry_limit_blocked', 'minute_window_invalid: 缺少10:03分钟', 'future_reason']
  calls.firstBoardTradeValidation.mockResolvedValue(result)
  await render(); await click('运行交易验证')
  expect(host.textContent).toContain('触及涨停，模型未能买入')
  expect(host.textContent).toContain('分钟窗口无法验证：缺少10:03分钟')
  expect(host.textContent).toContain('future_reason')
})
it('clears stale results on mode, version and date changes and uses the frozen account', async () => {
  await render(); await click('运行交易验证'); await click('模拟盘成交归因')
  expect(host.textContent).not.toContain('signal-replay')
  await change('交易验证规则版本', '2'); expect(host.textContent).toContain('previous-account')
  calls.firstBoardTradeValidation.mockResolvedValue(paper()); await click('运行交易验证')
  expect(calls.firstBoardTradeValidation.mock.calls.at(-1)![0]).toMatchObject({ revision: 2, account_id: 'previous-account' })
  await change('交易验证开始日期', beijingDay(-10)); expect(host.textContent).not.toContain('signal-paper')
})
it('validates mode-specific dates, the 92-day bound, and cost ranges', async () => {
  await render(); await change('交易验证结束日期', beijingDay())
  expect(button('运行交易验证').disabled).toBe(true)
  await click('模拟盘成交归因'); expect(button('运行交易验证').disabled).toBe(false)
  await change('交易验证开始日期', beijingDay(-92)); expect(button('运行交易验证').disabled).toBe(true)
  await change('交易验证开始日期', beijingDay(-10)); await change('交易验证结束日期', beijingDay(-1)); await click('已记录信号成交回放（模型估计）')
  await change('入场窗口（分钟）', '2'); expect(button('运行交易验证').disabled).toBe(true)
  await change('入场窗口（分钟）', '3'); expect(button('运行交易验证').disabled).toBe(false)
  await change('成交量参与率（%）', '11'); expect(button('运行交易验证').disabled).toBe(true)
  await change('成交量参与率（%）', '2'); await change('双边佣金（%）', '0.04'); await click('运行交易验证')
  expect(calls.firstBoardTradeValidation.mock.calls.at(-1)![0].options).toMatchObject({ participation_rate: .02, commission_pct: .0004 })
})
it('shows loading, error and no-evidence results without replacing unknown returns with zero', async () => {
  let reject!: (reason: Error) => void
  calls.firstBoardTradeValidation.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
  await render(); await click('运行交易验证')
  expect(button('正在验证…').disabled).toBe(true)
  expect(host.querySelector('[aria-label="正在加载首板数据"]')).not.toBeNull()
  await act(async () => reject(new Error('账本损坏，无法验证'))); await settle()
  expect(host.textContent).toContain('账本损坏，无法验证')
  const empty = replay(); empty.status = 'no_signals'; empty.trades = []; empty.total_trades = 0; empty.summary = { signals: 0, closed: 0, no_fill: 0, unverifiable: 0, open_position: 0, win_count: 0, net_return_mean: null, gross_return_mean: null, win_rate: null, net_pnl_sum: null }
  calls.firstBoardTradeValidation.mockResolvedValue(empty); await click('运行交易验证')
  expect(host.textContent).toContain('没有可验证的已记录信号')
  expect(host.textContent).toContain('闭环平均净收益率：—')
})
it('explains unavailable accounts and can inspect saved results without rerunning', async () => {
  calls.firstBoardTradeValidationRuns.mockResolvedValue({ runs: [{ id: base.id, created_at: base.created_at, kind: 'replay', result: replay() }] })
  await render({ ...config, paper_account_id: null }); await click('模拟盘成交归因')
  expect(host.textContent).toContain('所选规则版本未关联模拟账户')
  await click('查看交易验证结果')
  expect(host.textContent).toContain('signal-replay')
  expect(calls.firstBoardTradeValidation).not.toHaveBeenCalled()
})
