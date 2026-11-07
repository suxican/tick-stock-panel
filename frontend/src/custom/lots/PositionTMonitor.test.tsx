// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Opportunity, PlanRow, PositionTMonitor } from './PositionTMonitor'
import { PositionTConfigForm, PositionTExecutionForm, PositionTPositionForm } from './PositionTForms'
import { PositionTDailyReview } from './PositionTDailyReview'
import { numberValue, signalCanPlan } from './positionTShared'
import { QK } from '@/lib/queryKeys'
import type { PositionTAccount, PositionTConfig, PositionTDailyReview as DailyReport, PositionTOverview, PositionTPlan, PositionTPosition, PositionTSignal } from '@/lib/api'

const mocks = vi.hoisted(() => ({ overview: vi.fn(), createPlan: vi.fn(), execution: vi.fn(), order: vi.fn(), savePosition: vi.fn(), planDetail: vi.fn(), syncLots: vi.fn(), config: vi.fn(), dailyReview: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  positionTOverview: (...args: unknown[]) => mocks.overview(...args),
  positionTCreatePlan: (...args: unknown[]) => mocks.createPlan(...args),
  positionTExecution: (...args: unknown[]) => mocks.execution(...args),
  positionTOrderEvent: (...args: unknown[]) => mocks.order(...args),
  positionTSavePosition: (...args: unknown[]) => mocks.savePosition(...args),
  positionTPlan: (...args: unknown[]) => mocks.planDetail(...args),
  positionTSyncLots: (...args: unknown[]) => mocks.syncLots(...args),
  positionTConfig: (...args: unknown[]) => mocks.config(...args),
  positionTDailyReview: (...args: unknown[]) => mocks.dailyReview(...args),
  monitorRuleOptions: async () => ({ sector_targets: { industry: [{ key: 'industry:test', name: '测试板块' }] } }),
} }))
vi.mock('@/components/StockIntradayChart', () => ({ StockIntradayChart: () => <div>分时图</div> }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const account = { id: 'a', name: '人工账户 A', mode: 'manual', available_cash: 5000, broker_frozen_cash: 0, cash_net_of_frozen: true, trade_date: '2026-10-16', snapshot_at: '2026-10-16T10:00:00+08:00', reconciled: true, version: 7, event_seq: 2, effective_cash: 5000, config: {} } as PositionTAccount
const position = { account_id: 'a', symbol: '600000.SH', name: '测试持仓', exchange: 'SH', board: 'main', security_type: 'stock', qty: 1000, sellable_qty: null, today_bought: 1000, today_sold: 0, broker_frozen_qty: 0, sellable_net_of_frozen: true, external_sell_qty: 0, core_qty: 800, target_qty: 1000, max_t_qty: 200, max_extra_qty: 200, cost_price: null, cost_method: 'manual_reference', enabled: true, trade_date: '2026-10-16', snapshot_at: '2026-10-16T10:00:00+08:00', reconciled: true, version: 3, benchmark_symbol: '000001.SH', sector_id: '' } as PositionTPosition
const signal = { id: 's', account_id: 'a', symbol: '600000.SH', direction: 'buy_sell', state: 'triggered', reasons: ['止跌条件成立'], evidence: { vwap: 10.2 }, first_price: [10, 10.01], second_price: [10.2, 10.21], quantity: 200, net_min: 25, net_max: 27, fees: {}, created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60000).toISOString(), strategy_version: 'test-v1', account_version: 7, position_version: 3, market: {}, source_mode: 'manual' } as PositionTSignal
const plan = { id: 'p', account_id: 'a', symbol: '600000.SH', direction: 'sell_buy', quantity: 200, state: 'first_pending', leg1_filled: 0, leg2_filled: 0, matched_qty: 0, unmatched_qty: 0, exposure_qty: 0, matched_pnl: 0, reserved_cash: 0, reserved_qty: 200, first_price: [10, 10.01], second_price: [9.8, 9.81], created_at: '', updated_at: '', version: 1, reasons: [], legs: [], strategy_version: 'v1' } as PositionTPlan
function overview(extra: Partial<PositionTOverview> = {}): PositionTOverview { return { accounts: [account], account, positions: [position], signals: [{ ...signal, state: 'paused', reasons: ['可卖数量未知'] }], plans: [], config: null, health: { running: true }, ...extra } }
function dailyReport(extra: Partial<DailyReport> = {}): DailyReport {
  return {
    account_id: 'a', date: '2026-10-16', schema_version: 1, revision: 1, source_event_seq: 10,
    status: 'final', evidence_status: 'available', generated_at: '2026-10-16T15:01:00+08:00', source_mode: 'simulation',
    summary: { realized_net: 38, realized_net_provisional: 38, realized_gross: 50, matched_fees: 12, matched_quantity: 200, started_count: 2, completed_count: 1, closed_today_count: 1, completion_rate: 0.5, win_count: 1, loss_count: 0, flat_count: 0, unknown_pnl_count: 0, trade_count: 3, buy_qty: 300, sell_qty: 200, trade_fees: 17, trade_fees_provisional: 17 },
    by_symbol: [], by_direction: [], unfinished: [{ plan_id: 'p', symbol: '600000.SH', direction: 'buy_sell', first_qty: 100, second_qty: 0, unmatched_qty: 100, exposure_qty: 100, pending_order_qty: 100, state: 'unfinished' }], trades: [], plans: [], blockers: [], suggestions: [{ code: 'unfinished', title: '先检查敞口', detail: '未完成部分继续保留。' }],
    coverage: { sample_count: 6, first_sample_at: null, last_sample_at: null, state_counts: {}, source_counts: {}, triggered_episode_count: 2, unaccepted_episode_count: 0, estimated_fee_count: 0 },
    strategy_versions: ['v1'], config_evidence: [], valuation: { close_equity: null, hold_equity: null, incremental_equity: null, max_drawdown: null, reason: '缺少收盘估值与完整权益轨迹' }, limitations: ['费用按第二笔成交日归属。'], ...extra,
  }
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.overview.mockResolvedValue(overview())
  mocks.createPlan.mockResolvedValue({ plan })
  mocks.execution.mockResolvedValue({ plan, execution: {} })
  mocks.savePosition.mockResolvedValue({ position })
  mocks.planDetail.mockResolvedValue({ plan, executions: [], events: [] })
  mocks.syncLots.mockResolvedValue({ created: [], skipped: [], account_version: 7 })
  mocks.config.mockResolvedValue({ account, config: account.config })
  mocks.dailyReview.mockResolvedValue({ report: dailyReport() })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(child: ReactNode, entry = '/lots') { await act(async () => root.render(<MemoryRouter initialEntries={[entry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><QueryClientProvider client={client}>{child}</QueryClientProvider></MemoryRouter>)); await settle() }
async function click(text: string) { const button = [...host.querySelectorAll('button')].find(b => b.textContent === text); expect(button).toBeDefined(); await act(async () => button!.click()); await settle() }
async function change(input: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => { const prototype = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true })) }); await settle()
}
function field(label: string) { const element = [...host.querySelectorAll('label')].find(x => x.textContent?.startsWith(label)); expect(element).toBeDefined(); return element!.querySelector('input')! }

