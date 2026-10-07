// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { HuichunConfig, HuichunObservation, HuichunSnapshot, HuichunTracking } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { Huichun } from './Huichun'
import extension from './extension'

const calls = vi.hoisted(() => ({
  huichunConfig: vi.fn(), huichunSaveConfig: vi.fn(), huichunSnapshot: vi.fn(), huichunScan: vi.fn(),
  huichunTracking: vi.fn(), huichunAddTracking: vi.fn(), huichunRefreshTracking: vi.fn(), huichunRemoveTracking: vi.fn(),
  marketSnapshotForSymbols: vi.fn(),
  extDataList: vi.fn(), extDataRows: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, onClose, navList }: { symbol: string | null; onClose: () => void; navList?: { symbol: string }[] }) => symbol ? <div role="dialog" data-symbol={symbol} data-nav-symbols={navList?.map(row => row.symbol).join(',')}><button onClick={onClose}>关闭股票</button></div> : null }))

function config(): HuichunConfig {
  return { schema_version: 1, revision: 1, updated_at: '2026-09-30T17:00:00+08:00',
    rules: { warmup_bars: 250, rally_threshold: .4, zero_threshold: .02, ma_window: 60, slope_lag: 5 } }
}
function snapshot(): HuichunSnapshot {
  return { config_revision: 1, job: { id: 'job1', kind: 'scan', status: 'completed', started_at: null,
    finished_at: '2026-09-30T17:00:00+08:00', processed_symbols: 100, total_symbols: 100, error: null },
  scan: { id: 'scan1', start_date: '2026-09-30', end_date: '2026-09-30', created_at: '2026-09-30T17:00:00+08:00',
    rule_revision: 1, rules: config().rules, coverage: { symbol_count: 100, candidate_count: 1, factor_excluded_count: 2, gap_symbol_count: 3, latest_daily_date: '2026-09-30', calendar_source: 'observed_market_daily_dates' }, limitations: ['历史 ST 状态未知'],
    candidates: [{ id: 'candidate1', symbol: '600000.SH', name: '测试股票', signal_date: '2026-09-30', g_date: '2026-04-20', d_date: '2026-05-20',
      rally_return: .45, zero_distance: .012, close_above_ma_pct: .02, ma_slope_pct: .01, raw_close: 10, adjusted_close: 12,
      eligibility_status: 'unknown', rule_revision: 1 }] } }
}
function tracking(): HuichunTracking {
  return { updated_at: '2026-10-03T17:00:00+08:00', records: [{ id: 'record1', symbol: '600000.SH', name: '测试股票', signal_date: '2026-09-30',
    rule_revision: 1, rules: config().rules, added_at: '2026-09-30T17:00:00+08:00', updated_at: '2026-10-03T17:00:00+08:00', base_close: 10, base_adjusted_close: 12,
    returns: [{ horizon: 1, target_date: '2026-10-08', status: 'ok', return_pct: .025 },
      { horizon: 2, target_date: '2026-10-09', status: 'unknown_gap', return_pct: null },
      { horizon: 3, target_date: '2026-10-12', status: 'pending', return_pct: null },
      { horizon: 4, target_date: '2026-10-13', status: 'invalid_factor', return_pct: null },
      { horizon: 5, target_date: '2026-10-14', status: 'baseline_changed', return_pct: null }] }] }
}
function observation(overrides: Partial<HuichunObservation> = {}): HuichunObservation {
  const { signal_date, ...candidate } = snapshot().scan!.candidates[0]
  return { ...candidate, id: 'observation1', symbol: '301373.SZ', name: '待金叉股票',
    observation_date: signal_date, gap_distance: .006, previous_gap_distance: .01,
    dif: .12, dea: .18, q: -.06, previous_q: -.1, ...overrides }
}
function observationSnapshot(rows = [observation()]): HuichunSnapshot {
  const state = snapshot()
  Object.assign(state.scan!, { observation_date: '2026-09-30', observations: rows })
  return state
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  storage.conceptAnalysisConfig.remove()
  storage.industryAnalysisConfig.remove()
  localStorage.removeItem('huichun-stock-marks')
  Object.values(calls).forEach(call => call.mockReset())
  calls.huichunConfig.mockResolvedValue(config())
  calls.huichunSnapshot.mockResolvedValue(snapshot())
  calls.huichunScan.mockResolvedValue(snapshot())
  calls.huichunTracking.mockResolvedValue({ records: [], updated_at: null })
  calls.huichunSaveConfig.mockImplementation(async body => {
    const saved = { ...config(), rules: body.rules, revision: 2 }
    calls.huichunConfig.mockResolvedValue(saved)
    calls.huichunSnapshot.mockResolvedValue({ ...snapshot(), config_revision: 2 })
    return saved
  })
  calls.huichunAddTracking.mockResolvedValue(tracking())
  calls.huichunRefreshTracking.mockResolvedValue({ ...snapshot(), job: { ...snapshot().job, id: 'refresh1', kind: 'tracking', status: 'running' } })
  calls.huichunRemoveTracking.mockResolvedValue({ records: [], updated_at: null })
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [] })
  calls.extDataList.mockResolvedValue({ items: [] })
  calls.extDataRows.mockResolvedValue({ rows: [], fields: [], total: 0, date: null, limit: 12000 })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); vi.restoreAllMocks(); client.clear(); host.remove(); storage.conceptAnalysisConfig.remove(); storage.industryAnalysisConfig.remove(); localStorage.removeItem('huichun-stock-marks') })
async function settle() { for (let i = 0; i < 6; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(tab = 'candidates') {
  await act(async () => root.render(<MemoryRouter initialEntries={[`/huichun?tab=${tab}`]}><QueryClientProvider client={client}><Huichun /></QueryClientProvider></MemoryRouter>))
  await settle()
}
function button(label: string) { return [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === label)! }
async function click(label: string) { expect(button(label)).toBeDefined(); await act(async () => button(label).click()); await settle() }
function candidateRows() { return [...host.querySelectorAll<HTMLTableRowElement>('table[aria-label="回春候选股票"] tbody tr')] }
function displayedSymbols() { return candidateRows().map(row => row.textContent!.match(/[0-9]{6}\.(?:SH|SZ|BJ)/)![0]) }
function observationRows() { return [...host.querySelectorAll<HTMLTableRowElement>('table[aria-label="待金叉观察池"] tbody tr')] }
function observationSymbols() { return observationRows().map(row => row.textContent!.match(/[0-9]{6}\.(?:SH|SZ|BJ)/)![0]) }
function trackingRows() { return [...host.querySelectorAll<HTMLTableRowElement>('table[aria-label="回春收益跟踪"] tbody tr')] }
function trackingCell(row: HTMLTableRowElement, label: string) {
  const headers = [...host.querySelectorAll('table[aria-label="回春收益跟踪"] th')]
  const index = headers.findIndex(header => header.textContent === label || header.textContent === `${label}复权`)
  expect(index, `缺少${label}列`).toBeGreaterThanOrEqual(0)
  return row.querySelectorAll('td')[index]
}
async function sortBy(label: string) {
  const header = host.querySelector<HTMLButtonElement>(`button[aria-label="按${label}排序"]`)
  expect(header, `缺少${label}排序按钮`).not.toBeNull()
  await act(async () => header!.click())
  await settle()
}
async function input(label: string, value: string) {
  const element = [...host.querySelectorAll('label')].find(item => item.textContent?.includes(label))!.querySelector('input')!
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value); element.dispatchEvent(new Event('input', { bubbles: true })) })
  await settle()
}

