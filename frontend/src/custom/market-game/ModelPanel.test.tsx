// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MarketGameModelEnvelope, MarketGameReport } from '@/lib/api'
import { ModelPanel } from './ModelPanel'

const calls = vi.hoisted(() => ({ read: vi.fn(), evaluate: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { marketGameModel: calls.read, marketGameEvaluateModel: calls.evaluate } }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.read.mockReset(); calls.evaluate.mockReset()
  calls.read.mockImplementation(async (id: string) => envelope(id))
  calls.evaluate.mockImplementation(async (id: string) => envelope(id))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(data = report()) { await act(async () => root.render(<QueryClientProvider client={client}><ModelPanel key={data.id} report={data} /></QueryClientProvider>)); await settle() }
async function click(label: string) { const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === label); expect(button).toBeDefined(); await act(async () => button!.click()); await settle() }
function report(id = 'one'): MarketGameReport {
  return { id, as_of: '2026-09-30', cutoff: '2026-09-30T15:30:00+08:00', created_at: '2026-09-30T16:00:00+08:00', quality: 'limited', research_only: false,
    summary: '冻结计划', input_version: 'snapshot', rule_version: '1.1', market_state: { trend: '上行', phase: '启动', emotion: '改善', crowding: '适中', change: '待观察' },
    allocation: { min: 0, max: .2, total_cap: .2, single_cap: .1, sector_cap: .2, risk_per_trade: .005, reason: '等待条件' }, hypotheses: [], scenarios: [], sectors: [], candidates: [], evidence: [], limitations: [] }
}
function envelope(id = 'one'): MarketGameModelEnvelope { return { report_id: id, evaluated_at: null, feedback: [], execution: null, adaptation: null, limitations: [] } }
function frozenReport(): MarketGameReport {
  return { ...report(), ecology: { version: '1', shadow_only: true, scope: 'audited_daily_sample', state: 'contracting', label: '风险收缩', as_of: '2026-09-30', cutoff: '2026-09-30T15:30:00+08:00', metadata_scope: 'latest', universe_version: '1', membership_version: null, coverage: .8, comparison_date: null,
    metrics: [{ id: 'activity', label: '成交活跃度', value: 0, unit: 'multiple', sample_size: 20, reason: '相对历史中位数' }, { id: 'hhi', label: '成交集中度', value: null, unit: 'index', sample_size: 0, reason: '缺少可比样本' }], sectors: [{ name: '机械', member_count: 10, amount_share: .125, share_change: null, breadth: .3, avg_return: -.01, relative_return: .002, status: '参与收窄', evidence: ['样本覆盖有限'] }], evidence: ['上涨覆盖收窄'], interpretation: '收缩待验证', limitations: ['只描述可核验样本'] },
    feedback_hypothesis: { version: '1', id: 'hyp-one', shadow_only: true, entry_authorized: false, kind: 'repair', label: '恐慌修复假设', as_of: '2026-09-30', cutoff: '2026-09-30T15:30:00+08:00', observation_sessions: ['2026-10-09'], expires_at: null, risk_appetite: null, panic_pressure: 82, minimum_samples: 60, confirmation: [{ metric: 'support', operator: '>=', value: 60, description: '承接持续改善' }], invalidation: [{ metric: 'drawdown', operator: '>', value: .03, description: '再次扩大回撤' }], interpretation: '等候承接', alternative: '可能只是短时反弹', limitations: [] } }
}

it('reads saved research only and keeps old reports explicitly unavailable', async () => {
  await render()
  expect(calls.read).toHaveBeenCalledWith('one'); expect(calls.evaluate).not.toHaveBeenCalled()
  expect(host.textContent).toContain('此报告未保存竞争环境')
  expect(host.textContent).toContain('此报告未冻结反馈假设')
  expect(host.textContent).toContain('尚未保存模型验证')
  await click('更新模型验证')
  expect(calls.evaluate).toHaveBeenCalledWith('one')
})

it('renders frozen metrics, coverage and conditions without replacing missing values with zero', async () => {
  await render(frozenReport())
  const ecology = host.querySelector('section[aria-label="市场竞争环境"]')!
  expect(ecology.textContent).toContain('风险收缩'); expect(ecology.textContent).toContain('80.00%')
  expect(ecology.textContent).toContain('0.00 倍'); expect(ecology.textContent).toContain('缺少可比样本')
  expect(host.textContent).toContain('12.50%'); expect(host.textContent).toContain('-1.00%')
  expect(host.textContent).toContain('份额变化尚未核验')
  expect(host.textContent).toContain('承接持续改善'); expect(host.textContent).toContain('再次扩大回撤')
  expect(host.textContent).toContain('可能只是短时反弹')
})

it('separates downward direction from reinforcing feedback and excludes immature labels from presented returns', async () => {
  const data = envelope()
  data.feedback = [{ version: '1', hypothesis_id: 'hyp-one', shadow_only: true, entry_authorized: false, scope: 'frozen_plan_sample', observed_at: '2026-10-09T10:00:00+08:00', status: 'available', summary: '下跌自我加强', rows: [{ symbol: '600001.SH', name: '样本公司', observed_at: '2026-10-09T10:00:00+08:00', status: 'contradicted', price_direction: 'down', feedback_type: 'reinforcing', stage: 'fragile', label: '修复假设未获支持', evidence: ['下跌扩大'], reason: '承接不足' }], limitations: [] }]
  data.execution = { version: '1', report_id: 'one', evaluated_at: '2026-10-09T16:00:00+08:00', kind: 'simulated_execution', input_version: '1', rule_version: '1', costs: { account_equity: 1000000, commission_rate: .0003, minimum_commission: 5, sell_tax_rate: .0005, transfer_rate: .00001, slippage: .001, max_volume_participation: .01 }, evidence_summary: [], status: 'partial', entry_authorized: false, rows: [{ symbol: '600001.SH', name: '样本公司', mode: 'panic_repair', sector: '机械', regime: 'contracting', signal_time: null, entry_time: null, entry_price: null, quantity: 0, reason: '尚未完成入场核验', labels: [{ horizon: 1, trade_date: '2026-10-09', state: 'not_executable', mature: true, exit_time: null, exit_price: null, net_return: .9, outcome_available_at: null, reason: 'T+1 尚不可退出' }] }], limitations: [] }
  data.adaptation = { version: '1', evaluated_at: '2026-10-09T16:00:00+08:00', status: 'shadow', automatic_adjustment: false, validation_status: 'not_validated', groups: [{ id: 'group-one', rule_version: '1', walk_forward: { state: 'insufficient', training_min_dates: 30, test_dates: 0, accepted_dates: 0, withheld_dates: 0, fixed_mean_net_return: null, filtered_mean_net_return: null, difference: null, reason: '滚动对照仍待成熟样本' }, mode: 'panic_repair', regime: 'contracting', horizon: 1, sample_size: 0, independent_dates: 0, excluded_count: 1, state: 'insufficient', mean_net_return: null, reason: '尚无成熟成交样本' }], limitations: [] }
  calls.read.mockResolvedValue(data); await render(frozenReport())
  const feedback = host.querySelector('section[aria-label="反馈兑现记录"]')!
  expect(feedback.textContent).toContain('下行'); expect(feedback.textContent).toContain('自我加强')
  expect([...feedback.querySelectorAll('td')].find(item => item.textContent === '下行')?.className).toContain('text-emerald-700')
  expect(host.textContent).toContain('T+1 尚不可退出'); expect(host.textContent).not.toContain('90.00%')
  expect(host.textContent).toContain('尚无成熟成交样本'); expect(host.textContent).toContain('自动调仓未启用')
  expect(host.textContent).toContain('滚动对照仍待成熟样本')
})

it('shows loading and retriable read errors, keeping frozen evidence available', async () => {
  let reject!: (error: Error) => void
  calls.read.mockReturnValue(new Promise((_, rej) => { reject = rej }))
  await render(frozenReport())
  expect(host.textContent).toContain('正在读取模型验证'); expect(host.textContent).toContain('恐慌修复假设')
  await act(async () => reject(new Error('权限不足'))); await settle()
  expect(host.textContent).toContain('权限不足')
  expect([...host.querySelectorAll('button')].find(item => item.textContent === '更新模型验证')!.disabled).toBe(true)
  calls.read.mockResolvedValue(envelope()); await click('重试读取模型验证')
  expect(host.textContent).toContain('尚未保存模型验证')
})

it('retains saved results on evaluation error and isolates late results between plans', async () => {
  calls.read.mockImplementation(async (id: string) => ({ ...envelope(id), limitations: [`已保存 ${id}`] }))
  await render(); calls.evaluate.mockRejectedValueOnce(new Error('行情读取失败')); await click('更新模型验证')
  expect(host.textContent).toContain('行情读取失败'); expect(host.textContent).toContain('已保存 one')
  let resolve!: (data: MarketGameModelEnvelope) => void
  calls.evaluate.mockReturnValueOnce(new Promise(res => { resolve = res }))
  await click('更新模型验证'); await render(report('two'))
  await act(async () => resolve({ ...envelope(), limitations: ['旧计划晚到结果'] })); await settle()
  expect(host.textContent).toContain('已保存 two'); expect(host.textContent).not.toContain('旧计划晚到结果')
  expect(host.textContent).not.toContain('行情读取失败')
})

it('rejects mismatched archived responses and refresh payloads', async () => {
  calls.read.mockResolvedValueOnce({ ...envelope('another-plan'), limitations: ['其他计划的限制'] })
  await render(frozenReport())
  expect(host.textContent).toContain('返回的计划不匹配')
  expect(host.textContent).not.toContain('其他计划的限制')
  expect(host.textContent).toContain('恐慌修复假设')
  await click('重试读取模型验证')
  calls.evaluate.mockResolvedValueOnce({ ...envelope('another-plan'), limitations: ['错误刷新内容'] })
  await click('更新模型验证')
  expect(host.textContent).toContain('模型验证更新失败')
  expect(host.textContent).not.toContain('错误刷新内容')
})

it('bounds the initial sector list and mounts older feedback only when expanded', async () => {
  const frozen = frozenReport()
  frozen.ecology!.sectors = Array.from({ length: 21 }, (_, index) => ({ ...frozen.ecology!.sectors[0], name: `板块 ${index + 1}` }))
  const data = envelope()
  const observation = (summary: string, observed_at: string, name: string): MarketGameModelEnvelope['feedback'][number] => ({ version: '1', hypothesis_id: 'hyp-one', shadow_only: true, entry_authorized: false, scope: 'frozen_plan_sample', observed_at, status: 'available', summary, rows: [{ symbol: '600001.SH', name, observed_at, status: 'pending', price_direction: 'flat', feedback_type: 'unclear', stage: 'unconfirmed', label: '待观察', evidence: [], reason: '等待承接' }], limitations: [] })
  data.feedback = [observation('最近一条反馈', '2026-10-09T10:00:00+08:00', '最近样本公司'), observation('此前反馈记录', '2026-10-09T09:59:00+08:00', '此前样本公司')]
  calls.read.mockResolvedValue(data); await render(frozen)
  expect(host.querySelectorAll('section[aria-label="板块竞争"] tbody tr')).toHaveLength(20)
  expect([...host.querySelectorAll('section[aria-label="板块竞争"] tbody td:first-child')].map(item => item.firstChild?.textContent)).not.toContain('板块 21')
  expect(host.textContent).toContain('最近样本公司'); expect(host.textContent).not.toContain('此前样本公司')
  await click('再显示 20 个板块')
  expect(host.querySelectorAll('section[aria-label="板块竞争"] tbody tr')).toHaveLength(21)
  const older = [...host.querySelectorAll('summary')].find(item => item.textContent?.includes('此前反馈记录'))!
  await act(async () => older.click()); await settle()
  expect(host.textContent).toContain('此前样本公司')
})