it('shows account onboarding and does not manufacture a live opportunity', async () => {
  mocks.overview.mockResolvedValue(overview({ accounts: [], account: null, positions: [], signals: [] }))
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('从核对真实持仓开始')
  expect(host.textContent).not.toContain('建立计划并预留')
  expect(mocks.createPlan).not.toHaveBeenCalled()
})
it('keeps unknown sellable distinct from zero and gives an explicit paused result', async () => {
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('未知 / 0')
  expect(host.textContent).toContain('可卖数量未知')
  expect(host.textContent).toContain('人工登记 · 券商可执行性待核对')
  expect(host.textContent).not.toContain('建立计划并预留')
})
it('never displays previous account positions while another account is loading', async () => {
  const b = { ...account, id: 'b', name: '账户 B' }
  mocks.overview.mockImplementation((id: string) => id === 'b' ? new Promise(() => {}) : Promise.resolve(overview({ accounts: [account, b] })))
  await render(<PositionTMonitor />)
  await change(host.querySelector<HTMLSelectElement>('#position-t-account')!, 'b')
  expect(host.textContent).not.toContain('测试持仓')
  expect(host.textContent).toContain('正在读取监控账户')
})
it('syncs batch holdings explicitly into an empty selected account and refreshes the overview', async () => {
  const researchKey = QK.holdingAnalysisOverview('position_t', 'a')
  client.setQueryData(researchKey, { input_version: 'before-sync' })
  const imported = { ...position, reconciled: false, enabled: false, source: 'lots' }
  mocks.overview.mockResolvedValue(overview({ positions: [], signals: [] }))
  mocks.syncLots.mockImplementation(async () => {
    mocks.overview.mockResolvedValue(overview({ account: { ...account, version: 8 }, positions: [imported], signals: [] }))
    return { created: [{ symbol: position.symbol, name: position.name, qty: 1000 }], skipped: [], account_version: 8 }
  })
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('尚未添加持仓')
  expect(mocks.syncLots).not.toHaveBeenCalled()
  expect(client.getQueryState(researchKey)?.isInvalidated).toBe(false)
  await click('从批次提醒同步')
  expect(mocks.syncLots).toHaveBeenCalledWith('a', { expected_version: 7 })
  expect(host.textContent).toContain('已同步 1 只持仓')
  expect(host.textContent).toContain('测试持仓 · 1000 股')
  expect(host.textContent).toContain('批次提醒同步')
  expect(host.textContent).toContain('未知 / 0')
  expect(host.textContent).not.toContain('尚未添加持仓')
  expect(mocks.createPlan).not.toHaveBeenCalled()
  expect(client.getQueryState(researchKey)?.isInvalidated).toBe(true)
})
it('shows empty batch sync and skipped holdings as distinct outcomes', async () => {
  await render(<PositionTMonitor />)
  await click('从批次提醒同步')
  expect(host.textContent).toContain('批次提醒中没有可同步的持仓')
  mocks.syncLots.mockResolvedValue({ created: [], skipped: [{ symbol: position.symbol, reason: '该股票已存在，保留做 T 持仓' }], account_version: 7 })
  await click('从批次提醒同步')
  expect(host.textContent).toContain('本次未新增持仓')
  expect(host.textContent).toContain('已跳过 1 只')
  expect(host.textContent).toContain('该股票已存在，保留做 T 持仓')
  expect(host.textContent).not.toContain('批次提醒中没有可同步的持仓')
})
it('disables duplicate sync while pending and exposes an actionable failure', async () => {
  let rejectSync!: (error: Error) => void
  mocks.syncLots.mockReturnValue(new Promise((_resolve, reject) => { rejectSync = reject }))
  await render(<PositionTMonitor />)
  await click('从批次提醒同步')
  const pending = [...host.querySelectorAll('button')].find(b => b.textContent === '正在同步…')!
  expect(pending.disabled).toBe(true)
  await act(async () => pending.click())
  expect(mocks.syncLots).toHaveBeenCalledTimes(1)
  await act(async () => rejectSync(new Error('账户已变化，请刷新后重试')))
  await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('同步失败：账户已变化，请刷新后重试')
  expect([...host.querySelectorAll('button')].find(b => b.textContent === '从批次提醒同步')?.disabled).toBe(false)
})
it('does not show a previous account sync result after switching accounts during a request', async () => {
  const b = { ...account, id: 'b', name: '账户 B' }
  mocks.overview.mockImplementation((id: string) => Promise.resolve(overview({ accounts: [account, b], account: id === 'b' ? b : account, positions: [], signals: [] })))
  let resolveSync!: (value: unknown) => void
  mocks.syncLots.mockReturnValue(new Promise(resolve => { resolveSync = resolve }))
  await render(<PositionTMonitor />)
  await click('从批次提醒同步')
  await change(host.querySelector<HTMLSelectElement>('#position-t-account')!, 'b')
  await act(async () => resolveSync({ created: [{ symbol: '600000.SH', name: '旧账户持仓', qty: 1000 }], skipped: [], account_version: 8 }))
  await settle()
  expect(host.textContent).not.toContain('旧账户持仓')
  expect(host.textContent).not.toContain('已同步 1 只持仓')
  expect([...host.querySelectorAll('button')].find(b => b.textContent === '从批次提醒同步')?.disabled).toBe(false)
})
it('builds a plan only on explicit action, retaining account and position versions', async () => {
  const done = vi.fn()
  await render(<Opportunity signal={signal} position={position} unavailable={false} onCreated={done} />)
  expect(mocks.createPlan).not.toHaveBeenCalled()
  expect(mocks.execution).not.toHaveBeenCalled()
  await click('建立计划并预留')
  expect(mocks.createPlan).toHaveBeenCalledWith(expect.objectContaining({ signal_id: 's', quantity: 200, expected_account_version: 7, expected_position_version: 3 }))
  expect(done).toHaveBeenCalledTimes(1)
  expect(mocks.execution).not.toHaveBeenCalled()
})
it('expires old opportunities and rejects unknown expiry', async () => {
  await render(<Opportunity signal={{ ...signal, expires_at: '2020-01-01T00:00:00+08:00' }} unavailable={false} onCreated={vi.fn()} />)
  expect(host.textContent).toContain('已过期')
  expect(host.textContent).not.toContain('建立计划并预留')
  expect(signalCanPlan('triggered', 'invalid', 200)).toBe(false)
})
it('registers sell executions only after confirmation and leaves proceeds unverified by default', async () => {
  await render(<PositionTExecutionForm plan={plan} mode="manual" kind="execution" onClose={vi.fn()} onSaved={vi.fn()} />)
  expect(mocks.execution).not.toHaveBeenCalled()
  await change(field('委托编号'), 'order-1')
  await change(field('本次实际成交股数'), '100')
  await change(field('实际成交均价'), '10')
  const checks = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
  expect(checks[0].checked).toBe(false)
  await act(async () => checks[1].click())
  await click('确认登记成交')
  expect(mocks.execution).toHaveBeenCalledWith(expect.objectContaining({ quantity: 100, price: 10, leg: 1, proceeds_available: false, fee: null }))
  expect(mocks.order).not.toHaveBeenCalled()
})
it('preserves empty sellable as null when saving a manually checked position', async () => {
  await render(<PositionTPositionForm account={account} position={position} onClose={vi.fn()} onSaved={vi.fn()} />)
  const checks = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
  await act(async () => checks[checks.length - 1].click())
  await click('保存核对结果')
  expect(mocks.savePosition).toHaveBeenCalledWith('a', '600000.SH', expect.objectContaining({ sellable_qty: null, expected_version: 3, included_event_seq: 2 }))
  expect(numberValue('', '可卖', { optional: true })).toBeNull()
  expect(() => numberValue('100.5', '股数', { integer: true })).toThrow()
})
it('records a real existing holding below its core floor without lowering that floor', async () => {
  await render(<PositionTPositionForm account={account} position={position} onClose={vi.fn()} onSaved={vi.fn()} />)
  await change(field('总持仓'), '500')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('实际持仓已低于核心持仓下限')
  expect(field('核心持仓下限').value).toBe('800')
  const checks = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
  await act(async () => checks[checks.length - 1].click())
  await click('保存核对结果')
  expect(mocks.savePosition).toHaveBeenCalledWith('a', '600000.SH', expect.objectContaining({ qty: 500, core_qty: 800, expected_version: 3 }))
})
it('still rejects a new position whose core floor exceeds its total holding', async () => {
  await render(<PositionTPositionForm account={account} onClose={vi.fn()} onSaved={vi.fn()} />)
  await change(field('证券代码'), '600000.SH')
  await change(field('总持仓'), '500')
  await change(field('核心持仓下限'), '800')
  const checks = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
  await act(async () => checks[checks.length - 1].click())
  await click('保存核对结果')
  expect(mocks.savePosition).not.toHaveBeenCalled()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('新增持仓的核心持仓下限不能超过当前总持仓')
})
it('describes environment evidence as the change in the quoted percentage over five minutes', async () => {
  await render(<Opportunity signal={{ ...signal, evidence: { market_return_5m: .001, sector_return_5m: .002 } }} unavailable={false} onCreated={vi.fn()} />)
  expect(host.textContent).toContain('大盘5分钟涨跌幅变化（小数）')
  expect(host.textContent).toContain('板块5分钟涨跌幅变化（小数）')
})
it('surfaces read failures without claiming monitoring is healthy', async () => {
  mocks.overview.mockRejectedValue(new Error('服务连接中断'))
  await render(<PositionTMonitor />)
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('服务连接中断')
  expect(host.textContent).not.toContain('后台服务运行中')
})
it('keeps expired-contract fees and resulting cash and profit explicitly unverified', async () => {
  const unverifiedPlan = { ...plan, leg1_filled: 100, unmatched_qty: 100, matched_pnl: null }
  mocks.overview.mockResolvedValue(overview({ account: { ...account, cash_estimated: true }, plans: [unverifiedPlan] }))
  mocks.planDetail.mockResolvedValue({ plan: unverifiedPlan, executions: [{ id: 'fill-1', plan_id: 'p', leg: 1, order_id: 'o', quantity: 100, price: 10, executed_at: '2026-10-16T10:00:00+08:00', fee: null, allocated_fee: 0, fee_unverified: true }], events: [] })
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('账户资金含未核对费用')
  await click('未完成计划 (1)')
  expect(host.textContent).toContain('费用未核对，匹配净收益暂不计算')
  await click('跟踪／登记')
  expect(host.textContent).toContain('费用待核对（合同不适用）')
  expect(host.textContent).not.toContain('暂估 ¥0.00')
})