it('registers 回春模式 as an isolated extension page', () => {
  expect(extension.routes?.map(route => route.path)).toEqual(['/huichun'])
  expect(extension.navigation?.[0].label).toBe('回春模式')
})
it('saves percentage input as decimals and keeps the optimistic concurrency revision', async () => {
  await render('rules')
  await input('前段最小涨幅', '55')
  await input('零轴距离上限', '1.5')
  await click('保存规则')
  expect(calls.huichunSaveConfig).toHaveBeenCalledWith({ expected_revision: 1, rules: { ...config().rules, rally_threshold: .55, zero_threshold: .015 } }, expect.anything())
  expect(host.textContent).toContain('已保存为版本 2')
})
it('scans latest complete data with no invented date and adds immutable scan candidates', async () => {
  await render()
  await click('开始筛选')
  expect(calls.huichunScan).toHaveBeenCalledWith({}, expect.anything())
  expect(host.textContent).toContain('45.00%')
  await click('加入跟踪')
  expect(calls.huichunAddTracking).toHaveBeenCalledWith({ scan_id: 'scan1', candidate_ids: ['candidate1'] }, expect.anything())
  expect(calls.huichunSnapshot).toHaveBeenCalledTimes(2)
  expect(button('已跟踪').disabled).toBe(true)
})
it('loads the latest rule values after a conflict in one reload action', async () => {
  calls.huichunSaveConfig.mockRejectedValue(new Error('规则版本冲突，请重新载入'))
  await render('rules')
  await input('前段最小涨幅', '55')
  await click('保存规则')
  expect(host.textContent).toContain('规则版本冲突')
  calls.huichunConfig.mockResolvedValue({ ...config(), revision: 3, rules: { ...config().rules, rally_threshold: .65 } })
  await click('重新载入已保存规则')
  const value = [...host.querySelectorAll('label')].find(item => item.textContent?.includes('前段最小涨幅'))!.querySelector('input')!.value
  expect(value).toBe('65')
  await input('前段最小涨幅', '70')
  calls.huichunSaveConfig.mockResolvedValue({ ...config(), revision: 4, rules: { ...config().rules, rally_threshold: .7 } })
  await click('保存规则')
  expect(calls.huichunSaveConfig).toHaveBeenLastCalledWith({ expected_revision: 3, rules: { ...config().rules, rally_threshold: .7 } }, expect.anything())
})
it('shows independent return statuses without converting missing data to zero', async () => {
  calls.huichunTracking.mockResolvedValue(tracking())
  await render('tracking')
  expect(host.textContent).toContain('+2.50%')
  expect(host.textContent).toContain('数据缺失')
  expect(host.textContent).toContain('未到期')
  expect(host.textContent).toContain('复权异常')
  expect(host.textContent).toContain('基准已变化')
  expect(host.querySelectorAll('td').length).toBeGreaterThan(5)
  await click('移除')
  expect(calls.huichunRemoveTracking).toHaveBeenCalledWith('record1', expect.anything())
  expect(host.textContent).toContain('尚未添加跟踪股票')
})
it('shows raw tracking quotes and their own dates separately from adjusted baselines and fixed returns', async () => {
  const state = tracking()
  state.records.push({ ...state.records[0], id: 'record2', symbol: '600001.SH', name: '第二只股票' })
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [
    { symbol: '600000.SH', raw_close: 11.25, close: 8, change_pct: .01, date: '2026-10-08' },
    { symbol: '600001.SH', raw_close: 9.5, close: 7, change_pct: -.02 },
  ] })
  await render('tracking')
  const rows = trackingRows()
  const latest = trackingCell(rows[0], '现价（元）')
  expect(latest.textContent).toBe('11.252026-10-08')
  expect(latest.querySelector('.text-bull')).not.toBeNull()
  expect(trackingCell(rows[1], '现价（元）').textContent).toBe('9.50日期未知')
  expect(trackingCell(rows[1], '现价（元）').querySelector('.text-bear')).not.toBeNull()
  expect(trackingCell(rows[0], '基准收盘（元）').textContent).toBe('10.00')
  expect(trackingCell(rows[0], 'T+1').textContent).toContain('+2.50%')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledWith(['600000.SH', '600001.SH'])
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('does not substitute adjusted prices or zero for missing and invalid raw tracking quotes', async () => {
  const state = tracking()
  const values = [undefined, null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY]
  state.records = values.map((_, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + index}.SH`, current_basis: { quote_date: '2026-10-09', status: 'ok', factor_multiplier: 1 } }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: values.map((value, index) => ({ symbol: `${600000 + index}.SH`, raw_close: value, close: 987.65, change_pct: .05, date: '2026-10-09' })) })
  await render('tracking')
  for (const row of trackingRows()) {
    expect(trackingCell(row, '现价（元）').textContent).toBe('—暂无行情')
    expect(trackingCell(row, '信号日至今收益率').textContent).toBe('暂无行情')
    expect(trackingCell(row, '现价（元）').querySelector('.text-bull')).toBeNull()
    expect(trackingCell(row, 'T+1').textContent).toContain('+2.50%')
  }
  expect(host.textContent).not.toContain('987.65')
})

it('keeps tracking records visible while the latest quote is loading', async () => {
  calls.huichunTracking.mockResolvedValue(tracking())
  let resolveQuote!: (value: unknown) => void
  calls.marketSnapshotForSymbols.mockImplementation(() => new Promise(resolve => { resolveQuote = resolve }))
  await render('tracking')
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('—行情读取中')
  expect(trackingCell(trackingRows()[0], 'T+1').textContent).toContain('+2.50%')
  await act(async () => resolveQuote({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 10.25, date: '2026-10-09' }] }))
  await settle()
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('10.252026-10-09')
})

it('retries failed tracking quotes without losing records or recomputing their returns', async () => {
  const state = tracking()
  state.records[0].current_basis = { quote_date: '2026-10-09', status: 'ok', factor_multiplier: 1 }
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockRejectedValue(new Error('跟踪行情读取失败'))
  await render('tracking')
  expect(host.textContent).toContain('跟踪行情读取失败')
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('—暂无行情')
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('暂无行情')
  expect(trackingCell(trackingRows()[0], 'T+1').textContent).toContain('+2.50%')
  expect(button('移除').disabled).toBe(false)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 11, date: '2026-10-09' }] })
  await click('重试')
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('11.002026-10-09')
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('+10.00%')
  expect(host.textContent).not.toContain('跟踪行情读取失败')
  expect(calls.huichunTracking).toHaveBeenCalledTimes(1)
  expect(calls.huichunRefreshTracking).not.toHaveBeenCalled()
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('updates tracking quotes on shared cache invalidation and manual return refresh', async () => {
  calls.huichunTracking.mockResolvedValue(tracking())
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 11 }] })
  await render('tracking')
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-12', rows: [{ symbol: '600000.SH', raw_close: 11.5, date: '2026-10-12' }] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('11.502026-10-12')
  expect(calls.huichunTracking).toHaveBeenCalledTimes(1)
  expect(calls.huichunRefreshTracking).not.toHaveBeenCalled()
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-13', rows: [{ symbol: '600000.SH', raw_close: 12, date: '2026-10-13' }] })
  await click('刷新收益')
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('12.002026-10-13')
  expect(calls.huichunRefreshTracking).toHaveBeenCalledTimes(1)
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(3)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('requests only unique visible tracking symbols and never carries a prior page quote to another stock', async () => {
  const state = tracking()
  state.records = Array.from({ length: 51 }, (_, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + Math.max(0, index - 1)}.SH` }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockImplementation(async symbols => ({ as_of: '2026-10-09', rows: symbols.includes('600000.SH') ? [{ symbol: '600000.SH', raw_close: 999 }] : [] }))
  await render('tracking')
  const symbols = calls.marketSnapshotForSymbols.mock.calls[0][0] as string[]
  expect(symbols).toHaveLength(49)
  expect(new Set(symbols).size).toBe(49)
  expect(trackingRows()).toHaveLength(50)
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toContain('999.00')
  expect(trackingCell(trackingRows()[1], '现价（元）').textContent).toContain('999.00')
  await click('下一页')
  expect(calls.marketSnapshotForSymbols).toHaveBeenLastCalledWith(['600049.SH'])
  expect(trackingRows()).toHaveLength(1)
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('—暂无行情')
  expect(host.textContent).not.toContain('999.00')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(2)
})

