// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Opportunity, PositionTMonitor } from './PositionTMonitor'
import { PositionTExecutionForm, PositionTPositionForm } from './PositionTForms'
import { numberValue, signalCanPlan } from './positionTShared'
import type { PositionTAccount, PositionTOverview, PositionTPlan, PositionTPosition, PositionTSignal } from '@/lib/api'

const mocks = vi.hoisted(() => ({ overview: vi.fn(), createPlan: vi.fn(), execution: vi.fn(), order: vi.fn(), savePosition: vi.fn(), planDetail: vi.fn(), syncLots: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  positionTOverview: (...args: unknown[]) => mocks.overview(...args),
  positionTCreatePlan: (...args: unknown[]) => mocks.createPlan(...args),
  positionTExecution: (...args: unknown[]) => mocks.execution(...args),
  positionTOrderEvent: (...args: unknown[]) => mocks.order(...args),
  positionTSavePosition: (...args: unknown[]) => mocks.savePosition(...args),
  positionTPlan: (...args: unknown[]) => mocks.planDetail(...args),
  positionTSyncLots: (...args: unknown[]) => mocks.syncLots(...args),
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

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.overview.mockResolvedValue(overview())
  mocks.createPlan.mockResolvedValue({ plan })
  mocks.execution.mockResolvedValue({ plan, execution: {} })
  mocks.savePosition.mockResolvedValue({ position })
  mocks.planDetail.mockResolvedValue({ plan, executions: [], events: [] })
  mocks.syncLots.mockResolvedValue({ created: [], skipped: [], account_version: 7 })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(child: ReactNode) { await act(async () => root.render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><QueryClientProvider client={client}>{child}</QueryClientProvider></MemoryRouter>)); await settle() }
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
  const imported = { ...position, reconciled: false, enabled: false, source: 'lots' }
  mocks.overview.mockResolvedValue(overview({ positions: [], signals: [] }))
  mocks.syncLots.mockImplementation(async () => {
    mocks.overview.mockResolvedValue(overview({ account: { ...account, version: 8 }, positions: [imported], signals: [] }))
    return { created: [{ symbol: position.symbol, name: position.name, qty: 1000 }], skipped: [], account_version: 8 }
  })
  await render(<PositionTMonitor />)
  expect(host.textContent).toContain('尚未添加持仓')
  expect(mocks.syncLots).not.toHaveBeenCalled()
  await click('从批次提醒同步')
  expect(mocks.syncLots).toHaveBeenCalledWith('a', { expected_version: 7 })
  expect(host.textContent).toContain('已同步 1 只持仓')
  expect(host.textContent).toContain('测试持仓 · 1000 股')
  expect(host.textContent).toContain('批次提醒同步')
  expect(host.textContent).toContain('未知 / 0')
  expect(host.textContent).not.toContain('尚未添加持仓')
  expect(mocks.createPlan).not.toHaveBeenCalled()
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
