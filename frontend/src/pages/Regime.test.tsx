// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type EnvConclusion, type RegimeRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Regime } from './Regime'

const fixtures = vi.hoisted(() => ({
  charts: [] as { dom: HTMLElement; option: any; disposed: boolean; resize: ReturnType<typeof vi.fn>; setOption: ReturnType<typeof vi.fn> }[],
  resizes: [] as (() => void)[],
}))

vi.mock('echarts', () => ({
  init: (dom: HTMLElement) => {
    const instance = {
      dom, option: null as any, disposed: false,
      resize: vi.fn(),
      setOption: vi.fn((option: any) => {
        instance.option = { ...option, legend: { ...option.legend, selected: { ...option.legend?.selected } } }
      }),
      dispose: () => { instance.disposed = true },
      isDisposed: () => instance.disposed,
      getDom: () => dom,
      getZr: () => instance.disposed ? null : { on: vi.fn(), off: vi.fn() },
    }
    fixtures.charts.push(instance)
    return instance
  },
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const rows: RegimeRow[] = ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30'].map((date, index) => ({
  date, state: index < 2 ? 'strong' : 'weak', score: index < 2 ? 80 : 6,
  limit_up: 47, limit_down: 10, broken_limit: 12, max_consecutive: 5, seal_rate: .7,
  up_count: 1000, down_count: 4000, up_ratio: .2, index_pct: -.01,
  above_ma20_pct: .3, total_amount: 100000, avg_turnover: .02,
  profit_score: 0, speculation_score: 17, resilience_score: 0, trend_score: 10,
  phase: 'repair', first_board: 38, ge2_count: 9, promo_rate: .23,
  promo_pool: 39, ladder_completeness: .75,
}))

function conclusion(overrides: Partial<EnvConclusion> = {}): EnvConclusion {
  return {
    date: '2026-10-30', cell: 'strong_expand', sentiment: 'strong', volume: 'expand', missing_reason: null,
    plan: null, gate: null, effective_cap_pct: null, limited_by: null, evidence: null, evidence_as_of: null,
    ...overrides,
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.charts.length = 0
  fixtures.resizes.length = 0
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { fixtures.resizes.push(() => callback([], this as unknown as ResizeObserver)) }
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.spyOn(api, 'regimeHistory').mockResolvedValue({ rows, total: rows.length })
  vi.spyOn(api, 'regimeCoverage').mockResolvedValue({ rows: 5, earliest_date: rows[0].date, latest_date: rows[4].date })
  vi.spyOn(api, 'regimeStates').mockResolvedValue({ days: 5, distribution: [
    { state: 'strong', label: '强势', count: 2, pct: 40 }, { state: 'weak', label: '弱势', count: 3, pct: 60 },
  ] })
  vi.spyOn(api, 'regimePhases').mockResolvedValue({ segments: [], total: 0 })
  vi.spyOn(api, 'regimeConclusion').mockResolvedValue(conclusion())
  vi.spyOn(api, 'envMatrixPlan').mockResolvedValue({ plan: {} })
  vi.spyOn(api, 'regimeMainline').mockResolvedValue({ rows: [], leaders: [], membership_note: '', filter: { min_members: 4, max_members: 600, blacklist: [], exclude_st: true } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) }) }
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><Regime /></QueryClientProvider>))
  await settle()
}
async function click(text: string) {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(el => el.textContent?.trim() === text)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
  await settle()
}
const panel = () => host.querySelector<HTMLElement>('[role="tabpanel"]:not([hidden])')!
const trendChart = () => fixtures.charts.find(chart => chart.option?.series?.some((series: any) => series.name === '综合分') && !chart.disposed)!

it('当前状态与分布直接显示真实数值, 切换范围同步刷新原有查询', async () => {
  await render()
  expect(panel().querySelector('[aria-label="综合分 6，满分 100"]')).not.toBeNull()
  const distribution = panel().querySelector('[aria-label="历史环境状态分布"]')!
  expect(distribution.textContent).toContain('2 天40.0%')
  expect(distribution.textContent).toContain('3 天60.0%')
  const ring = fixtures.charts.find(chart => chart.option?.series?.some((series: any) => series.type === 'pie'))
  expect(ring?.option.series[0].data.map((item: { value: number }) => item.value)).toEqual([2, 3])
  await click('2年')
  expect(api.regimeHistory).toHaveBeenLastCalledWith(undefined, undefined, 500)
  expect(api.regimeStates).toHaveBeenLastCalledWith(500)
})