it('shows both directions in the holdings summary when only one direction triggers', async () => {
  mocks.overview.mockResolvedValue(overview({ signals: [{ ...signal, id: 'sell', direction: 'sell_buy', state: 'none' }, signal] }))
  await render(<PositionTMonitor />)
  const row = host.querySelector('tbody tr')!
  expect(row.textContent).toContain('先买后卖')
  expect(row.textContent).toContain('条件触发')
  expect(row.textContent).toContain('先卖后买')
  expect(row.textContent).toContain('无机会')
})

it('shows the second leg executable quote and net estimate only while fresh', async () => {
  const now = Date.now()
  const ready = { ...plan, unmatched_qty: 200, monitor: { state: 'ready', actionable_price: 9.8, net_profit: 30, computed_at: new Date(now).toISOString(), expires_at: new Date(now + 10000).toISOString(), second_leg_condition: '买回条件满足' } } as PositionTPlan
  await render(<PlanRow plan={ready} now={now} onOpen={vi.fn()} />)
  expect(host.textContent).toContain('第二笔条件满足')
  expect(host.textContent).toContain('9.80')
  expect(host.textContent).toContain('¥30.00')
  await act(async () => root.render(<PlanRow plan={ready} now={now + 10001} onOpen={vi.fn()} />))
  expect(host.textContent).toContain('跟踪数据已过期')
  expect(host.textContent).not.toContain('第二笔条件满足')
  expect(host.textContent).not.toContain('买回条件满足')
  expect(host.textContent).not.toContain('¥30.00')
})