it('calculates adjusted current returns from raw quotes and factor ratios including ex-rights, gains, losses and zero', async () => {
  const state = tracking()
  const cases = [
    { raw: 11, factor: 1, expected: '+10.00%', color: 'text-bull' },
    { raw: 5.5, factor: 2, expected: '+10.00%', color: 'text-bull' },
    { raw: 10, factor: 1, expected: '0.00%', color: 'text-foreground' },
    { raw: 9, factor: 1, expected: '-10.00%', color: 'text-bear' },
  ]
  state.records = cases.map((value, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + index}.SH`,
    current_basis: { quote_date: '2026-10-09', status: 'ok', factor_multiplier: value.factor } }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: cases.map((value, index) => ({ symbol: `${600000 + index}.SH`, raw_close: value.raw, close: 999, date: '2026-10-09', change_pct: -.08 })) })
  await render('tracking')
  for (const [index, row] of trackingRows().entries()) {
    const cell = trackingCell(row, '信号日至今收益率')
    expect(cell.textContent).toBe(cases[index].expected)
    expect(cell.querySelector(`.${cases[index].color}`)).not.toBeNull()
    expect(trackingCell(row, '基准收盘（元）').textContent).toBe('10.00')
    expect(trackingCell(row, 'T+1').textContent).toContain('+2.50%')
  }
  expect(trackingCell(trackingRows()[1], '现价（元）').textContent).toContain('5.50')
  expect(trackingCell(trackingRows()[2], '信号日至今收益率').querySelector('.text-bull, .text-bear')).toBeNull()
})

it('withholds current returns for legacy records and quotes without a matching valid date', async () => {
  const state = tracking()
  const validBasis = { quote_date: '2026-10-09', status: 'ok' as const, factor_multiplier: 1 }
  const cases = [
    { date: '2026-10-09', basis: undefined, expected: '待刷新' },
    { date: undefined, basis: validBasis, expected: '日期未知' },
    { date: '2026-02-30', basis: validBasis, expected: '日期未知' },
    { date: '2026/10/09', basis: validBasis, expected: '日期未知' },
    { date: '2026-09-29', basis: { ...validBasis, quote_date: '2026-09-29' }, expected: '行情早于信号' },
    { date: '2026-10-12', basis: validBasis, expected: '待刷新' },
    { date: '2026-10-09', basis: { ...validBasis, quote_date: null }, expected: '待刷新' },
  ]
  state.records = cases.map((value, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + index}.SH`, current_basis: value.basis }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: cases.map((value, index) => ({ symbol: `${600000 + index}.SH`, raw_close: 11, date: value.date })) })
  await render('tracking')
  trackingRows().forEach((row, index) => {
    const cell = trackingCell(row, '信号日至今收益率')
    expect(cell.textContent).toBe(cases[index].expected)
    expect(cell.querySelector('.text-bull, .text-bear')).toBeNull()
  })
})