it('键盘切换页签后保留图例选择和图表窗口', async () => {
  await render()
  const chart = trendChart()
  chart.option.legend.selected.赚钱 = true
  const writes = chart.setOption.mock.calls.length
  const overview = host.querySelector<HTMLButtonElement>('#regime-tab-regime')!
  await act(async () => overview.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })))
  await settle()
  expect(panel().id).toBe('regime-panel-phase')
  expect(document.activeElement?.id).toBe('regime-tab-phase')
  await act(async () => document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })))
  await settle()
  expect(panel().id).toBe('regime-panel-regime')
  expect(chart.setOption).toHaveBeenCalledTimes(writes)
  expect(chart.option.legend.selected.赚钱).toBe(true)
})

it('阶段和主线统计跟随主图的真实交易日范围, 不混入全部历史', async () => {
  const older = { ...rows[0], date: '2025-10-24' }
  vi.mocked(api.regimeHistory).mockImplementation(async (_start, _end, limit) => ({
    rows: limit === 500 ? [older, ...rows] : rows, total: limit === 500 ? 6 : 5,
  }))
  await render()
  expect(api.regimePhases).toHaveBeenLastCalledWith('2026-10-26', '2026-10-30')
  expect(api.regimeMainline).toHaveBeenLastCalledWith('2026-10-26', '2026-10-30', 10, 'concept')
  await click('2年')
  expect(api.regimePhases).toHaveBeenLastCalledWith('2025-10-24', '2026-10-30')
  expect(api.regimeMainline).toHaveBeenLastCalledWith('2025-10-24', '2026-10-30', 10, 'concept')
  expect(vi.mocked(api.regimePhases).mock.calls.every(([start, end]) => start != null && end != null)).toBe(true)
})

it('自定义范围仍按交易日提交, 日历展开可收回', async () => {
  await render()
  await click('自定义')
  const input = document.querySelector<HTMLInputElement>('[aria-label="自定义交易日天数"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '90')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('应用')
  expect(api.regimeHistory).toHaveBeenLastCalledWith(undefined, undefined, 90)
  expect(api.regimeStates).toHaveBeenLastCalledWith(90)
  await click('展开全部月份')
  expect(panel().querySelector('[aria-controls="regime-calendar"]')?.getAttribute('aria-expanded')).toBe('true')
  await click('收起月份')
  expect(panel().querySelector('[aria-controls="regime-calendar"]')?.getAttribute('aria-expanded')).toBe('false')
})

it('综合分和四维保持 0–100 左轴, 涨停数使用独立数量轴', async () => {
  await render()
  const option = trendChart().option
  expect(option.yAxis[0]).toMatchObject({ min: 0, max: 100, position: 'left' })
  const series = option.series as { name: string; data: number[]; yAxisIndex: number }[]
  expect(series.find(item => item.name === '综合分')).toMatchObject({ data: rows.map(row => row.score), yAxisIndex: 0 })
  expect(series.find(item => item.name === '涨停数')).toMatchObject({ data: rows.map(row => row.limit_up), yAxisIndex: 1 })
  expect(series.filter(item => item.name !== '涨停数').every(item => item.yAxisIndex === 0)).toBe(true)
})

it('容器尺寸变化触发图表重排, 空数据后重新挂载不引用已销毁实例', async () => {
  await render()
  const chart = trendChart()
  const before = chart.resize.mock.calls.length
  await act(async () => fixtures.resizes.forEach(resize => resize()))
  expect(chart.resize.mock.calls.length).toBeGreaterThan(before)
  await act(async () => client.setQueryData(QK.regimeHistory({ limit: 250 }), { rows: [], total: 0 }))
  await settle()
  expect(chart.disposed).toBe(true)
  await act(async () => client.setQueryData(QK.regimeHistory({ limit: 250 }), { rows, total: rows.length }))
  await settle()
  expect(trendChart()).not.toBe(chart)
  expect(panel().querySelector('[aria-label="综合分 6，满分 100"]')).not.toBeNull()
})

it('旧数据缺少最新阶段时给出缺失说明, 不默认显示修复阶段', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ rows: rows.map((row, index) => ({ ...row, phase: index === 4 ? null : row.phase })), total: 5 })
  await render()
  await click('情绪周期')
  expect(panel().textContent).toContain('最新交易日暂无情绪阶段数据')
  expect(panel().querySelector('[aria-label="当前情绪阶段"]')).toBeNull()
})

