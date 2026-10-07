// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MarketGameEvaluation, MarketGameReport, MarketGamePsychology } from '@/lib/api'
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
vi.mock('echarts-for-react', () => ({ default: ({ option }: { option: unknown }) => <div data-chart>{JSON.stringify(option)}</div> }))

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
async function reveal(element: HTMLElement) {
  const disclosures: HTMLDetailsElement[] = []
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLDetailsElement && !parent.open) disclosures.unshift(parent)
  }
  await act(async () => disclosures.forEach(details => details.querySelector('summary')!.click()))
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  expect(button, text).toBeDefined()
  await reveal(button!)
  await act(async () => button!.click())
  await settle()
}
async function input(label: string, value: string) {
  const field = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!
  expect(field).not.toBeNull()
  await reveal(field)
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}
async function select(id: string) {
  const field = host.querySelector<HTMLSelectElement>('select[aria-label="历史计划"]')!
  await reveal(field)
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
  expect(calls.analyze).toHaveBeenCalledWith({ risk: { total_cap: .5, single_cap: .15, sector_cap: .25, risk_per_trade: .005, portfolio_risk_budget: .015, min_amount: 1e8, max_candidates: 5 } })
  expect(host.textContent).toContain('3.66%')
  expect(host.textContent).not.toContain('366%')
  expect(host.textContent).toContain('尚未校准')
  expect(host.textContent).toContain('缺少可核验的散户持仓数据')
  await click('情绪与心理')
  expect(host.textContent).toContain('情绪与心理刻度尚未记录')
  expect(host.textContent).toContain('另一种解释')
})

it('accepts zero total exposure for observation and rejects blank or invalid risk inputs', async () => {
  await render()
  await input('本计划新增仓位上限（%）', '0')
  await input('组合压力预算（%）', '0')
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[0][0].risk.total_cap).toBe(0)
  expect(calls.analyze.mock.calls[0][0].risk.portfolio_risk_budget).toBe(0)
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
  await click('情绪与心理')
  await click('生成 AI 解读')
  await select('plan-two')
  await act(async () => resolve({ content: '仅属于旧报告的解释' }))
  await settle()
  expect(host.textContent).not.toContain('仅属于旧报告的解释')
  await select('plan-one')
  expect(host.textContent).toContain('仅属于旧报告的解释')
})

