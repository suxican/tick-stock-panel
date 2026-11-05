// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { HoldingAnalysis } from './HoldingAnalysis'
import type { HoldingAnalysisInterpretation, HoldingAnalysisOverview, HoldingAnalysisProfile, HoldingAnalysisReport, HoldingAnalysisRow, HoldingAnalysisSavedPlan } from '@/lib/api'

const mocks = vi.hoisted(() => ({ overview: vi.fn(), analyze: vi.fn(), profile: vi.fn(), adopt: vi.fn(), invalidate: vi.fn(), alert: vi.fn(), interpret: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  holdingAnalysisOverview: (...args: unknown[]) => mocks.overview(...args),
  holdingAnalysisAnalyze: (...args: unknown[]) => mocks.analyze(...args),
  holdingAnalysisProfileSave: (...args: unknown[]) => mocks.profile(...args),
  holdingAnalysisAdopt: (...args: unknown[]) => mocks.adopt(...args),
  holdingAnalysisInvalidate: (...args: unknown[]) => mocks.invalidate(...args),
  holdingAnalysisAlert: (...args: unknown[]) => mocks.alert(...args),
  holdingAnalysisInterpret: (...args: unknown[]) => mocks.interpret(...args),
} }))

const profile: HoldingAnalysisProfile = { opened_on: '2026-11-04', horizon: 'short', thesis: '', invalidation: '', max_loss_pct: null, deep_loss_pct: 15, new_position_days: 3, max_holding_days: 5, primary_sector_id: '' }
const row: HoldingAnalysisRow = {
  key: '600000.SH', symbol: '600000.SH', name: '测试持仓', asset_type: 'stock', qty: 1000, cost_price: 12, cost_method: 'manual_reference', opened_on: '2026-11-04', profile,
  pnl_ratio: -.2, age_days: 1, age_state: 'new', pnl_state: 'deep_loss', trend: 'down', relative_strength: 'weaker',
  price: { value: 9.6, date: '2026-11-05', market_at: '2026-11-05T10:00:00+08:00', source: 'daily', fresh: false },
  technicals: { support: 9, resistance: 10, ma20: 10.1, ma60: null, atr14: .3, return5: -.05, return20: null },
  sectors: [{ key: 'industry:test', name: '测试行业', kind: 'industry', source_id: 'test', member_count: 10, valid_count: 5, coverage_ratio: .5, change_ratio: null, valid: false }],
  evidence: ['价格跌破20日均线'], warnings: ['板块覆盖不足'], constraints: { sellable_qty: null, active_t_plan: false, can_size: false },
  plan: { summary: '先核对持仓并观察风险条件', priority: 'risk', expires_on: '2099-12-31', can_adopt: true, scenarios: [
    { key: 'base', title: '基准情景', action: '观察', conditions: ['保持在支撑与压力之间'], price_trigger: null, invalidation: '有效跌破支撑' },
    { key: 'bull', title: '转强情景', action: '复核持有条件', conditions: ['板块恢复'], price_trigger: { op: '>=', value: 10 }, invalidation: '不能站稳压力位' },
    { key: 'bear', title: '转弱情景', action: '复核减仓条件', conditions: ['个股跌破支撑'], price_trigger: { op: '<=', value: 9 }, invalidation: '重新站回支撑' },
  ] },
}
const report: HoldingAnalysisReport = { id: 'report-a', profile_version: '1', source: 'lots', account_id: '', accounts: [], input_version: 'input-1', generated_at: '2026-11-05T10:01:00+08:00', as_of_date: '2026-11-05', rows: [row], warnings: [] }
const savedPlan: HoldingAnalysisSavedPlan = { id: 'plan-a', version: 1, status: 'adopted', created_at: '2026-11-05T10:02:00+08:00', expires_on: '2099-12-31', report_id: report.id, row, input_version: 'input-1', needs_review: false, alert_rule_ids: [] }
const interpretation: HoldingAnalysisInterpretation = { report_id: report.id, generated_at: '2026-11-05T10:03:00+08:00', interpretation: { summary: 'AI归纳：先复核当前风险证据', observations: [{ key: row.key, text: 'AI解释：趋势证据需要关注', evidence_indices: [0] }], limitations: ['只解释报告证据，不补充实时信息'] } }
const overview = (patch: Partial<HoldingAnalysisOverview> = {}): HoldingAnalysisOverview => ({ source: 'lots', account_id: '', accounts: [{ id: 'a', name: '账户甲' }, { id: 'b', name: '账户乙' }], input_version: 'input-1', positions: [{ key: row.key, symbol: row.symbol, name: row.name, qty: row.qty, cost_price: row.cost_price, opened_on: row.opened_on }], profiles: { [row.key]: { profile, version: 2 } }, report: null, plans: [], warnings: [], ...patch })

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(mock => mock.mockReset())
  mocks.overview.mockImplementation((source: string, account_id: string) => Promise.resolve(overview({ source: source as 'lots' | 'position_t', account_id, ...(source === 'position_t' && !account_id ? { positions: [], profiles: {} } : {}) })))
  mocks.analyze.mockResolvedValue(report)
  mocks.profile.mockResolvedValue({ profile, version: 3 })
  mocks.adopt.mockResolvedValue({ plan: savedPlan })
  mocks.invalidate.mockResolvedValue({ plan: { ...savedPlan, status: 'invalidated', version: 2 } })
  mocks.alert.mockResolvedValue({ plan: { ...savedPlan, alert_rule_ids: ['rule-a'], alert_bindings: { 'rule-a': 'active' }, version: 2 }, rule_id: 'rule-a' })
  mocks.interpret.mockResolvedValue(interpretation)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity }, mutations: { retry: false } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render() { await act(async () => root.render(<QueryClientProvider client={client}><HoldingAnalysis /></QueryClientProvider>)); await settle() }