it.each(['loading', 'empty', 'error'] as const)('加载状态 %s 有明确反馈与恢复入口', async state => {
  if (state === 'loading') vi.mocked(api.regimeHistory).mockImplementation(() => new Promise(() => {}))
  if (state === 'empty') vi.mocked(api.regimeHistory).mockResolvedValue({ rows: [], total: 0 })
  if (state === 'error') vi.mocked(api.regimeHistory).mockRejectedValue(new Error('离线'))
  await render()
  if (state === 'loading') expect(panel().querySelector('[role="status"]')?.getAttribute('aria-label')).toBe('正在加载市场环境')
  if (state === 'empty') expect(panel().textContent).toContain('暂无环境数据')
  if (state === 'error') {
    expect(panel().querySelector('[role="alert"]')?.textContent).toContain('环境数据加载失败')
    vi.mocked(api.regimeHistory).mockResolvedValue({ rows, total: 5 })
    await click('重新加载')
    expect(panel().querySelector('[aria-label="综合分 6，满分 100"]')).not.toBeNull()
  }
})

const capital = {
  amount_valid_count: 5000, amount_top5_share: .412, amount_multiple: 1.32,
  amount_change_5d: .084, amount_multiple_pctl: 88, top5_share_pctl: 81,
  capital_volume: 'expand', capital_concentration: 'concentrated', capital_flag: 'volume_stall',
} as const
const capitalCard = () => host.querySelector<HTMLElement>('[aria-label="资金状态"]')!

it('资金卡显示档位、倍数、5日变化和集中度', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ rows: rows.map(row => ({ ...row, ...capital })), total: rows.length })
  await render()
  const text = capitalCard().textContent ?? ''
  expect(text).toContain('放量 · 集中')
  expect(text).toContain('放量滞涨')
  expect(text).toContain('成交额 1.32 倍')
  expect(text).toContain('5日 +8.4%')
  expect(text).toContain('前5%个股占成交 41.2%')
})

it('旧数据没有资金列时显示未计算, 不显示数值', async () => {
  await render()
  const text = capitalCard().textContent ?? ''
  expect(text).toContain('未计算')
  expect(text).not.toMatch(/\d\s*倍|缩量|平量|放量|集中|分散/)
})

it('只有一项档位可用时另一项显示占位符, 缺失数值不显示为 0', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ total: rows.length, rows: rows.map(row => ({
    ...row, ...capital, capital_concentration: null, amount_top5_share: null, amount_change_5d: null, capital_flag: null,
  })) })
  await render()
  const text = capitalCard().textContent ?? ''
  expect(text).toContain('放量 · —')
  expect(text).toContain('5日 —')
  expect(text).toContain('前5%个股占成交 —')
  expect(text).not.toContain('放量滞涨')
})

it('成交额分位可叠加到趋势图左轴, 默认不显示', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ rows: rows.map(row => ({ ...row, ...capital })), total: rows.length })
  await render()
  const option = trendChart().option
  expect(option.legend.selected['成交额分位']).toBe(false)
  const series = (option.series as { name: string; data: (number | null)[]; yAxisIndex: number }[]).find(item => item.name === '成交额分位')
  expect(series).toMatchObject({ data: rows.map(() => 88), yAxisIndex: 0 })
})

const conclusionCard = () => host.querySelector<HTMLElement>('[aria-label="环境结论"]')!
const evidence = {
  all: { n: 238, mean: .0036, positive_rate: .58, p10: -.012 },
  train: { n: 91, mean: .0044, positive_rate: .6, p10: -.011 },
  test: { n: 147, mean: .0031, positive_rate: .57, p10: -.013 },
}