it('opens the linked plan only in its requested account', async () => {
  const b = { ...account, id: 'b', name: '账户 B' }
  const bp = { ...plan, account_id: 'b' }
  mocks.overview.mockImplementation((id: string) => Promise.resolve(overview({ account: id === 'b' ? b : account, accounts: [account, b], plans: id === 'b' ? [bp] : [] })))
  mocks.planDetail.mockResolvedValue({ plan: bp, executions: [], events: [] })
  await render(<PositionTMonitor />, '/lots?account_id=b&plan_id=p&symbol=600000.SH')
  expect(mocks.overview).toHaveBeenCalledWith('b')
  expect(mocks.planDetail).toHaveBeenCalledWith('p')
  expect(host.textContent).toContain('两笔计划跟踪')
})

it('exposes expired account reconciliation with its recovery action', async () => {
  mocks.overview.mockResolvedValue(overview({ config: { position_max_age_seconds: 900 } as PositionTOverview['config'] }))
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('资金核对已到期')
  expect(host.textContent).toContain('持仓核对已到期')
  expect([...host.querySelectorAll('button')].some(button => button.textContent?.includes('核对资金'))).toBe(true)
})

it('does not present stale service health or a risky second leg as ready', async () => {
  mocks.overview.mockResolvedValue(overview({ health: { running: true, last_run: new Date(Date.now() - 60000).toISOString() } }))
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('后台检查已延迟')
  expect(host.textContent).not.toContain('后台服务运行中')
  const now = Date.now()
  await act(async () => root.render(<PlanRow now={now} plan={{ ...plan, unmatched_qty: 200, monitor: { state: 'risk', computed_at: new Date(now).toISOString(), expires_at: new Date(now + 10000).toISOString(), second_leg_condition: '第二笔条件满足', reasons: ['账户待核对'] } }} onOpen={vi.fn()} />))
  expect(host.textContent).toContain('第二笔风险待处理')
  expect(host.textContent).not.toContain('第二笔条件满足')
})