async function click(label: string) { const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === label); expect(button, label).toBeDefined(); await act(async () => button!.click()); await settle() }
async function select(label: string, value: string) { const el = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!; expect(el).toBeTruthy(); await act(async () => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })) }); await settle() }

it('loads source records without generating analysis and explains batch reference semantics', async () => {
  await render()
  expect(mocks.overview).toHaveBeenCalledWith('lots', '')
  expect(mocks.analyze).not.toHaveBeenCalled()
  expect(host.textContent).toContain('批次参考')
  expect(host.textContent).toContain('不代表可卖库存')
  expect(host.textContent).toContain('测试持仓')
})

it('generates on explicit action and keeps new-position and deep-loss labels together', async () => {
  await render(); await click('生成分析')
  expect(mocks.analyze).toHaveBeenCalledWith({ source: 'lots', account_id: '' })
  expect(host.textContent).toContain('刚建仓')
  expect(host.textContent).toContain('深度浮亏')
  expect(host.textContent).toContain('参考成本价差')
  expect(host.textContent).toContain('-20.00%')
  expect(host.textContent).toContain('2026-11-05')
  expect(host.textContent).toContain('5 / 10')
  expect(host.textContent).toContain('覆盖不足')
  expect(host.textContent).toContain('基准情景')
  expect(host.textContent).toContain('转强情景')
  expect(host.textContent).toContain('转弱情景')
  expect(host.textContent).toContain('可卖数量未知')
})

it('adopts a frozen report row without creating an execution or T plan', async () => {
  await render(); await click('生成分析'); await click('采纳研究计划')
  expect(mocks.adopt).toHaveBeenCalledWith({ report_id: 'report-a', row_key: row.key, expected_input_version: 'input-1' })
  expect(host.textContent).toContain('研究计划已采纳')
})

it('refreshes the input snapshot after analysis so a newly generated report can be adopted', async () => {
  await render()
  mocks.analyze.mockResolvedValue({ ...report, input_version: 'input-2' })
  mocks.overview.mockResolvedValue(overview({ input_version: 'input-2' }))
  await click('生成分析')
  expect(mocks.overview).toHaveBeenCalledTimes(2)
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')?.disabled).toBe(false)
})

