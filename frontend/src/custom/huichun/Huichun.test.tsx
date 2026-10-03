// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { HuichunConfig, HuichunSnapshot, HuichunTracking } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Huichun } from './Huichun'
import extension from './extension'

const calls = vi.hoisted(() => ({
  huichunConfig: vi.fn(), huichunSaveConfig: vi.fn(), huichunSnapshot: vi.fn(), huichunScan: vi.fn(),
  huichunTracking: vi.fn(), huichunAddTracking: vi.fn(), huichunRefreshTracking: vi.fn(), huichunRemoveTracking: vi.fn(),
  marketSnapshotForSymbols: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, onClose }: { symbol: string | null; onClose: () => void }) => symbol ? <div role="dialog" data-symbol={symbol}><button onClick={onClose}>关闭股票</button></div> : null }))

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
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 6; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(tab = 'candidates') {
  await act(async () => root.render(<MemoryRouter initialEntries={[`/huichun?tab=${tab}`]}><QueryClientProvider client={client}><Huichun /></QueryClientProvider></MemoryRouter>))
  await settle()
}
function button(label: string) { return [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === label)! }
async function click(label: string) { expect(button(label)).toBeDefined(); await act(async () => button(label).click()); await settle() }
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