it('环境结论显示格子、生效上限、闸门限制、打法与证据', async () => {
  vi.mocked(api.regimeConclusion).mockResolvedValue(conclusion({
    plan: { cap_pct: 70, styles: ['打板', '半路'], note: '主线龙头为主' },
    gate: { cap_pct: 50, report_id: 'a'.repeat(32), as_of: '2026-10-30' },
    effective_cap_pct: 50, limited_by: 'gate', evidence, evidence_as_of: '2026-11-16T08:12:44+08:00',
  }))
  await render()
  const text = conclusionCard().textContent ?? ''
  expect(text).toContain('强 · 放量')
  expect(text).toContain('仓位上限 50%')
  expect(text).toContain('我的预设 70%，被市场闸门 50% 限制')
  expect(text).toContain('打板 · 半路')
  expect(text).toContain('主线龙头为主')
  expect(text).toContain('238 天')
  expect(text).toContain('均值 +0.36%')
  expect(text).toContain('上涨比例 58%')
  expect(text).toContain('第10百分位 -1.20%')
  expect(text).not.toMatch(/建议|推荐/)
})

it('未设置的格子不显示任何仓位数字, 闸门缺失与证据不足都有说明', async () => {
  vi.mocked(api.regimeConclusion).mockResolvedValue(conclusion({
    evidence: { all: { n: 36, mean: null, positive_rate: null, p10: null }, train: { n: 16, mean: null, positive_rate: null, p10: null }, test: { n: 20, mean: null, positive_rate: null, p10: null } },
  }))
  await render()
  const text = conclusionCard().textContent ?? ''
  expect(text).toContain('未设置')
  expect(text).not.toMatch(/\d+%/)
  expect(text).toContain('样本不足（36 天）')
})

it('只有预设时注明未叠加市场闸门', async () => {
  vi.mocked(api.regimeConclusion).mockResolvedValue(conclusion({
    plan: { cap_pct: 40, styles: [], note: '' }, effective_cap_pct: 40, limited_by: 'plan',
  }))
  await render()
  expect(conclusionCard().textContent).toContain('当日未生成盘后计划，未叠加市场闸门')
})

it('无格子时只显示缺失原因', async () => {
  vi.mocked(api.regimeConclusion).mockResolvedValue(conclusion({
    cell: null, volume: null, missing_reason: '资金档位未计算',
  }))
  await render()
  const text = conclusionCard().textContent ?? ''
  expect(text).toContain('资金档位未计算')
  expect(text).not.toContain('仓位上限')
})

async function openEditor() {
  const button = conclusionCard().querySelector<HTMLButtonElement>('button')!
  await act(async () => button.click())
  await settle()
  return document.querySelector<HTMLElement>('[aria-label="编辑环境预设"]')!
}
function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

it('编辑预设保存整表, 取消后再打开显示已保存的值', async () => {
  vi.mocked(api.envMatrixPlan).mockResolvedValue({ plan: { weak_shrink: { cap_pct: 0, styles: ['空仓观望'], note: '' } } })
  const update = vi.spyOn(api, 'envMatrixPlanUpdate').mockImplementation(async plan => ({ plan }))
  await render()
  let dialog = await openEditor()
  const cap = dialog.querySelector<HTMLInputElement>('[aria-label="强·放量仓位上限"]')!
  await act(async () => setInput(cap, '35'))
  await click('取消')
  dialog = await openEditor()
  expect(dialog.querySelector<HTMLInputElement>('[aria-label="强·放量仓位上限"]')!.value).toBe('')
  expect(dialog.querySelector<HTMLInputElement>('[aria-label="弱·缩量仓位上限"]')!.value).toBe('0')
  await act(async () => setInput(dialog.querySelector<HTMLInputElement>('[aria-label="强·放量仓位上限"]')!, '60'))
  await act(async () => dialog.querySelector<HTMLInputElement>('[aria-label="强·放量打法打板"]')!.click())
  await click('保存')
  expect(update).toHaveBeenCalledWith({
    strong_expand: { cap_pct: 60, styles: ['打板'], note: '' },
    weak_shrink: { cap_pct: 0, styles: ['空仓观望'], note: '' },
  })
})


it('重算后环境结论随之刷新', async () => {
  vi.spyOn(api, 'regimeRecompute').mockResolvedValue({ ok: true, computed: 1 })
  await render()
  const before = vi.mocked(api.regimeConclusion).mock.calls.length
  await click('重算')
  expect(vi.mocked(api.regimeConclusion).mock.calls.length).toBeGreaterThan(before)
})