it('saves explicit profile versions and preserves an empty loss budget as unknown', async () => {
  await render(); await click('编辑研究设置')
  const thesis = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="买入理由"]')!
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(thesis, '跟踪板块修复'); thesis.dispatchEvent(new Event('input', { bubbles: true })) })
  await click('保存研究设置')
  expect(mocks.profile).toHaveBeenCalledWith(row.key, { source: 'lots', account_id: '', expected_version: 2, profile: expect.objectContaining({ thesis: '跟踪板块修复', max_loss_pct: null }) })
  expect(host.textContent).toContain('设置已保存，请重新生成分析')
})

it('does not carry a late analysis response into another source or account', async () => {
  let resolve!: (value: HoldingAnalysisReport) => void
  mocks.analyze.mockReturnValue(new Promise<HoldingAnalysisReport>(done => { resolve = done }))
  await render(); await click('生成分析'); await select('分析来源', 'position_t')
  expect(host.textContent).toContain('请选择账户')
  expect(host.textContent).not.toContain('深度浮亏')
  await select('分析账户', 'b')
  await act(async () => resolve(report)); await settle()
  expect(host.textContent).not.toContain('深度浮亏')
  expect(host.textContent).not.toContain('先核对持仓并观察风险条件')
})

it('does not display previous account data while the next account is loading', async () => {
  await render(); await select('分析来源', 'position_t'); await select('分析账户', 'a')
  mocks.overview.mockImplementation(() => new Promise(() => {}))
  await select('分析账户', 'b')
  expect(host.textContent).not.toContain('测试持仓')
  expect(host.textContent).toContain('正在读取持仓与研究计划')
})

it('shows useful empty and read failure states without inventing analysis', async () => {
  mocks.overview.mockResolvedValue(overview({ positions: [], profiles: {} }))
  await render()
  expect(host.textContent).toContain('暂无可分析记录')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '生成分析')?.disabled).toBe(true)
  mocks.overview.mockRejectedValue(new Error('持仓读取失败，请重试'))
  await act(async () => { await client.invalidateQueries() }); await settle()
  expect(host.textContent).toContain('持仓读取失败，请重试')
  expect(host.textContent).toContain('重新读取')
})

it('keeps the last report visible when regeneration fails and disables stale adoption', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  await render()
  mocks.analyze.mockRejectedValue(new Error('行情暂不可用'))
  await click('重新生成分析')
  expect(host.textContent).toContain('行情暂不可用')
  expect(host.textContent).toContain('深度浮亏')
  const adopt = [...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')!
  expect(adopt.disabled).toBe(true)
})

it('disables adoption when inputs changed or the report plan expired', async () => {
  mocks.overview.mockResolvedValue(overview({ input_version: 'input-2', report }))
  await render()
  expect(host.textContent).toContain('请重新生成后采纳')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')?.disabled).toBe(true)
  mocks.overview.mockResolvedValue(overview({ report: { ...report, rows: [{ ...row, plan: { ...row.plan, expires_on: '2000-01-01' } }] } }))
  await act(async () => { await client.invalidateQueries() }); await settle()
  expect(host.textContent).toContain('研究计划已过期')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')?.disabled).toBe(true)
})

it('renders unknown reference quantity and cost without treating missing values as zero', async () => {
  mocks.overview.mockResolvedValue(overview({ positions: [{ key: row.key, symbol: row.symbol, name: row.name, qty: null, cost_price: null, opened_on: null }], profiles: {} }))
  await render()
  expect(host.textContent).toContain('参考数量 未知')
  expect(host.textContent).toContain('参考成本 未知')
  expect(host.textContent).toContain('建仓日 未知')
})