it('displays adjustment basis failure states without presenting them as current zero returns', async () => {
  const state = tracking()
  const cases = [
    { status: 'no_quote', expected: '待刷新' },
    { status: 'unknown_gap', expected: '数据缺失' },
    { status: 'invalid_factor', expected: '复权异常' },
    { status: 'baseline_changed', expected: '基准已变化' },
  ] as const
  state.records = cases.map((value, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + index}.SH`,
    current_basis: { quote_date: '2026-10-09', status: value.status, factor_multiplier: null } }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: cases.map((_, index) => ({ symbol: `${600000 + index}.SH`, raw_close: 11, date: '2026-10-09' })) })
  await render('tracking')
  trackingRows().forEach((row, index) => expect(trackingCell(row, '信号日至今收益率').textContent).toBe(cases[index].expected))
})

it('rejects invalid signal baselines, adjustment multipliers and overflowing current return calculations', async () => {
  const state = tracking()
  const cases = [
    ...[0, -1, Number.NaN, Number.POSITIVE_INFINITY].map(base => ({ base, factor: 1, raw: 11, expected: '基准无效' })),
    ...[null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY].map(factor => ({ base: 10, factor, raw: 11, expected: '复权异常' })),
    { base: 10, factor: 1e308, raw: 1e308, expected: '复权异常' },
  ]
  state.records = cases.map((value, index) => ({ ...state.records[0], id: `record${index}`, symbol: `${600000 + index}.SH`, base_close: value.base,
    current_basis: { quote_date: '2026-10-09', status: 'ok', factor_multiplier: value.factor } }))
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: cases.map((value, index) => ({ symbol: `${600000 + index}.SH`, raw_close: value.raw, close: 999, date: '2026-10-09' })) })
  await render('tracking')
  trackingRows().forEach((row, index) => expect(trackingCell(row, '信号日至今收益率').textContent).toBe(cases[index].expected))
})

it('refreshes same-day adjusted current returns but waits for a verified new-day basis after quotes cross dates', async () => {
  const state = tracking()
  state.records[0].current_basis = { quote_date: '2026-10-09', status: 'ok', factor_multiplier: 1 }
  calls.huichunTracking.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 10.5, date: '2026-10-09' }] })
  await render('tracking')
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('+5.00%')
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 11, date: '2026-10-09' }] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('+10.00%')
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-12', rows: [{ symbol: '600000.SH', raw_close: 5.5, date: '2026-10-12' }] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  expect(trackingCell(trackingRows()[0], '现价（元）').textContent).toBe('5.502026-10-12')
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('待刷新')
  const refreshed = { ...state, records: [{ ...state.records[0], current_basis: { quote_date: '2026-10-12', status: 'ok' as const, factor_multiplier: 2 } }] }
  calls.huichunTracking.mockResolvedValue(refreshed)
  await click('刷新收益')
  await act(async () => { client.setQueryData(QK.huichunSnapshot, { ...snapshot(), job: { ...snapshot().job, id: 'refresh1', kind: 'tracking', status: 'completed' } }) })
  await settle()
  expect(trackingCell(trackingRows()[0], '信号日至今收益率').textContent).toBe('+10.00%')
  expect(calls.huichunTracking).toHaveBeenCalledTimes(2)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})
it('keeps previous results visible while scanning and prevents duplicate actions', async () => {
  calls.huichunSnapshot.mockResolvedValue({ ...snapshot(), job: { ...snapshot().job, status: 'running', processed_symbols: 10 } })
  await render()
  expect(host.textContent).toContain('测试股票')
  expect(host.textContent).toContain('10 / 100')
  expect(button('筛选中').disabled).toBe(true)
})
it('identifies previous rule versions and reuses stock preview', async () => {
  calls.huichunConfig.mockResolvedValue({ ...config(), revision: 2 })
  calls.huichunSnapshot.mockResolvedValue({ ...snapshot(), config_revision: 2 })
  await render()
  expect(host.textContent).toContain('当前规则为版本 2')
  await click('测试股票')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('600000.SH')
})
it('reports API failure while preserving previous candidates', async () => {
  await render()
  calls.huichunSnapshot.mockRejectedValue(new Error('扫描状态读取失败'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.huichunSnapshot }) })
  await settle()
  expect(host.textContent).toContain('扫描状态读取失败')
  expect(host.textContent).toContain('测试股票')
  expect(button('开始筛选').disabled).toBe(true)
})

it('identifies legacy scans that have not excluded current ST stocks', async () => {
  await render()
  expect(host.textContent).toContain('本次结果尚未排除 ST、*ST，请重新筛选。')
  expect(host.textContent).not.toContain('已按当前名称排除 ST、*ST：')
})

it('reports current-name ST exclusions without claiming historical eligibility', async () => {
  const state = snapshot()
  Object.assign(state.scan!.coverage, { st_filter: 'current_instrument_name', st_excluded_count: 6 })
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  expect(host.textContent).toContain('已按当前名称排除 ST、*ST：6 只')
  expect(host.textContent).not.toContain('本次结果尚未排除 ST、*ST')
  expect(host.textContent).toContain('历史 ST、停牌及可成交条件尚未全部核验')
})

it('shows shared board tags and latest quotes separately from signal prices', async () => {
  const state = snapshot()
  state.scan!.candidates = [
    { ...state.scan!.candidates[0], symbol: '301373.SZ', name: '凌玮科技' },
    { ...state.scan!.candidates[0], id: 'candidate2', symbol: '688001.SH', name: '科创股票' },
    { ...state.scan!.candidates[0], id: 'candidate3', symbol: '689009.SH', name: '科创存托' },
  ]
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [
    { symbol: '301373.SZ', raw_close: 119.15, close: 100, change_pct: -.0992 },
    { symbol: '688001.SH', close: 31.58, change_pct: .0266 },
  ] })
  await render()
  const rows = [...host.querySelectorAll('tbody tr')]
  expect(rows[0].textContent).toContain('创')
  expect(rows[0].textContent).toContain('119.15')
  expect(rows[0].textContent).toContain('-9.92%')
  expect(rows[0].textContent).toContain('10.00')
  expect(rows[0].querySelectorAll('.text-bear')).toHaveLength(2)
  const stockCell = rows[0].querySelectorAll('td')[1]
  expect(stockCell.textContent!.indexOf('凌玮科技')).toBeLessThan(stockCell.textContent!.indexOf('301373.SZ'))
  const nameLine = stockCell.querySelector('button')!.parentElement!
  expect(nameLine.textContent).toContain('创')
  expect(nameLine.textContent).not.toContain('301373.SZ')
  expect(nameLine.nextElementSibling?.textContent).toBe('301373.SZ')
  expect(rows[1].textContent).toContain('科')
  expect(rows[1].textContent).toContain('31.58')
  expect(rows[1].textContent).toContain('+2.66%')
  expect(rows[2].textContent).toContain('科')
  expect(rows[2].querySelectorAll('td')[2].textContent).toBe('—')
  expect(host.textContent).toContain('行情日期：2026-10-09')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledWith(['301373.SZ', '688001.SH', '689009.SH'])
})

it('refreshes quote cache without recomputing the persisted A0 scan', async () => {
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 12, change_pct: 0 }] })
  await render()
  const snapshotCalls = calls.huichunSnapshot.mock.calls.length
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [{ symbol: '600000.SH', raw_close: 12.5, change_pct: .025 }] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  expect(host.textContent).toContain('12.50')
  expect(host.textContent).toContain('+2.50%')
  expect(calls.huichunSnapshot).toHaveBeenCalledTimes(snapshotCalls)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('keeps candidates usable if the latest quote request fails', async () => {
  calls.marketSnapshotForSymbols.mockRejectedValue(new Error('行情读取失败'))
  await render()
  expect(host.textContent).toContain('行情读取失败')
  expect(host.textContent).toContain('测试股票')
  expect(host.querySelectorAll('tbody tr td')[2].textContent).toBe('—')
  expect(button('加入跟踪').disabled).toBe(false)
})

it('only requests the current page symbols and does not reuse the previous page prices', async () => {
  const state = snapshot()
  state.scan!.candidates = Array.from({ length: 51 }, (_, i) => ({ ...state.scan!.candidates[0], id: `candidate${i}`, symbol: `${600000 + i}.SH` }))
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockImplementation(async symbols => ({ as_of: '2026-10-09', rows: symbols.includes('600000.SH') ? [{ symbol: '600000.SH', raw_close: 999, change_pct: .03 }] : [] }))
  await render()
  expect(calls.marketSnapshotForSymbols.mock.calls[0][0]).toHaveLength(50)
  await click('下一页')
  expect(calls.marketSnapshotForSymbols).toHaveBeenLastCalledWith(['600050.SH'])
  expect(host.textContent).not.toContain('999.00')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
})

it('sorts names, codes, dates and numeric signal fields in three states without changing source rows', async () => {
  const state = snapshot()
  const symbols = ['600003.SH', '600001.SH', '600002.SH']
  state.scan!.candidates = [10, 2, 4].map((value, i) => ({
    ...state.scan!.candidates[0], id: `candidate${i}`, symbol: symbols[i],
    name: ['Z股票', 'A股票', 'M股票'][i], signal_date: ['2026-09-30', '2026-09-28', '2026-09-29'][i],
    rally_return: value / 100, zero_distance: value / 100, close_above_ma_pct: value / 100,
    ma_slope_pct: value / 100, raw_close: value,
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  for (const field of ['名称', '代码', '信号日期', '前段涨幅', '零轴距离', '高于均线', '均线涨幅', '信号收盘（元）']) {
    await sortBy(field)
    expect(displayedSymbols()).toEqual([symbols[1], symbols[2], symbols[0]])
    await sortBy(field)
    expect(displayedSymbols()).toEqual([symbols[0], symbols[2], symbols[1]])
    await sortBy(field)
    expect(displayedSymbols()).toEqual(symbols)
  }
  expect(state.scan!.candidates.map(row => row.symbol)).toEqual(symbols)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('sorts all candidates before pagination and returns to page one when changing the sort', async () => {
  const state = snapshot()
  state.scan!.candidates = Array.from({ length: 51 }, (_, i) => ({
    ...state.scan!.candidates[0], id: `candidate${i}`, symbol: `${600000 + i}.SH`,
    raw_close: i === 50 ? 2 : 20 + i,
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  await click('下一页')
  expect(displayedSymbols()).toEqual(['600050.SH'])
  await sortBy('信号收盘（元）')
  expect(displayedSymbols()).toHaveLength(50)
  expect(displayedSymbols().slice(0, 3)).toEqual(['600050.SH', '600000.SH', '600001.SH'])
  expect(button('上一页').disabled).toBe(true)
})

it('uses displayed raw prices and decimal changes for sorting and keeps missing quotes last', async () => {
  const state = snapshot()
  const symbols = ['600000.SH', '600001.SH', '600002.SH', '600003.SH']
  state.scan!.candidates = symbols.map((symbol, i) => ({ ...state.scan!.candidates[0], id: `candidate${i}`, symbol, name: `股票${i}` }))
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [
    { symbol: symbols[1], raw_close: 10, close: 1, change_pct: .025 },
    { symbol: symbols[2], raw_close: 2, close: 99, change_pct: -.0992 },
    { symbol: symbols[3], raw_close: 4, change_pct: 0 },
  ] })
  await render()
  await sortBy('现价')
  expect(displayedSymbols()).toEqual([symbols[2], symbols[3], symbols[1], symbols[0]])
  await sortBy('现价')
  expect(displayedSymbols()).toEqual([symbols[1], symbols[3], symbols[2], symbols[0]])
  await sortBy('涨跌幅')
  expect(displayedSymbols()).toEqual([symbols[2], symbols[3], symbols[1], symbols[0]])
  await sortBy('涨跌幅')
  expect(displayedSymbols()).toEqual([symbols[1], symbols[3], symbols[2], symbols[0]])
  await click('股票1')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-nav-symbols')).toBe([symbols[1], symbols[3], symbols[2], symbols[0]].join(','))
})

it('loads every candidate quote in bounded batches and keeps the query stable during sorting and pagination', async () => {
  const state = snapshot()
  const symbols = Array.from({ length: 101 }, (_, i) => `${600000 + i}.SH`)
  state.scan!.candidates = symbols.map((symbol, i) => ({ ...state.scan!.candidates[0], id: `candidate${i}`, symbol }))
  calls.huichunSnapshot.mockResolvedValue(state)
  let refreshed = false
  calls.marketSnapshotForSymbols.mockImplementation(async (requested: string[]) => ({ as_of: '2026-10-09', rows: requested.map(symbol => ({
    symbol, raw_close: refreshed && symbol === '600050.SH' ? 0.5 : 101 - symbols.indexOf(symbol), change_pct: 0,
  })) }))
  await render()
  expect(calls.marketSnapshotForSymbols.mock.calls[0][0]).toHaveLength(50)
  calls.marketSnapshotForSymbols.mockClear()
  await sortBy('现价')
  expect(calls.marketSnapshotForSymbols.mock.calls.map(call => call[0].length)).toEqual([100, 1])
  expect(displayedSymbols()[0]).toBe('600100.SH')
  await click('下一页')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(2)
  await click('上一页')
  await sortBy('现价')
  expect(displayedSymbols()[0]).toBe('600000.SH')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(2)
  refreshed = true
  const snapshotCalls = calls.huichunSnapshot.mock.calls.length
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  expect(calls.marketSnapshotForSymbols.mock.calls.map(call => call[0].length)).toEqual([100, 1, 100, 1])
  expect(calls.huichunSnapshot).toHaveBeenCalledTimes(snapshotCalls)
  expect(calls.huichunScan).not.toHaveBeenCalled()
  await click('下一页')
  await click('下一页')
  expect(displayedSymbols()).toEqual(['600050.SH'])
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(4)
})

it('asks to regenerate observations for legacy snapshots without treating them as an empty pool', async () => {
  await render('watch')
  expect(host.textContent).toMatch(/观察[^。]*重新筛选|重新筛选[^。]*观察/)
  expect(host.textContent).not.toContain('暂无符合')
  expect(observationRows()).toHaveLength(0)
  expect(calls.marketSnapshotForSymbols).not.toHaveBeenCalled()
  expect(button('加入跟踪')).toBeUndefined()
})

it('shows an empty dated observation pool for a completed scan', async () => {
  calls.huichunSnapshot.mockResolvedValue(observationSnapshot([]))
  await render('watch')
  expect(host.textContent).toContain('2026-09-30')
  expect(host.textContent).toMatch(/暂无[^。]*观察|观察[^。]*暂无/)
  expect(host.textContent).not.toMatch(/观察[^。]*请重新筛选/)
  expect(observationRows()).toHaveLength(0)
  expect(calls.marketSnapshotForSymbols).not.toHaveBeenCalled()
})

it('keeps observation quotes, sorting and preview navigation separate from confirmed candidates', async () => {
  const rows = [
    observation(),
    observation({ id: 'observation2', symbol: '688001.SH', name: '科创观察', gap_distance: .002, previous_gap_distance: .003 }),
    observation({ id: 'observation3', symbol: '600003.SH', name: '主板观察', gap_distance: .004, previous_gap_distance: .005 }),
  ]
  const state = observationSnapshot(rows)
  state.scan!.candidates = [
    { ...state.scan!.candidates[0], symbol: '600002.SH', name: '确认乙' },
    { ...state.scan!.candidates[0], id: 'candidate2', symbol: '600001.SH', name: '确认甲' },
  ]
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockImplementation(async (symbols: string[]) => ({ as_of: '2026-10-09', rows: symbols.map(symbol => ({
    symbol, raw_close: symbol === '301373.SZ' ? 119.15 : symbol === '688001.SH' ? 20 : 30, change_pct: -.0992,
  })) }))
  await render()
  await sortBy('代码')
  expect(displayedSymbols()).toEqual(['600001.SH', '600002.SH'])
  calls.marketSnapshotForSymbols.mockClear()
  await click('待金叉观察池')
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledWith(['301373.SZ', '600003.SH', '688001.SH'])
  expect(observationSymbols()).toEqual(rows.map(row => row.symbol))
  const first = observationRows()[0]
  expect(first.textContent).toContain('119.15')
  expect(first.textContent).toContain('-9.92%')
  expect(first.textContent).toContain('0.60%')
  expect(first.textContent).toContain('1.00%')
  expect(first.textContent).toContain('创')
  expect(first.textContent).toContain('待金叉')
  const nameLine = first.querySelector('button')!.parentElement!
  expect(nameLine.textContent).not.toContain('301373.SZ')
  expect(nameLine.nextElementSibling?.textContent).toBe('301373.SZ')
  expect(observationRows()[1].textContent).toContain('科')
  expect(host.querySelectorAll('input[type="checkbox"]')).toHaveLength(0)
  expect(button('加入跟踪')).toBeUndefined()
  expect(button('加入所选跟踪')).toBeUndefined()
  await sortBy('当前差距')
  expect(observationSymbols()).toEqual(['688001.SH', '600003.SH', '301373.SZ'])
  await click('科创观察')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-nav-symbols')).toBe('688001.SH,600003.SH,301373.SZ')
  await click('关闭股票')
  await sortBy('昨日差距')
  expect(observationSymbols()).toEqual(['688001.SH', '600003.SH', '301373.SZ'])
  await sortBy('现价')
  expect(observationSymbols()).toEqual(['688001.SH', '600003.SH', '301373.SZ'])
  await click('候选筛选')
  expect(displayedSymbols()).toEqual(['600001.SH', '600002.SH'])
  expect(calls.marketSnapshotForSymbols).toHaveBeenLastCalledWith(['600001.SH', '600002.SH'])
  expect(calls.huichunScan).not.toHaveBeenCalled()
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
})

it('sorts the full observation pool before paging without changing the candidate page', async () => {
  const state = observationSnapshot(Array.from({ length: 51 }, (_, i) => observation({
    id: `observation${i}`, symbol: `${300000 + i}.SZ`, gap_distance: i === 50 ? .0001 : .01 + i / 10000,
  })))
  state.scan!.candidates = Array.from({ length: 51 }, (_, i) => ({ ...state.scan!.candidates[0], id: `candidate${i}`, symbol: `${600000 + i}.SH` }))
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  await click('下一页')
  expect(displayedSymbols()).toEqual(['600050.SH'])
  await click('待金叉观察池')
  expect(observationRows()).toHaveLength(50)
  await click('下一页')
  expect(observationSymbols()).toEqual(['300050.SZ'])
  await sortBy('当前差距')
  expect(observationRows()).toHaveLength(50)
  expect(observationSymbols()[0]).toBe('300050.SZ')
  expect(button('上一页').disabled).toBe(true)
  await click('候选筛选')
  expect(displayedSymbols()).toEqual(['600050.SH'])
})

it('starts the shared scan from the observation pool and replaces rows only when its snapshot completes', async () => {
  const previous = observationSnapshot()
  calls.huichunSnapshot.mockResolvedValue(previous)
  calls.huichunScan.mockResolvedValue({ ...previous, job: { ...previous.job, id: 'watch-scan', status: 'running', processed_symbols: 10 } })
  await render('watch')
  await click('开始筛选')
  expect(calls.huichunScan).toHaveBeenCalledWith({}, expect.anything())
  expect(observationSymbols()).toEqual(['301373.SZ'])
  expect(button('筛选中').disabled).toBe(true)
  expect(host.textContent).toContain('10 / 100')
  const completed = observationSnapshot([observation({ id: 'new-observation', symbol: '688001.SH', name: '新观察' })])
  completed.scan!.id = 'scan2'
  calls.huichunSnapshot.mockResolvedValue(completed)
  await act(async () => { await client.invalidateQueries({ queryKey: QK.huichunSnapshot }) })
  await settle()
  expect(observationSymbols()).toEqual(['688001.SH'])
  expect(button('开始筛选').disabled).toBe(false)
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
})

it('preserves the observation pool when starting a new scan fails', async () => {
  calls.huichunSnapshot.mockResolvedValue(observationSnapshot())
  calls.huichunScan.mockRejectedValue(new Error('筛选请求失败'))
  await render('watch')
  await click('开始筛选')
  expect(host.textContent).toContain('筛选请求失败')
  expect(observationSymbols()).toEqual(['301373.SZ'])
  expect(button('开始筛选').disabled).toBe(false)
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
})

function dimensionConfig(id: string, field: string, extra: Record<string, unknown> = {}) {
  return { id, label: id, mode: 'snapshot', created_at: '', updated_at: '',
    fields: [{ name: 'symbol', dtype: 'string', label: '代码' }, { name: field, dtype: 'string', label: field }], ...extra }
}
function dimensionResult(id: string, field: string, rows: Record<string, unknown>[]) {
  return { id, label: id, mode: 'snapshot', date: '2026-10-09', total: rows.length, limit: 12000,
    fields: dimensionConfig(id, field).fields, rows }
}
function sectorCell(row: HTMLTableRowElement, _watching = false) {
  return row.querySelectorAll<HTMLTableCellElement>('td')[5]
}
async function retryDimensionError(message: string) {
  const notice = [...host.querySelectorAll('[role="alert"]')].find(item => item.textContent?.includes(message))
  expect(notice, `缺少独立错误提示：${message}`).toBeDefined()
  const retry = notice!.querySelector<HTMLButtonElement>('button')
  expect(retry, '错误提示应提供重试').not.toBeNull()
  await act(async () => retry!.click())
  await settle()
}

it('shows current industry and collapsible concepts in both pools using the same batch cache', async () => {
  const state = observationSnapshot()
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.extDataList.mockResolvedValue({ items: [
    dimensionConfig('concepts', '所属概念'), dimensionConfig('industries', '所属行业'),
  ] })
  calls.extDataRows.mockImplementation(async (id: string) => id === 'concepts'
    ? dimensionResult(id, '所属概念', [
      { symbol: '600000.SH', 所属概念: '机器人;人工智能;新能源;芯片' },
      { symbol: '301373.SZ', 所属概念: '新材料;专精特新' },
    ])
    : dimensionResult(id, '所属行业', [
      { symbol: '600000.SH', 所属行业: '金融-银行-股份制银行' },
      { symbol: '301373.SZ', 所属行业: '基础化工-化学制品' },
    ]))
  await render()
  expect([...host.querySelectorAll('thead th')].map(item => item.textContent)).toContain('题材 / 板块')
  const cell = sectorCell(candidateRows()[0])
  expect(cell.textContent).toContain('金融-银行-股份制银行')
  expect(cell.textContent).toContain('机器人')
  expect(cell.textContent).toContain('人工智能')
  expect(cell.textContent).not.toContain('新能源')
  expect(cell.textContent).not.toContain('芯片')
  await click('展开 2 项题材')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('新能源')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('芯片')
  await click('收起题材')
  expect(sectorCell(candidateRows()[0]).textContent).not.toContain('新能源')
  expect(host.textContent).toContain('当前归属')
  expect(calls.extDataRows).toHaveBeenCalledWith('concepts', { limit: 12000 })
  expect(calls.extDataRows).toHaveBeenCalledWith('industries', { limit: 12000 })
  await click('待金叉观察池')
  expect(sectorCell(observationRows()[0], true).textContent).toContain('基础化工-化学制品')
  expect(sectorCell(observationRows()[0], true).textContent).toContain('新材料')
  expect(observationRows()[0].textContent).toContain('创')
  await click('候选筛选')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('金融-银行-股份制银行')
  expect(calls.extDataRows).toHaveBeenCalledTimes(2)
  expect(calls.huichunScan).not.toHaveBeenCalled()
  expect(state.scan!.candidates[0]).not.toHaveProperty('sector')
})

it('keeps both lists usable with no stock membership source and ignores market-level tables', async () => {
  calls.huichunSnapshot.mockResolvedValue(observationSnapshot())
  calls.extDataList.mockResolvedValue({ items: [
    dimensionConfig('market_theme', '题材', { market_level: true }),
    dimensionConfig('market_sector', '行业', { market_level: true }),
  ] })
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toBe('—')
  expect(button('加入跟踪').disabled).toBe(false)
  await click('待金叉观察池')
  expect(sectorCell(observationRows()[0], true).textContent).toBe('—')
  expect(calls.extDataRows).not.toHaveBeenCalled()
  expect(observationSymbols()).toEqual(['301373.SZ'])
})

it('retries a failed membership request and preserves its last data on later refresh failure', async () => {
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('concepts', '所属概念')] })
  calls.extDataRows.mockRejectedValue(new Error('题材数据读取失败'))
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toBe('—')
  expect(button('加入跟踪').disabled).toBe(false)
  calls.extDataRows.mockResolvedValue(dimensionResult('concepts', '所属概念', [
    { symbol: '600000.SH', 所属概念: '金融科技' },
  ]))
  await retryDimensionError('题材数据读取失败')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('金融科技')
  calls.extDataRows.mockRejectedValue(new Error('题材刷新失败'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.extDataRows('concepts', undefined, 12000) }) })
  await settle()
  expect(host.textContent).toContain('题材刷新失败')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('金融科技')
  expect(button('加入跟踪').disabled).toBe(false)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('recovers from a source-list failure without rescanning candidate signals', async () => {
  calls.extDataList.mockRejectedValue(new Error('板块来源读取失败'))
  await render()
  expect(displayedSymbols()).toEqual(['600000.SH'])
  expect(calls.extDataRows).not.toHaveBeenCalled()
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('industries', '所属行业')] })
  calls.extDataRows.mockResolvedValue(dimensionResult('industries', '所属行业', [
    { symbol: '600000.SH', 所属行业: '金融-银行' },
  ]))
  await retryDimensionError('板块来源读取失败')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('金融-银行')
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('uses the saved analysis sources and fields instead of a different detected membership', async () => {
  storage.conceptAnalysisConfig.set({ configId: 'chosen_theme', dimensionField: '分组' })
  storage.industryAnalysisConfig.set({ configId: 'chosen_sector', dimensionField: '分类' })
  calls.extDataList.mockResolvedValue({ items: [
    dimensionConfig('default_theme', '所属概念'), dimensionConfig('default_sector', '所属行业'),
    dimensionConfig('chosen_theme', '分组'), dimensionConfig('chosen_sector', '分类'),
  ] })
  calls.extDataRows.mockImplementation(async (id: string) => id === 'chosen_theme'
    ? dimensionResult(id, '分组', [{ symbol: '600000.SH', 分组: '用户题材' }])
    : dimensionResult(id, '分类', [{ symbol: '600000.SH', 分类: '用户行业' }]))
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('用户题材')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('用户行业')
  expect(calls.extDataRows.mock.calls.map(call => call[0]).sort()).toEqual(['chosen_sector', 'chosen_theme'])
})

it('sorts by complete membership text before paging and keeps unmatched stocks last in both directions', async () => {
  const state = snapshot()
  state.scan!.candidates = Array.from({ length: 51 }, (_, i) => ({
    ...state.scan!.candidates[0], id: `candidate${i}`, symbol: `${600000 + i}.SH`,
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('concepts', '所属概念')] })
  calls.extDataRows.mockResolvedValue(dimensionResult('concepts', '所属概念', [
    { symbol: '600000.SH', 所属概念: 'A共同;B共同;Z隐藏' },
    { symbol: '600050.SH', 所属概念: 'A共同;B共同;C隐藏' },
  ]))
  await render()
  await click('下一页')
  await sortBy('题材 / 板块')
  expect(displayedSymbols().slice(0, 2)).toEqual(['600050.SH', '600000.SH'])
  expect(button('上一页').disabled).toBe(true)
  expect(sectorCell(candidateRows()[0]).textContent).not.toContain('C隐藏')
  await sortBy('题材 / 板块')
  expect(displayedSymbols().slice(0, 3)).toEqual(['600000.SH', '600050.SH', '600001.SH'])
  expect(sectorCell(candidateRows()[2]).textContent).toBe('—')
  expect(calls.extDataRows).toHaveBeenCalledTimes(1)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('maps per-dimension members and plain codes without matching the wrong exchange', async () => {
  const state = snapshot()
  state.scan!.candidates = ['600000.SH', '600001.SH', '600002.SH'].map((symbol, i) => ({
    ...state.scan!.candidates[0], symbol, id: `candidate${i}`,
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  const concepts = dimensionConfig('concepts', '概念')
  concepts.fields.push({ name: 'members', dtype: 'string', label: '成分股' })
  calls.extDataList.mockResolvedValue({ items: [concepts, dimensionConfig('industries', '所属行业')] })
  calls.extDataRows.mockImplementation(async (id: string) => id === 'concepts'
    ? { ...dimensionResult(id, '概念', [
      { 概念: '纯代码题材', members: ['600000'] },
      { 概念: '错误交易所题材', members: [{ symbol: '600001.SZ' }] },
      { 概念: '完整代码题材', members: ['600002.SH', { symbol: '600002.SH' }] },
    ]), fields: concepts.fields }
    : dimensionResult(id, '所属行业', [
      { symbol: '600000.SH', 所属行业: '金融-银行' },
      { symbol: '600001.SZ', 所属行业: '错误交易所行业' },
      { code: '600002', 所属行业: '制造-装备' },
    ]))
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('金融-银行')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('纯代码题材')
  expect(sectorCell(candidateRows()[1]).textContent).toBe('—')
  expect(sectorCell(candidateRows()[2]).textContent).toContain('制造-装备')
  expect(sectorCell(candidateRows()[2]).textContent).toContain('完整代码题材')
  expect(host.textContent).not.toContain('错误交易所')
  expect(sectorCell(candidateRows()[2]).textContent!.match(/完整代码题材/g)).toHaveLength(1)
})

it('discloses truncated membership coverage without discarding available rows', async () => {
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('concepts', '所属概念')] })
  calls.extDataRows.mockResolvedValue({ ...dimensionResult('concepts', '所属概念', [
    { symbol: '600000.SH', 所属概念: '已获取题材' },
  ]), total: 12001 })
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('已获取题材')
  expect(host.textContent).toContain('题材、行业资料超过读取上限')
  expect(host.textContent).toContain('部分股票可能未显示归属')
  expect(button('加入跟踪').disabled).toBe(false)
  expect(calls.extDataRows).toHaveBeenCalledWith('concepts', { limit: 12000 })
})

it('shows loading membership without leaking the previous source while its replacement is pending', async () => {
  let resolvePrevious!: (value: ReturnType<typeof dimensionResult>) => void
  let resolveReplacement!: (value: ReturnType<typeof dimensionResult>) => void
  const previousRequest = new Promise<ReturnType<typeof dimensionResult>>(resolve => { resolvePrevious = resolve })
  const replacementRequest = new Promise<ReturnType<typeof dimensionResult>>(resolve => { resolveReplacement = resolve })
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('previous_theme', '所属概念')] })
  calls.extDataRows.mockImplementation((id: string) => id === 'previous_theme' ? previousRequest : replacementRequest)
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('读取中')
  expect(button('加入跟踪').disabled).toBe(false)
  await act(async () => resolvePrevious(dimensionResult('previous_theme', '所属概念', [
    { symbol: '600000.SH', 所属概念: '原来源题材' },
  ])))
  await settle()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('原来源题材')
  storage.conceptAnalysisConfig.set({ configId: 'replacement_theme', dimensionField: '新题材' })
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('replacement_theme', '新题材')] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.extData }) })
  await settle()
  expect(calls.extDataRows).toHaveBeenLastCalledWith('replacement_theme', { limit: 12000 })
  expect(sectorCell(candidateRows()[0]).textContent).toContain('读取中')
  expect(host.textContent).not.toContain('原来源题材')
  await act(async () => resolveReplacement(dimensionResult('replacement_theme', '新题材', [
    { symbol: '600000.SH', 新题材: '新来源题材' },
  ])))
  await settle()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('新来源题材')
  expect(host.textContent).not.toContain('原来源题材')
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('rejects cached rows from a renamed membership field until a retry supplies the current field', async () => {
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('concepts', '所属概念')] })
  calls.extDataRows.mockResolvedValue(dimensionResult('concepts', '所属概念', [
    { symbol: '600000.SH', 所属概念: '过期字段题材' },
  ]))
  await render()
  expect(sectorCell(candidateRows()[0]).textContent).toContain('过期字段题材')
  calls.extDataList.mockResolvedValue({ items: [dimensionConfig('concepts', '所属题材')] })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.extData }) })
  await settle()
  expect(sectorCell(candidateRows()[0]).textContent).toBe('—')
  expect(host.textContent).not.toContain('过期字段题材')
  expect(button('加入跟踪').disabled).toBe(false)
  calls.extDataRows.mockResolvedValue(dimensionResult('concepts', '所属题材', [
    { symbol: '600000.SH', 所属题材: '更新字段题材' },
  ]))
  await retryDimensionError('题材 / 板块')
  expect(sectorCell(candidateRows()[0]).textContent).toContain('更新字段题材')
  expect(host.querySelector('[role="alert"]')).toBeNull()
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

function stockMarkButton(action: string, name: string, symbol: string) {
  const label = `${action} ${name} ${symbol}`
  const found = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.getAttribute('aria-label') === label)
  expect(found, `缺少股票标记按钮：${label}`).toBeDefined()
  return found!
}
async function markStock(action: string, name: string, symbol: string) {
  await act(async () => stockMarkButton(action, name, symbol).click())
  await settle()
}
async function remount(tab = 'candidates') {
  await act(async () => root.unmount())
  root = createRoot(host)
  await render(tab)
}

it('shares stock marks across duplicate signals and both pools without adding return tracking or moving focused rows', async () => {
  const state = observationSnapshot([observation({ symbol: '600000.SH', name: '测试股票' })])
  state.scan!.candidates.push({ ...state.scan!.candidates[0], id: 'duplicate', signal_date: '2026-09-29' })
  state.scan!.candidates.unshift({ ...state.scan!.candidates[0], id: 'other', symbol: '600001.SH', name: '其他股票' })
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  const before = displayedSymbols()
  const cellCount = candidateRows()[0].cells.length
  expect(stockMarkButton('重点关注', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('false')
  await markStock('重点关注', '测试股票', '600000.SH')
  expect(displayedSymbols()).toEqual(before)
  const duplicateFocus = [...host.querySelectorAll('button[aria-label="取消重点关注 测试股票 600000.SH"]')]
  expect(duplicateFocus).toHaveLength(2)
  expect(duplicateFocus.every(item => item.getAttribute('aria-pressed') === 'true')).toBe(true)
  expect(candidateRows()[0].cells.length).toBe(cellCount)
  await markStock('置顶', '测试股票', '600000.SH')
  expect(displayedSymbols()).toEqual(['600000.SH', '600000.SH', '600001.SH'])
  await click('待金叉观察池')
  expect(stockMarkButton('取消重点关注', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(stockMarkButton('取消置顶', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  await markStock('取消重点关注', '测试股票', '600000.SH')
  expect(stockMarkButton('取消置顶', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  await click('候选筛选')
  expect([...host.querySelectorAll('button[aria-label="重点关注 测试股票 600000.SH"]')]).toHaveLength(2)
  await markStock('取消置顶', '测试股票', '600000.SH')
  expect(displayedSymbols()).toEqual(before)
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('persists stock marks across quote refreshes, new scan identifiers and component remounts', async () => {
  await render()
  await markStock('重点关注', '测试股票', '600000.SH')
  await markStock('置顶', '测试股票', '600000.SH')
  expect(JSON.parse(localStorage.getItem('huichun-stock-marks')!)).toMatchObject({
    '600000.SH': { focused: true, pinned: true },
  })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.marketSnapshot }) })
  await settle()
  const next = snapshot()
  next.scan!.id = 'new-scan'
  Object.assign(next.scan!.candidates[0], { id: 'new-signal', signal_date: '2026-10-09', name: '更名股票' })
  calls.huichunScan.mockResolvedValue(next)
  await click('开始筛选')
  expect(stockMarkButton('取消重点关注', '更名股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(stockMarkButton('取消置顶', '更名股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  await remount()
  expect(stockMarkButton('取消重点关注', '更名股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(stockMarkButton('取消置顶', '更名股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
})

it('applies pinned stock marks before pagination, resets both pages and follows that order in stock navigation', async () => {
  const state = observationSnapshot(Array.from({ length: 51 }, (_, i) => observation({
    id: `observation${i}`, symbol: `${600000 + i}.SH`, name: `股票${i}`,
  })))
  state.scan!.candidates = Array.from({ length: 51 }, (_, i) => ({
    ...state.scan!.candidates[0], id: `candidate${i}`, symbol: `${600000 + i}.SH`, name: `股票${i}`,
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  await render()
  await click('下一页')
  expect(displayedSymbols()).toEqual(['600050.SH'])
  await click('待金叉观察池')
  await click('下一页')
  expect(observationSymbols()).toEqual(['600050.SH'])
  await markStock('置顶', '股票50', '600050.SH')
  expect(observationSymbols().slice(0, 3)).toEqual(['600050.SH', '600000.SH', '600001.SH'])
  expect(button('上一页').disabled).toBe(true)
  await click('候选筛选')
  expect(displayedSymbols().slice(0, 3)).toEqual(['600050.SH', '600000.SH', '600001.SH'])
  expect(button('上一页').disabled).toBe(true)
  await click('股票50')
  const nav = host.querySelector('[role="dialog"]')?.getAttribute('data-nav-symbols')?.split(',')
  expect(nav).toHaveLength(51)
  expect(nav?.slice(0, 3)).toEqual(['600050.SH', '600000.SH', '600001.SH'])
  await click('关闭股票')
  await markStock('取消置顶', '股票50', '600050.SH')
  expect(displayedSymbols()[0]).toBe('600000.SH')
  await click('下一页')
  expect(displayedSymbols()).toEqual(['600050.SH'])
})

it('keeps pinned stock marks ahead of numeric and quote sort groups without duplicate quote requests', async () => {
  const state = snapshot()
  const symbols = ['600000.SH', '600001.SH', '600002.SH', '600003.SH']
  state.scan!.candidates = symbols.map((symbol, i) => ({
    ...state.scan!.candidates[0], id: `candidate${i}`, symbol, name: `股票${i}`, raw_close: [30, 10, 20, 40][i],
  }))
  localStorage.setItem('huichun-stock-marks', JSON.stringify({
    '600001.SH': { focused: false, pinned: true }, '600003.SH': { focused: false, pinned: true },
  }))
  calls.huichunSnapshot.mockResolvedValue(state)
  calls.marketSnapshotForSymbols.mockResolvedValue({ as_of: '2026-10-09', rows: [
    { symbol: symbols[0], raw_close: 2, change_pct: -.1 },
    { symbol: symbols[1], raw_close: 5, change_pct: .2 },
    { symbol: symbols[2], raw_close: 3, change_pct: .05 },
  ] })
  await render()
  expect(displayedSymbols()).toEqual([symbols[1], symbols[3], symbols[0], symbols[2]])
  await sortBy('信号收盘（元）')
  expect(displayedSymbols()).toEqual([symbols[1], symbols[3], symbols[2], symbols[0]])
  await sortBy('信号收盘（元）')
  expect(displayedSymbols()).toEqual([symbols[3], symbols[1], symbols[0], symbols[2]])
  await sortBy('现价')
  expect(displayedSymbols()).toEqual([symbols[1], symbols[3], symbols[0], symbols[2]])
  await markStock('置顶', '股票2', symbols[2])
  expect(displayedSymbols()).toEqual([symbols[2], symbols[1], symbols[3], symbols[0]])
  await sortBy('现价')
  expect(displayedSymbols()).toEqual([symbols[1], symbols[2], symbols[3], symbols[0]])
  await sortBy('涨跌幅')
  expect(displayedSymbols()).toEqual([symbols[2], symbols[1], symbols[3], symbols[0]])
  await markStock('取消置顶', '股票3', symbols[3])
  expect(displayedSymbols()).toEqual([symbols[2], symbols[1], symbols[0], symbols[3]])
  expect(calls.marketSnapshotForSymbols).toHaveBeenCalledTimes(1)
  expect(calls.huichunScan).not.toHaveBeenCalled()
})

it('ignores malformed stock marks and allows a valid mark after damaged local storage', async () => {
  const damaged = ['null', '[]', '"broken"', '{invalid json', JSON.stringify({ '600000.SH': true }),
    JSON.stringify({ '600000.SH': { focused: 'yes', pinned: 1 } })]
  for (const raw of damaged) {
    localStorage.setItem('huichun-stock-marks', raw)
    await remount()
    expect(displayedSymbols()).toEqual(['600000.SH'])
    expect(stockMarkButton('重点关注', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('false')
    expect(stockMarkButton('置顶', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('false')
  }
  await markStock('重点关注', '测试股票', '600000.SH')
  expect(stockMarkButton('取消重点关注', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(JSON.parse(localStorage.getItem('huichun-stock-marks')!)['600000.SH'].focused).toBe(true)
})

it('reports failed stock marks persistence and retries the intended mark without blocking candidate actions', async () => {
  await render()
  const original = Storage.prototype.setItem
  const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (key === 'huichun-stock-marks') throw new DOMException('Storage full', 'QuotaExceededError')
    original.call(this, key, value)
  })
  await markStock('重点关注', '测试股票', '600000.SH')
  expect(host.textContent).toContain('标记未能保存')
  expect(displayedSymbols()).toEqual(['600000.SH'])
  expect(button('加入跟踪').disabled).toBe(false)
  expect(localStorage.getItem('huichun-stock-marks')).toBeNull()
  save.mockRestore()
  await retryDimensionError('标记未能保存')
  expect(JSON.parse(localStorage.getItem('huichun-stock-marks')!)['600000.SH'].focused).toBe(true)
  expect(stockMarkButton('取消重点关注', '测试股票', '600000.SH').getAttribute('aria-pressed')).toBe('true')
  expect(host.textContent).not.toContain('标记未能保存')
  expect(calls.huichunAddTracking).not.toHaveBeenCalled()
})