it('distinguishes observed zero returns from pending data without presenting a backtest', async () => {
  const evaluation: MarketGameEvaluation = { report_id: 'plan-one', as_of: '2026-09-30', evaluated_at: '2026-10-09T16:00:00+08:00', kind: 'observation_only', status: 'partial', calendar_verified: true, plan_status: 'expired',
    rows: [
      { symbol: '600001.SH', name: '示例公司', horizon: 1, label: '第 1 个观察日', trade_date: '2026-10-09', close_return: 0, state: 'available', benchmark_return: .01, excess_return: -.01, max_upside: .04, max_drawdown: -.02 },
      { symbol: '600001.SH', name: '示例公司', horizon: 2, label: '第 2 个观察日', trade_date: null, close_return: null, state: 'pending' },
    ], limitations: ['按已观测市场日排序'] }
  calls.evaluation.mockResolvedValue(evaluation)
  await render()
  await click('生成盘后计划')
  expect(calls.evaluation).not.toHaveBeenCalled()
  await click('证据与复盘')
  await click('查看后续表现')
  expect(calls.evaluation).toHaveBeenCalledWith('plan-one')
  const section = host.querySelector('section[aria-label="后续观察"]')!
  expect(section.textContent).toContain('0%')
  expect(section.textContent).toContain('待观察')
  expect(section.textContent).toContain('观察收益不等于触发或实盘收益')
  expect(section.textContent).toContain('按已观测市场日排序')
  expect(section.textContent).toContain('缺失行情不顺延')
  expect(section.textContent).toContain('基准收益')
  expect(section.textContent).toContain('超额收益')
  expect(section.textContent).toContain('4% / -2%')
  expect(section.textContent).toContain('不是入场后的收益或回撤')
  const cells = section.querySelectorAll('tbody tr')[0].querySelectorAll('td')
  expect(cells[4].textContent).toBe('1%')
  expect(cells[5].textContent).toBe('-1%')
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
  await click('情绪与心理')
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

function psychology(): MarketGamePsychology {
  const dimensions = ([['risk_appetite', '风险偏好'], ['panic_pressure', '恐慌压力'], ['profit_pressure', '获利了结压力'], ['repair_support', '修复承接']] as const).map(([id, label], index) => ({
    id, label, score: index === 1 ? null : 60, delta: index === 1 ? null : 0, level: index === 1 ? '无法判断' : '中等', sample_size: 200, coverage: .8,
    metrics: [{ id: 'breadth', label: '上涨占比', value: .6, unit: 'ratio', percentile: 75 }, { id: 'premium', label: '昨日涨停股溢价', value: .012, unit: 'return', percentile: 65 }],
    facts: [`${label}的观察事实`], interpretation: `${label}的行为解释`, alternative: '也可能来自指数权重变化', confirm: ['继续观察承接'], invalidation: ['市场广度再次恶化'], limitations: [],
  }))
  return { version: 'psych-1', scope: 'market_proxy', summary: '基于可核验市场行为的心理代理。', dimensions,
    history: [{ date: '2026-09-29', risk_appetite: 55, panic_pressure: null, profit_pressure: 50, repair_support: 40 }, { date: '2026-09-30', risk_appetite: 60, panic_pressure: null, profit_pressure: 60, repair_support: 60 }], limitations: ['不识别真实散户身份'] }
}

it('shows real psychology series with null gaps, exact percentile units and switchable explanations', async () => {
  calls.analyze.mockResolvedValue({ ...report(), psychology: psychology() })
  await render()
  await click('生成盘后计划')
  expect(host.querySelector('[data-chart]')).toBeNull()
  await click('情绪与心理')
  expect(host.textContent).toContain('较前日 0.0 分')
  expect(host.textContent).toContain('覆盖 80%')
  expect(host.textContent).toContain('分位 75%')
  expect(host.textContent).toContain('1.20%')
  expect(host.textContent).not.toContain('7500%')
  const option = JSON.parse(host.querySelector('[data-chart]')!.textContent!)
  expect(option.series[0].data).toEqual([55, 60])
  expect(option.series[1].data).toEqual([null, null])
  expect(option.series[1].connectNulls).toBe(false)
  const panic = host.querySelector('[aria-label="恐慌压力评分"]')!
  expect(panic.getAttribute('aria-valuenow')).toBeNull()
  expect(panic.getAttribute('aria-valuetext')).toContain('无法评分')
  await act(async () => (panic.closest('button') as HTMLButtonElement).click())
  expect(host.querySelector('section[aria-label="恐慌压力解释"]')?.textContent).toContain('恐慌压力的观察事实')
  await click('证据与复盘')
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(host.textContent).toContain('输入版本 snapshot-1')
})

it('restores only explicit archived parameters and does not rewrite the report or analysis date', async () => {
  const original = { total_cap: .3, single_cap: .1, sector_cap: .2, risk_per_trade: .003, portfolio_risk_budget: .01, min_amount: 2e8, max_candidates: 3 }
  calls.reports.mockResolvedValue({ reports: [report()] })
  calls.report.mockResolvedValue({ ...report(), requested_risk: original })
  await render()
  await input('盘后日期', '2026-09-29')
  await select('plan-one')
  const total = () => host.querySelector<HTMLInputElement>('input[aria-label="本计划新增仓位上限（%）"]')!.value
  expect(total()).toBe('50')
  await click('载入此报告的风险参数')
  expect(total()).toBe('30')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="盘后日期"]')!.value).toBe('2026-09-29')
  expect(host.textContent).toContain('行情归属 2026-09-30')
  expect(host.textContent).toContain('不等于重放原快照')
  await click('生成盘后计划')
  expect(calls.analyze).toHaveBeenCalledWith({ as_of: '2026-09-29', risk: original })
})

it('keeps old reports readable without assuming risk parameters or a valid execution window', async () => {
  calls.analyze.mockResolvedValue({ ...report(), psychology: null, validity: null, requested_risk: null })
  await render()
  await click('生成盘后计划')
  expect(host.textContent).toContain('有效交易日待核验')
  expect(host.textContent).toContain('未扣除已有持仓')
  expect([...host.querySelectorAll('button')].find(item => item.textContent === '载入此报告的风险参数')!.disabled).toBe(true)
  await click('情绪与心理')
  expect(host.textContent).toContain('情绪与心理刻度尚未记录')
  expect(host.querySelector('[data-chart]')).toBeNull()
})

it('marks an expired plan without altering frozen candidate amounts or claiming live verification', async () => {
  const saved = report()
  saved.validity = { calendar_verified: true, status: 'scheduled', entry_session: '2000-01-04', observation_sessions: ['2000-01-04', '2000-01-05', '2000-01-06'], expires_at: '2000-01-06T15:00:00+08:00', reason: '日历已核验' }
  saved.candidates[0].conditions = [{ id: 'price', phase: 'entry', scope: 'stock', metric: 'price', operator: 'between', value: 10.1, upper: 10.3, window: 'quote', minimum_samples: 1, description: '报价位于确认区间' }, { id: 'stop', phase: 'cancel', scope: 'stock', metric: 'price', operator: '<', value: 9.5, upper: null, window: 'quote', minimum_samples: 1, description: '报价低于结构参考位' }]
  calls.analyze.mockResolvedValue(saved)
  await render()
  await click('生成盘后计划')
  expect(host.textContent).toContain('观察计划已到期')
  expect(host.textContent).toContain('入场窗口已结束')
  expect(host.textContent).toContain('入场：全部满足')
  expect(host.textContent).toContain('取消：任一成立')
  expect(host.textContent).toContain('尚未接入实时核验')
  expect(saved.candidates[0].max_position).toBe(.1)
  expect(saved.validity.status).toBe('scheduled')
})

it('supports keyboard tab navigation without starting explanation or evaluation requests', async () => {
  await render()
  await click('生成盘后计划')
  const tab = host.querySelector('[role="tab"][aria-selected="true"]')!
  await act(async () => tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })))
  expect(host.querySelector('[role="tab"][aria-selected="true"]')!.textContent).toContain('情绪与心理')
  expect(document.activeElement?.id).toBe('game-tab-psychology')
  expect(calls.explain).not.toHaveBeenCalled()
  expect(calls.evaluation).not.toHaveBeenCalled()
})

it('keeps the generation toolbar available after loading a report and allows manual collapse', async () => {
  await render()
  const settings = () => [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent?.startsWith('生成设置'))!
  expect(settings().open).toBe(true)
  await click('生成盘后计划')
  expect(settings().open).toBe(true)
  await act(async () => settings().querySelector('summary')!.click())
  expect(settings().open).toBe(false)
  await act(async () => settings().querySelector('summary')!.click())
  expect(settings().open).toBe(true)
  await input('组合压力预算（%）', '2')
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[1][0].risk.portfolio_risk_budget).toBe(.02)
  await click('情绪与心理')
  const conclusion = [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent === '查看冻结的市场结论')!
  expect(conclusion.open).toBe(false)
  expect(conclusion.textContent).toContain('计划 plan-one：')
})
