// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MarketGameEvaluation, MarketGameReport } from '@/lib/api'
import { MarketGame } from './MarketGame'
import extension from './extension'

const calls = vi.hoisted(() => ({ analyze: vi.fn(), reports: vi.fn(), report: vi.fn(), explain: vi.fn(), evaluation: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  marketGameAnalyze: calls.analyze, marketGameReports: calls.reports, marketGameReport: calls.report,
  marketGameExplain: calls.explain, marketGameEvaluation: calls.evaluation,
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, triggerInfo }: {
  symbol: string; triggerInfo: { message: string }
}) => <div role="dialog" aria-label="个股详情">{symbol} {triggerInfo.message}</div> }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(mock => mock.mockReset())
  calls.reports.mockResolvedValue({ reports: [] })
  calls.analyze.mockResolvedValue(report())
  calls.report.mockImplementation(async (id: string) => report(id))
  calls.explain.mockResolvedValue({ content: '这是一份可选的解释。' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><MarketGame /></QueryClientProvider>))
  await settle()
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  expect(button, text).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
async function input(label: string, value: string) {
  const field = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  expect(field).not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}
async function select(id: string) {
  const field = host.querySelector<HTMLSelectElement>('select[aria-label="历史计划"]')!
  await act(async () => { field.value = id; field.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
}
function report(id = 'plan-one'): MarketGameReport {
  return {
    id, as_of: '2026-09-30', cutoff: '2026-09-30T15:30:00+08:00', created_at: '2026-09-30T16:00:00+08:00',
    input_version: 'snapshot-1', rule_version: 'v1', quality: 'limited', research_only: false,
    summary: `计划 ${id}：修复仍需确认`, market_state: { trend: '震荡', phase: '修复', emotion: '偏弱', crowding: '适中', change: '较昨日改善' },
    allocation: { min: 0, max: .2, total_cap: .5, single_cap: .15, sector_cap: .25, risk_per_trade: .005, reason: '量价确认后才增加风险暴露。' },
    hypotheses: [{ id: 'panic', title: '恐慌后的承接', status: 'watch', facts: ['下跌家数减少'], interpretation: '抛压可能减弱', alternative: '也可能只是缩量反弹', confirm: ['放量承接'], invalidation: ['再次放量下跌'] }],
    scenarios: [{ horizon: 1, label: '下一交易日', scenario: '承接修复', condition: '站稳参考区间', action: '等待确认', max_position: .2 }],
    sectors: [{ name: '机械设备', avg_return: .0366, breadth: .6, status: '观察', reason: '扩散改善' }],
    candidates: [{ symbol: '600001.SH', name: '示例公司', sector: '机械设备', mode: 'panic_repair', role: '观察', reference_price: 10,
      trigger_low: 10.1, trigger_high: 10.3, invalidation_price: 9.5, max_position: .1, stress_loss_pct: .08, holding_days: 3,
      evidence: ['缩量回撤'], trigger: ['承接确认'], invalidation: ['跌破参考位'], exit_rules: ['最晚第 3 个交易日评估退出'] }],
    evidence: [{ id: 'breadth', label: '上涨占比', value: .6, unit: 'ratio', source: '本地日线', observed_at: '2026-09-30', available_at: null }],
    limitations: ['缺少可核验的散户持仓数据'],
  }
}

it('registers a lazy independent route and matching navigation', () => {
  expect(extension.routes?.[0].path).toBe('/market-game')
  expect(extension.navigation?.[0].routeId).toBe(extension.routes?.[0].id)
  expect(extension.navigation?.[0].label).toBe('超短线博弈')
})

it('starts with an explicit empty state and generates latest post-close using decimal risk units', async () => {
  await render()
  expect(host.textContent).toContain('选择盘后日期，生成第一份条件计划')
  expect(calls.analyze).not.toHaveBeenCalled()
  await click('生成盘后计划')
  expect(calls.analyze).toHaveBeenCalledWith({ risk: { total_cap: .5, single_cap: .15, sector_cap: .25, risk_per_trade: .005, min_amount: 1e8, max_candidates: 5 } })
  expect(host.textContent).toContain('3.66%')
  expect(host.textContent).not.toContain('366%')
  expect(host.textContent).toContain('尚未校准')
  expect(host.textContent).toContain('缺少可核验的散户持仓数据')
  expect(host.textContent).toContain('另一种解释')
})

it('accepts zero total exposure for observation and rejects blank or invalid risk inputs', async () => {
  await render()
  await input('总仓位上限（%）', '0')
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[0][0].risk.total_cap).toBe(0)
  await input('单笔风险预算（%）', '')
  await click('生成盘后计划')
  expect(calls.analyze).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('请填写有效的风险参数')
})

it('clears the selected report on date change and displays analysis failures without stale results', async () => {
  await render()
  await click('生成盘后计划')
  await input('盘后日期', '2026-09-29')
  expect(host.textContent).not.toContain('计划 plan-one：')
  calls.analyze.mockRejectedValueOnce(new Error('该日期缺少日线'))
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[1][0].as_of).toBe('2026-09-29')
  expect(host.textContent).toContain('该日期缺少日线')
  expect(host.textContent).not.toContain('示例公司')
})