it('only offers automatic simulation for a simulated account and saves the versioned boolean', async () => {
  await render(<PositionTMonitor />)
  expect(host.textContent).not.toContain('启用自动模拟')
  const simulated = { ...account, id: 'sim', mode: 'simulation' as const }
  const config = { ...account.config, enabled: true, auto_simulation_enabled: false }
  mocks.overview.mockResolvedValue(overview({ accounts: [simulated], account: simulated, config }))
  await act(async () => client.invalidateQueries({ queryKey: QK.positionTAll })); await settle()
  await click('启用自动模拟')
  expect(mocks.config).toHaveBeenCalledWith('sim', expect.objectContaining({ auto_simulation_enabled: true, expected_version: 7 }))
  expect(typeof mocks.config.mock.calls[0][1].auto_simulation_enabled).toBe('boolean')
  expect(host.textContent).toContain('完整分钟')
  expect(host.textContent).toContain('非真实交易')
})

it('shows an enabled simulation with no runtime state as waiting and exposes stop errors', async () => {
  const simulated = { ...account, mode: 'simulation' as const }
  mocks.overview.mockResolvedValue(overview({ accounts: [simulated], account: simulated, config: { ...account.config, enabled: true, auto_simulation_enabled: true }, simulation: null }))
  mocks.config.mockRejectedValue(new Error('账户版本已变化，请刷新后重试'))
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('等待后台确认')
  await click('停止自动模拟')
  expect(mocks.config).toHaveBeenCalledWith('a', expect.objectContaining({ auto_simulation_enabled: false }))
  expect(host.textContent).toContain('账户版本已变化')
  expect(host.textContent).toContain('停止自动模拟')
})