it('keeps missing-profile reasons and a way to edit beside disabled adoption', async () => {
  mocks.overview.mockResolvedValue(overview({ report: { ...report, rows: [{ ...row, plan: { ...row.plan, can_adopt: false }, warnings: ['缺少研究失效条件'] }] } }))
  await render()
  expect(host.textContent).toContain('缺少研究失效条件')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')?.disabled).toBe(true)
  await click('完善研究设置')
  expect(host.querySelector('textarea[aria-label="研究失效条件"]')).toBeTruthy()
})

it('binds only the selected price reminder on a current adopted plan', async () => {
  mocks.overview.mockResolvedValue(overview({ plans: [savedPlan] }))
  await render(); await click('绑定转弱价格提醒')
  expect(mocks.alert).toHaveBeenCalledWith('plan-a', { expected_version: 1, scenario_key: 'bear' })
  expect(host.textContent).toContain('仅价格提醒')
  expect(host.textContent).toContain('不会下单')
  expect(host.textContent).toContain('价格提醒仅当日有效')
  expect(host.textContent).toContain('跨日、持仓/设置变化或行情过期时暂停')
})

it('counts only confirmed active bindings and exposes pending and unverified reminder states', async () => {
  mocks.overview.mockResolvedValue(overview({ plans: [{ ...savedPlan, alert_rule_ids: ['active-rule', 'pending-rule', 'legacy-rule'], alert_bindings: { 'active-rule': 'active', 'pending-rule': 'pending', 'unrelated-rule': 'active' } }] }))
  await render()
  expect(host.textContent).toContain('已绑定 1 条价格提醒')
  expect(host.textContent).not.toContain('已绑定 3 条价格提醒')
  expect(host.textContent).toContain('有提醒待重新绑定')
  expect(host.textContent).toContain('有提醒状态待核验')
})

it('treats legacy rules without binding metadata as unverified rather than active', async () => {
  mocks.overview.mockResolvedValue(overview({ plans: [{ ...savedPlan, alert_rule_ids: ['legacy-rule'] }] }))
  await render()
  expect(host.textContent).toContain('已绑定 0 条价格提醒')
  expect(host.textContent).toContain('有提醒状态待核验')
  expect(host.textContent).not.toContain('已绑定 1 条价格提醒')
})

it.each([
  { ...savedPlan, status: 'invalidated' as const },
  { ...savedPlan, expires_on: '2000-01-01' },
  { ...savedPlan, needs_review: true },
  { ...savedPlan, input_version: 'old-input' },
])('disables reminders for invalid, expired, or changed-input plans', async plan => {
  mocks.overview.mockResolvedValue(overview({ plans: [plan] }))
  await render()
  const reminders = [...host.querySelectorAll('button')].filter(button => button.textContent?.includes('价格提醒'))
  expect(reminders.length).toBe(2)
  expect(reminders.every(button => button.disabled)).toBe(true)
  expect(mocks.alert).not.toHaveBeenCalled()
})

it('invalidates saved plans with the expected version', async () => {
  mocks.overview.mockResolvedValue(overview({ plans: [{ ...savedPlan, input_version: 'old-input' }] }))
  await render(); await click('作废研究计划')
  expect(mocks.invalidate).toHaveBeenCalledWith('plan-a', { expected_version: 1, status: 'invalidated' })
})

it('interprets only on request and shows the original evidence cited by each observation', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  await render()
  expect(mocks.interpret).not.toHaveBeenCalled()
  expect(host.textContent).toContain('仅解释程序证据与计划')
  await click('AI 解读依据')
  expect(mocks.interpret).toHaveBeenCalledWith('report-a')
  const explanation = host.querySelector('[aria-label="AI 依据解读"]')!
  expect(explanation.textContent).toContain(interpretation.interpretation.summary)
  expect(explanation.textContent).toContain('AI解释：趋势证据需要关注')
  expect(explanation.textContent).toContain('价格跌破20日均线')
  expect(explanation.textContent).toContain('只解释报告证据，不补充实时信息')
  expect(host.textContent).toContain('转弱情景')
  expect(mocks.adopt).not.toHaveBeenCalled()
})