it('preserves the viewed plan on regeneration failure and locks controls while generating', async () => {
  await render()
  await click('生成盘后计划')
  let reject!: (error: Error) => void
  calls.analyze.mockReturnValueOnce(new Promise((_resolve, rejectPromise) => { reject = rejectPromise }))
  await click('生成盘后计划')
  expect(host.textContent).toContain('计划 plan-one：')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="盘后日期"]')!.disabled).toBe(true)
  await act(async () => reject(new Error('计算失败')))
  await settle()
  expect(host.textContent).toContain('计划 plan-one：')
  expect(host.textContent).toContain('计算失败')
})

it('marks research-only history and closes current-data preview when switching plans', async () => {
  const historical = { ...report('history-two'), research_only: true, candidates: [], summary: '历史研究计划' }
  calls.reports.mockResolvedValue({ reports: [report(), historical] })
  calls.report.mockImplementation(async (id: string) => id === historical.id ? historical : report())
  await render()
  await select('plan-one')
  await click('示例公司')
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain('当前数据')
  await select('history-two')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).toContain('研究阶段计划')
  expect(host.textContent).toContain('本计划没有可列出的候选')
})

it.each(['', '2026-09-29'])('keeps generation date %s independent from a selected archived report', async (generationDate) => {
  calls.reports.mockResolvedValue({ reports: [report()] })
  await render()
  if (generationDate) await input('盘后日期', generationDate)
  await select('plan-one')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="盘后日期"]')!.value).toBe(generationDate)
  expect(host.textContent).toContain('行情归属 2026-09-30')
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[0][0].as_of).toBe(generationDate || undefined)
})

it('keeps a late AI explanation attached to its original report', async () => {
  calls.reports.mockResolvedValue({ reports: [report(), report('plan-two')] })
  let resolve!: (value: { content: string }) => void
  calls.explain.mockReturnValueOnce(new Promise(done => { resolve = done }))
  await render()
  await select('plan-one')
  await click('生成 AI 解读')
  await select('plan-two')
  await act(async () => resolve({ content: '仅属于旧报告的解释' }))
  await settle()
  expect(host.textContent).not.toContain('仅属于旧报告的解释')
  await select('plan-one')
  expect(host.textContent).toContain('仅属于旧报告的解释')
})

it('distinguishes observed zero returns from pending data without presenting a backtest', async () => {
  const evaluation: MarketGameEvaluation = { report_id: 'plan-one', as_of: '2026-09-30', evaluated_at: '2026-10-09T16:00:00+08:00', kind: 'observation_only', status: 'partial',
    rows: [
      { symbol: '600001.SH', name: '示例公司', horizon: 1, label: '第 1 个观察日', trade_date: '2026-10-09', close_return: 0, state: 'available' },
      { symbol: '600001.SH', name: '示例公司', horizon: 2, label: '第 2 个观察日', trade_date: null, close_return: null, state: 'pending' },
    ], limitations: ['按已观测市场日排序'] }
  calls.evaluation.mockResolvedValue(evaluation)
  await render()
  await click('生成盘后计划')
  expect(calls.evaluation).not.toHaveBeenCalled()
  await click('查看后续表现')
  expect(calls.evaluation).toHaveBeenCalledWith('plan-one')
  const section = host.querySelector('section[aria-label="后续观察"]')!
  expect(section.textContent).toContain('0%')
  expect(section.textContent).toContain('待观察')
  expect(section.textContent).toContain('观察收益不等于触发或实盘收益')
  expect(section.textContent).toContain('按已观测市场日排序')
})

it('shows unavailable inputs with no candidates and disables AI instead of inventing a plan', async () => {
  const unavailable = report()
  unavailable.quality = 'unavailable'
  unavailable.research_only = true
  unavailable.candidates = []
  unavailable.scenarios = []
  unavailable.sectors = []
  unavailable.hypotheses = []
  unavailable.allocation.max = 0
  calls.analyze.mockResolvedValue(unavailable)
  await render()
  await click('生成盘后计划')
  expect(host.textContent).toContain('暂不可分析')
  expect(host.textContent).toContain('数据不足，暂不形成情景计划')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/settings?tab=data-sources')
  const ai = [...host.querySelectorAll('button')].find(item => item.textContent === '生成 AI 解读')!
  expect(ai.disabled).toBe(true)
  expect(calls.explain).not.toHaveBeenCalled()
})

it('never displays the previous report when a newly selected history record fails', async () => {
  calls.reports.mockResolvedValue({ reports: [report(), report('plan-two')] })
  calls.report.mockImplementation(async (id: string) => {
    if (id === 'plan-two') throw new Error('报告不存在')
    return report()
  })
  await render()
  await select('plan-one')
  await select('plan-two')
  expect(host.textContent).toContain('计划读取失败：报告不存在')
  expect(host.textContent).not.toContain('计划 plan-one：')
  expect(host.textContent).not.toContain('示例公司')
})