it('shows paused simulation reasons and labels automatic plans', async () => {
  const simulated = { ...account, mode: 'simulation' as const }
  mocks.overview.mockResolvedValue(overview({ accounts: [simulated], account: simulated, config: { ...account.config, auto_simulation_enabled: true }, simulation: { state: 'paused', last_run: new Date().toISOString(), reasons: ['分钟数据不连续'] } }))
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('自动模拟已暂停')
  expect(host.textContent).toContain('分钟数据不连续')
  await act(async () => root.render(<PlanRow plan={{ ...plan, execution_mode: 'automatic_simulation' }} onOpen={vi.fn()} />))
  expect(host.textContent).toContain('自动模拟')
})

it('prevents duplicate simulation changes and manual plan creation while automatic mode is active', async () => {
  const simulated = { ...account, mode: 'simulation' as const }
  mocks.overview.mockResolvedValue(overview({ accounts: [simulated], account: simulated, signals: [signal], config: { ...account.config, enabled: true, auto_simulation_enabled: true }, simulation: { state: 'running', last_run: new Date().toISOString(), reasons: [] } }))
  let resolveSave!: (value: unknown) => void
  mocks.config.mockReturnValue(new Promise(resolve => { resolveSave = resolve }))
  await render(<PositionTMonitor />)
  const manual = [...host.querySelectorAll('button')].find(b => b.textContent === '建立计划并预留')!
  expect(manual.disabled).toBe(true)
  await act(async () => manual.click())
  expect(mocks.createPlan).not.toHaveBeenCalled()
  await click('停止自动模拟')
  const saving = [...host.querySelectorAll('button')].find(b => b.textContent === '正在保存…')!
  expect(saving.disabled).toBe(true)
  await act(async () => saving.click())
  expect(mocks.config).toHaveBeenCalledTimes(1)
  await act(async () => resolveSave({ account: simulated, config: simulated.config }))
})

it('loads daily facts only in the review tab and isolates selected dates and accounts', async () => {
  const second = { ...account, id: 'b', name: '账户 B' }
  mocks.overview.mockImplementation((id: string) => Promise.resolve(overview({ accounts: [account, second], account: id === 'b' ? second : account })))
  await render(<PositionTMonitor />)
  expect(mocks.dailyReview).not.toHaveBeenCalled()
  await click('盘后复盘')
  expect(mocks.dailyReview).toHaveBeenCalledWith('a', expect.any(String))
  expect(host.textContent).toContain('当日配对净收益')
  expect(host.textContent).toContain('¥38.00')
  expect(host.textContent).toContain('单日保守模拟')
  expect(host.textContent).not.toContain('整体账户权益')
  mocks.dailyReview.mockImplementation(() => new Promise(() => {}))
  await change(field('交易日'), '2026-10-15')
  expect(mocks.dailyReview).toHaveBeenLastCalledWith('a', '2026-10-15')
  expect(host.textContent).not.toContain('¥38.00')
  expect(host.textContent).toContain('正在读取复盘')
  await change(host.querySelector<HTMLSelectElement>('#position-t-account')!, 'b')
  expect(mocks.dailyReview).toHaveBeenLastCalledWith('b', expect.any(String))
  expect(host.textContent).not.toContain('¥38.00')
})