it('keeps deterministic analysis and controls available when optional interpretation fails', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  mocks.interpret.mockRejectedValue(new Error('尚未配置可用模型'))
  await render(); await click('AI 解读依据')
  expect(host.textContent).toContain('尚未配置可用模型')
  expect(host.textContent).toContain('刚建仓')
  expect(host.textContent).toContain('转弱情景')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '采纳研究计划')?.disabled).toBe(false)
})

it('does not display observations with unresolvable evidence and deduplicates valid references', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  mocks.interpret.mockResolvedValue({ ...interpretation, interpretation: { ...interpretation.interpretation, observations: [
    { key: row.key, text: '有可核验引用的解释', evidence_indices: [0, 0, -1, 999] },
    { key: row.key, text: '只有无效引用的解释', evidence_indices: [999] },
    { key: 'unknown-stock', text: '其他股票的解释', evidence_indices: [0] },
  ] } })
  await render(); await click('AI 解读依据')
  const explanation = host.querySelector('[aria-label="AI 依据解读"]')!
  expect(explanation.textContent).toContain('有可核验引用的解释')
  expect(explanation.textContent).not.toContain('只有无效引用的解释')
  expect(explanation.textContent).not.toContain('其他股票的解释')
  expect([...explanation.querySelectorAll('li')].filter(item => item.textContent === row.evidence[0])).toHaveLength(1)
})

it('does not interpret a stale report', async () => {
  mocks.overview.mockResolvedValue(overview({ report, input_version: 'changed-input' }))
  await render()
  expect([...host.querySelectorAll('button')].find(button => button.textContent === 'AI 解读依据')?.disabled).toBe(true)
  expect(mocks.interpret).not.toHaveBeenCalled()
})

it('discards a late interpretation when the report changes', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  let resolve!: (value: HoldingAnalysisInterpretation) => void
  mocks.interpret.mockReturnValue(new Promise<HoldingAnalysisInterpretation>(done => { resolve = done }))
  await render(); await click('AI 解读依据')
  expect(host.textContent).toContain('正在解读…')
  mocks.analyze.mockResolvedValue({ ...report, id: 'report-b' })
  await click('重新生成分析')
  await act(async () => resolve(interpretation)); await settle()
  expect(host.textContent).not.toContain(interpretation.interpretation.summary)
  expect(host.textContent).not.toContain('AI解释：趋势证据需要关注')
  expect(host.querySelector('[aria-label="AI 依据解读"]')).toBeNull()
})

it('discards a late interpretation after changing source and account', async () => {
  mocks.overview.mockImplementation((source: string, account_id: string) => Promise.resolve(overview({ source: source as 'lots' | 'position_t', account_id, report: source === 'lots' ? report : null })))
  let resolve!: (value: HoldingAnalysisInterpretation) => void
  mocks.interpret.mockReturnValue(new Promise<HoldingAnalysisInterpretation>(done => { resolve = done }))
  await render(); await click('AI 解读依据'); await select('分析来源', 'position_t'); await select('分析账户', 'b')
  await act(async () => resolve(interpretation)); await settle()
  expect(host.textContent).not.toContain(interpretation.interpretation.summary)
  expect(host.querySelector('[aria-label="AI 依据解读"]')).toBeNull()
})

it('renders AI text as text and rejects a response attributed to a different report', async () => {
  mocks.overview.mockResolvedValue(overview({ report }))
  mocks.interpret.mockResolvedValue({ ...interpretation, interpretation: { ...interpretation.interpretation, summary: '<img src=x onerror=alert(1)>' } })
  await render(); await click('AI 解读依据')
  expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
  expect(host.querySelector('[aria-label="AI 依据解读"] img')).toBeNull()
  mocks.interpret.mockResolvedValue({ ...interpretation, report_id: 'other-report' })
  await click('重新解读依据')
  expect(host.textContent).toContain('解读结果与当前报告不一致')
  expect(host.textContent).toContain('转弱情景')
})
