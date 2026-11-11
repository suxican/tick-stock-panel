// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MarketGameCalibration, MarketGameEvaluation, MarketGameReport, MarketGamePsychology } from '@/lib/api'
import { MarketGame } from './MarketGame'
import extension from './extension'

const calls = vi.hoisted(() => ({ analyze: vi.fn(), reports: vi.fn(), report: vi.fn(), explain: vi.fn(), evaluation: vi.fn(), capital: vi.fn(), model: vi.fn(), calibration: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  marketGameAnalyze: calls.analyze, marketGameReports: calls.reports, marketGameReport: calls.report,
  marketGameExplain: calls.explain, marketGameEvaluation: calls.evaluation,
  marketGameCapital: calls.capital, marketGameModel: calls.model, marketGameCalibration: calls.calibration,
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, triggerInfo }: {
  symbol: string; triggerInfo: { message: string }
}) => <div role="dialog" aria-label="个股详情">{symbol} {triggerInfo.message}</div> }))
vi.mock('./HistoryChart', () => ({ HistoryChart: ({ option }: { option: unknown }) => <div data-chart>{JSON.stringify(option)}</div> }))

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
  calls.capital.mockRejectedValue(new Error('尚无资金观察'))
  calls.calibration.mockResolvedValue({ status: 'missing' })
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
async function openProfile() {
  const profile = [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent?.startsWith('行为代理画像'))!
  expect(profile.open).toBe(false)
  await act(async () => profile.querySelector('summary')!.click())
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

it('keeps supplemental research out of the primary views and fetches it only when opened', async () => {
  await render()
  await click('生成盘后计划')
  expect([...host.querySelectorAll('[role="tab"]')].map(item => item.textContent)).toEqual(['今日结论', '情绪与假设', '复盘与证据'])
  expect(host.textContent).toContain('短线趋势回踩候选')
  expect(host.querySelector('section[aria-label="海外市场 · 计划冻结证据"]')).toBeNull()
  expect(calls.capital).not.toHaveBeenCalled()
  expect(calls.model).not.toHaveBeenCalled()
  const advanced = [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent === '补充研究')!
  expect(advanced.open).toBe(false)
  await act(async () => advanced.querySelector('summary')!.click())
  await settle()
  expect(calls.capital).not.toHaveBeenCalled()
  await click('资金观察')
  expect(calls.capital).toHaveBeenCalledWith('plan-one')
  await click('外围环境')
  expect(host.querySelector('section[aria-label="海外市场 · 计划冻结证据"]')).not.toBeNull()
  expect(calls.model).not.toHaveBeenCalled()
  calls.analyze.mockResolvedValue(report('plan-two'))
  await click('生成盘后计划')
  const nextAdvanced = [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent === '补充研究')!
  expect(nextAdvanced.open).toBe(false)
  expect(host.querySelector('section[aria-label="海外市场 · 计划冻结证据"]')).toBeNull()
  expect(calls.capital).toHaveBeenCalledTimes(1)
})

it('shows risk vetoes independently of a repairing market phase and handles absent legacy risk fields', async () => {
  const saved = report()
  saved.market_state = { ...saved.market_state, risk_vetoes: ['指数短期回撤，暂停新增仓位'], max_boards: 5, promotion_rate: .3, phase_basis: '当日与前日的广度、连板变化' }
  calls.analyze.mockResolvedValue(saved)
  await render()
  await click('生成盘后计划')
  const risk = host.querySelector('section[aria-label="新增风险限制"]')!
  expect(risk.textContent).toContain('指数短期回撤，暂停新增仓位')
  expect(host.textContent).toContain('修复')
  expect(host.textContent).toContain('最高连板 5 板')
  expect(host.textContent).toContain('晋级率 30%')
  calls.analyze.mockResolvedValue(report('legacy'))
  await click('生成盘后计划')
  expect(host.querySelector('section[aria-label="新增风险限制"]')).toBeNull()
  expect(host.textContent).not.toContain('指数短期回撤，暂停新增仓位')
})

it('starts with an explicit empty state and generates latest post-close using decimal risk units', async () => {
  await render()
  expect(host.textContent).toContain('选择盘后日期，生成第一份条件计划')
  expect(calls.analyze).not.toHaveBeenCalled()
  await click('生成盘后计划')
  expect(calls.analyze).toHaveBeenCalledWith({ risk: { total_cap: .5, single_cap: .15, sector_cap: .25, risk_per_trade: .005, portfolio_risk_budget: .015, min_amount: 1e8, max_candidates: 5 } })
  expect(host.textContent).toContain('3.66%')
  expect(host.textContent).not.toContain('366%')
  expect(host.textContent).toContain('量价确认后才增加风险暴露')
  // limitations text is now inside an InfoTip; verify reason text is present instead
  expect(host.textContent).toContain('量价确认后才增加风险暴露')
  await click('情绪与假设')
  expect(host.textContent).not.toContain('情绪与心理刻度尚未记录')
  await openProfile()
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
  await click('情绪与假设')
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
  await click('复盘与证据')
  await click('查看后续表现')
  expect(calls.evaluation).toHaveBeenCalledWith('plan-one')
  const section = host.querySelector('section[aria-label="后续观察"]')!
  expect(section.textContent).toContain('0%')
  expect(section.textContent).toContain('待观察')
  expect(section.textContent).toContain('基准收益')
  expect(section.textContent).toContain('缺失行情不顺延')
  expect(section.textContent).toContain('缺失行情不顺延')
  expect(section.textContent).toContain('基准收益')
  expect(section.textContent).toContain('超额收益')
  expect(section.textContent).toContain('4% / -2%')
  expect(section.textContent).toContain('4% / -2%')
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
  await click('情绪与假设')
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
  await click('情绪与假设')
  expect(host.querySelector('[data-chart]')).toBeNull()
  await openProfile()
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
  await click('复盘与证据')
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
  expect(host.textContent).toContain('占账户总资产，未扣除已有持仓')
  expect([...host.querySelectorAll('button')].find(item => item.textContent === '载入此报告的风险参数')!.disabled).toBe(true)
  await click('情绪与假设')
  await openProfile()
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
  expect(host.querySelector('[role="tab"][aria-selected="true"]')!.textContent).toContain('情绪与假设')
  expect(document.activeElement?.id).toBe('game-tab-psychology')
  expect(calls.explain).not.toHaveBeenCalled()
  expect(calls.evaluation).not.toHaveBeenCalled()
})

it('collapses generation settings after loading a report while keeping the toolbar usable', async () => {
  await render()
  const settings = () => [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent?.startsWith('生成设置'))!
  expect(settings().open).toBe(true)
  await click('生成盘后计划')
  // 载入报告后收起设置，生成按钮与历史计划仍在工具条上。
  expect(settings().open).toBe(false)
  await act(async () => settings().querySelector('summary')!.click())
  expect(settings().open).toBe(true)
  await input('组合压力预算（%）', '2')
  await click('生成盘后计划')
  expect(calls.analyze.mock.calls[1][0].risk.portfolio_risk_budget).toBe(.02)
  await click('情绪与假设')
  const conclusion = [...host.querySelectorAll('details')].find(item => item.firstElementChild?.textContent === '查看冻结的市场结论')!
  expect(conclusion.open).toBe(false)
  expect(conclusion.textContent).toContain('计划 plan-one：')
})

function sentiment(level: 'veto' | 'caution' | 'none', score: number | null): NonNullable<MarketGameReport['sentiment']> {
  return {
    version: 'sentiment-v1', source: 'zzshare', status: 'ready', as_of: '2026-09-30', fetched_at: '2026-09-30T17:00:00+08:00',
    point_in_time: true, reason: '盘后情绪与亏钱效应门可用', sync: '已增量同步 zzshare 盘后情绪',
    metrics: [
      { id: 'earn_effect', label: '赚钱效应', unit: 'score', value: 50, percentile: 40, sample_size: 252 },
      { id: 'lose_effect', label: '亏钱效应', unit: 'score', value: 14, percentile: 88, sample_size: 252 },
      { id: 'loss_count', label: '面+大面+天地板', unit: 'count', value: 4, percentile: 70, sample_size: 252 },
      { id: 'drop5_share', label: '跌超5%家数占比', unit: 'ratio', value: .0425, percentile: 65, sample_size: 252 },
    ],
    loss_gate: { score, level, components: ['loss_count', 'lose_effect', 'drop5_share'], caution: 60, veto: 80 },
    cross_check: { status: 'matched', reason: '本地涨跌停家数与 zzshare 盘后统计在容差内一致', local_limit_up: 55, vendor_limit_up: 53, local_limit_down: 2, vendor_limit_down: 1 },
    history: [{ date: '2026-09-29', loss_gate: 50, earn_effect: 60, lose_effect: 5 }, { date: '2026-09-30', loss_gate: score, earn_effect: 50, lose_effect: 14 }],
    revisions: 0, limitations: ['赚钱/亏钱效应为供应商模型'],
  }
}

it('shows one frozen sentiment line on the overview and full metrics on the psychology tab', async () => {
  calls.analyze.mockResolvedValue({ ...report(), sentiment: sentiment('veto', 81) })
  await render()
  await click('生成盘后计划')
  const strip = host.querySelector('[aria-label="短线情绪"]')!
  expect(strip.textContent).toContain('亏钱效应')
  expect(strip.textContent).toContain('分位 88')
  expect(strip.textContent).toContain('亏钱门 81 分位：暂停新增')
  expect(strip.textContent).not.toContain('跌超5%家数占比')  // secondary metrics stay off the overview
  await click('情绪与假设')
  const panel = host.querySelector('section[aria-label="短线情绪"]')!
  expect(panel.textContent).toContain('81 分位 · 暂停新增')
  expect(panel.textContent).toContain('4.25%')
  expect(panel.querySelector('[data-chart]')?.textContent).toContain('亏钱门分位')
})

it('keeps legacy reports readable and states unavailable vendor sentiment plainly', async () => {
  await render()
  await click('生成盘后计划')
  expect(host.querySelector('[aria-label="短线情绪"]')).toBeNull()
  await click('情绪与假设')
  expect(host.textContent).toContain('短线情绪未记录')
  const unavailable = { ...sentiment('none', null), status: 'unavailable' as const, reason: 'zzshare 情绪归档未覆盖行情日期', metrics: [], history: [],
    loss_gate: { score: null, level: 'unavailable' as const, components: [], caution: 60, veto: 80 } }
  calls.analyze.mockResolvedValue({ ...report('plan-two'), sentiment: unavailable })
  await click('今日结论')
  await click('生成盘后计划')
  expect(host.querySelector('[aria-label="短线情绪"]')?.textContent).toContain('暂不可用：zzshare 情绪归档未覆盖行情日期')
})

it('labels 1.4.0 candidates as an observation list and the cap as the market gate', async () => {
  const saved = report()
  calls.analyze.mockResolvedValue({ ...saved, rule_version: '1.4.0', allocation: { ...saved.allocation, max: 0 },
    candidates: saved.candidates.map(item => ({ ...item, max_position: 0 })) })
  await render()
  await click('生成盘后计划')
  expect(host.textContent).toContain('候选观察名单')
  expect(host.textContent).not.toContain('短线趋势回踩候选')
  expect(host.textContent).toContain('市场闸门上限')
  expect(host.textContent).toContain('不分配')
  // 观察模式不再逐股、逐情景重复“不分配”的仓位列。
  const list = host.querySelector('section[aria-label="候选观察名单"]')!
  expect(list.textContent).not.toContain('单股上限')
  expect(list.textContent).not.toContain('本计划仓位上限')
  expect(host.querySelector('section[aria-label="结论依据"]')?.textContent).toContain('选股层回放未通过')
  await click('查看假设')
  expect(host.querySelector('[role="tab"][aria-selected="true"]')!.textContent).toContain('情绪与假设')
})

function calibration(): MarketGameCalibration {
  const segment = (n: number, value: number | null) => ({ n, mean: value, control_n: 400, control_mean: 0, diff: value })
  return {
    schema_version: 1, status: 'ready', metric: 'ew_1', min_days: 30, scope: 'in_sample_market_proxy', rule_version: '1.4.0',
    start: '2020-01-02', end: '2026-11-06', split: '2023-01-01', sentiment_days: 1500, created_at: '2026-11-11T14:25:27+08:00',
    archive: 'cal_20261111T142527_1a66e965.json', skipped_archives: 0, limitations: ['样本内研究'],
    rows: [
      { group: 'gate:veto', control: 'gate:none', label: '亏钱门 · 暂停新增', kind: 'rule', train: segment(102, -.0027), test: segment(128, -.0011), verdict: 'consistent' },
      { group: 'risk:short_index_risk', control: 'baseline', label: '指数短期风险否决', kind: 'rule', train: segment(135, -.0007), test: segment(158, .0003), verdict: 'inconsistent' },
      { group: 'shadow:volume_stall', control: 'shadow:volume_advance', label: '放量滞涨', kind: 'shadow', card: 'C1-4 浮动供给雪球', definition: '趋势上行且放量滞涨', train: segment(5, null), test: segment(8, null), verdict: 'insufficient' },
    ],
  }
}

it('shows archived credibility on the evidence tab and drives the basis tags', async () => {
  calls.calibration.mockResolvedValue(calibration())
  await render()
  await click('生成盘后计划')
  const basis = host.querySelector('section[aria-label="结论依据"]')!
  expect(basis.textContent).toContain('样本内方向一致')
  expect(basis.textContent).toContain('样本内方向不一致')
  await click('复盘与证据')
  const panel = host.querySelector('section[aria-label="规则可信度"]')!
  expect(panel.textContent).toContain('2020-01-02 至 2026-11-06 · 分段 2023-01-01')
  expect(panel.textContent).toContain('-0.27% → -0.11%')
  expect(panel.textContent).toContain('102/400 · 128/400')
  expect(panel.textContent).toContain('影子检验 · 不参与规则')
  expect(panel.textContent).toContain('C1-4 浮动供给雪球')
  expect(panel.textContent).toContain('— → —')  // missing differences are never shown as zero
  expect(calls.calibration).toHaveBeenCalledTimes(1)  // overview and evidence share one cached read
})

it('explains a missing archive and isolates calibration errors from the plan', async () => {
  await render()
  await click('生成盘后计划')
  expect(host.querySelector('section[aria-label="结论依据"]')!.textContent).toContain('未校准')
  await click('复盘与证据')
  const panel = () => host.querySelector('section[aria-label="规则可信度"]')!
  expect(panel().textContent).toContain('尚无校准归档')
  expect(panel().textContent).toContain('--archive')
  client.removeQueries({ queryKey: ['market-game-calibration'] })
  calls.calibration.mockRejectedValue(new Error('boom'))
  await click('今日结论')
  await click('复盘与证据')
  expect(panel().textContent).toContain('校准归档读取失败')
  expect(host.textContent).toContain('输入版本 snapshot-1')
})