it('shows report failures with a retry action without displaying cached results', async () => {
  await render(<PositionTMonitor />)
  await click('盘后复盘')
  mocks.dailyReview.mockRejectedValue(new Error('复盘读取暂不可用'))
  await click('更新复盘')
  expect(host.textContent).toContain('复盘读取失败')
  expect(host.textContent).not.toContain('¥38.00')
  mocks.dailyReview.mockResolvedValue({ report: dailyReport() })
  await click('更新复盘')
  expect(host.textContent).toContain('¥38.00')
})

it('keeps unknown fees and insufficient evidence distinct from zero and historical equity', async () => {
  const report = dailyReport()
  report.summary.realized_net = null
  report.summary.trade_fees = null
  report.coverage.estimated_fee_count = 1
  report.execution_blockers = [{ reason: '自动委托等待完整分钟', count: 2 }]
  await render(<PositionTDailyReview report={report} />)
  const values = [...host.querySelectorAll('dl dd')].map(element => element.textContent)
  expect(values).toEqual(['待核对', '待核对', '50.0%', '1 个'])
  expect(host.textContent).toContain('缺少收盘估值与完整权益轨迹')
  expect(host.textContent).toContain('相对计划基准多出 100 股')
  expect(host.textContent).toContain('合同暂估')
  expect(host.textContent).toContain('自动委托等待完整分钟 · 2 次账户分钟样本')
  await act(async () => root.render(<PositionTDailyReview report={dailyReport({ evidence_status: 'insufficient', status: 'preliminary' })} />))
  expect(host.textContent).toContain('该日记录不足')
  expect(host.textContent).not.toContain('当日配对净收益')
  expect(host.textContent).not.toContain('¥38.00')
})

it('preserves automatic simulation as a boolean when editing numeric strategy settings', async () => {
  const config: PositionTConfig = { auto_simulation_enabled: true, enabled: true, fees_confirmed: true, minute_timestamp_semantics: 'start', min_net_profit: 10, min_net_return: 0.001, max_daily_plans: 3, max_symbol_daily_plans: 1, daily_quantity_ratio: 0.2, cooldown_minutes: 10, single_risk_budget: 50, daily_risk_budget: 100, max_extra_notional: 5000, quote_max_age_seconds: 15, position_max_age_seconds: 900, new_plan_cutoff: '14:30', max_leg_minutes: 30, commission_rate: 0.0003, min_commission: 5, stamp_tax_rate: 0.0005, transfer_rate: 0.00001, slippage_per_share: 0.01, fee_effective_from: '2026-01-01' }
  await render(<PositionTConfigForm account={account} config={config} onClose={vi.fn()} onSaved={vi.fn()} />)
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].some(input => input.value === 'true')).toBe(false)
  await change(field('最低净价差（元）'), '15')
  await click('保存配置')
  expect(mocks.config).toHaveBeenCalledWith('a', expect.objectContaining({ min_net_profit: 15, auto_simulation_enabled: true, expected_version: 7 }))
})

it('identifies a still-reserved first leg even without exposure or pending orders', async () => {
  const report = dailyReport()
  report.unfinished = [{ plan_id: 'p', symbol: '600000.SH', direction: 'buy_sell', first_qty: 0, second_qty: 0, unmatched_qty: 0, exposure_qty: 0, pending_order_qty: 0, remaining_first_qty: 200, state: 'unfinished' }]
  await render(<PositionTDailyReview report={report} />)
  expect(host.textContent).toContain('第一笔仍可继续 200 股')
  expect(host.textContent).not.toContain('未决委托 200 股')
})

it('still allows stopping automatic simulation when overview refresh fails', async () => {
  const simulated = { ...account, mode: 'simulation' as const }
  mocks.overview.mockResolvedValue(overview({ accounts: [simulated], account: simulated, config: { ...account.config, enabled: true, auto_simulation_enabled: true } }))
  await render(<PositionTMonitor />)
  mocks.overview.mockRejectedValue(new Error('行情检查暂不可用'))
  await click('刷新')
  expect(host.textContent).toContain('监控数据读取失败')
  await click('停止自动模拟')
  expect(mocks.config).toHaveBeenCalledWith('a', expect.objectContaining({ auto_simulation_enabled: false }))
})
