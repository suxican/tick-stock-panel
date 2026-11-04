// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsDataSourcesPanel } from './DataSources'
import { QK } from '@/lib/queryKeys'
import { ApiError } from '@/lib/api'

const calls = vi.hoisted(() => ({
  dataSources: vi.fn(), capabilityMatrix: vi.fn(), updateDataProviders: vi.fn(),
  testDataSource: vi.fn(), syncZzshareLimitReasons: vi.fn(), extDataRows: vi.fn(),
}))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: {} }), useCapabilities: () => ({ data: undefined }),
}))
vi.mock('./Keys', () => ({
  TickFlowKeySection: () => null, TierHelpPopover: () => null, useInvalidateTierRelated: () => vi.fn(),
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  calls.dataSources.mockResolvedValue({ builtin: [], custom: [], plugins: [{
    name: 'zzshare', display_name: '自在量化', datasets: ['daily'], available: true, runtime: 'none',
  }] })
  calls.capabilityMatrix.mockResolvedValue({ capabilities: [{
    id: 'daily', label: '日K', field: 'daily_data_provider', usable: true, current: 'tickflow',
    default: 'tickflow', candidates: [{ name: 'zzshare', display: '自在量化' }], pending: [],
  }] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}
async function render() {
  await act(async () => root.render(
    <QueryClientProvider client={client}><SettingsDataSourcesPanel /></QueryClientProvider>,
  ))
  await settle()
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label || b.textContent?.trim() === label)
  expect(button, label).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
async function selectDate(day: string) {
  const input = host.querySelector<HTMLInputElement>('input[type=date]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, day)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

it('通过已注册插件试拉股票日 K 并按选定日期同步，保留原路由', async () => {
  calls.testDataSource.mockResolvedValue({ rows: 1, preview: [{ date: '2025-09-30', close: 10.82 }] })
  calls.syncZzshareLimitReasons.mockResolvedValue({
    date: '2025-09-30', table_id: 'zzshare_limit_reasons', state: 'synced', raw_rows: 17, stocks: 12,
  })
  client.setQueryData(QK.extData, [])
  await render()
  expect([...host.querySelectorAll('button')].some(b => b.textContent === '套用')).toBe(false)
  await click('查看操作')
  expect(host.textContent).toContain('ETF')
  await click('试拉股票日 K')
  expect(calls.testDataSource).toHaveBeenCalledWith('zzshare', 'daily', ['600000.SH'])
  expect(host.textContent).toContain('10.82')

  await click('同步涨停归因')
  expect(calls.syncZzshareLimitReasons).not.toHaveBeenCalled()
  await selectDate('2025-09-30')
  await click('同步涨停归因')
  expect(calls.syncZzshareLimitReasons).toHaveBeenCalledWith('2025-09-30')
  expect(host.textContent).toContain('17 条')
  expect(host.textContent).toContain('12 只')
  expect(client.getQueryState(QK.extData)?.isInvalidated).toBe(true)
  expect(calls.updateDataProviders).not.toHaveBeenCalled()
  await click('自在量化')
  expect(calls.updateDataProviders).toHaveBeenCalledWith({ daily_data_provider: 'zzshare' })
})

it('无行情与同步失败分别给出反馈，可以重试', async () => {
  calls.testDataSource.mockResolvedValue({ rows: 0, preview: [] })
  calls.syncZzshareLimitReasons.mockRejectedValue(new Error('当日尚未发布'))
  await render()
  await click('查看操作')
  await click('试拉股票日 K')
  expect(host.textContent).toContain('未返回日 K')
  await selectDate('2025-09-30')
  client.setQueryData(QK.extData, [])
  await click('同步涨停归因')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('当日尚未发布')
  calls.syncZzshareLimitReasons.mockResolvedValue({ date: '2025-09-30', state: 'no_data', raw_rows: 0, stocks: 0 })
  await click('同步涨停归因')
  expect(host.textContent).toContain('当日暂无归因数据')
  calls.syncZzshareLimitReasons.mockResolvedValue({ date: '2025-09-30', state: 'cached', raw_rows: 17, stocks: 12 })
  await click('同步涨停归因')
  expect(host.textContent).toContain('读取已有快照')
  expect(client.getQueryState(QK.extData)?.isInvalidated).toBe(false)
  expect(calls.updateDataProviders).not.toHaveBeenCalled()
})

it('在卡片内按所选交易日查看完整归因，切日后不展示旧日期', async () => {
  calls.extDataRows.mockImplementation(async (_id, opts) => ({
    date: opts.date, total: 1, rows: [{
      symbol: '600000.SH', name: '浦发银行', reason: `${opts.date} 完整原因`,
    }],
  }))
  await render()
  await click('查看操作')
  await click('查看涨停归因')
  expect(calls.extDataRows).not.toHaveBeenCalled()
  await selectDate('2025-09-30')
  await click('查看涨停归因')
  expect(calls.extDataRows).toHaveBeenLastCalledWith('zzshare_limit_reasons', {
    date: '2025-09-30', limit: 20000, quiet: true,
  })
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('2025-09-30 完整原因')
  await click('关闭')
  await selectDate('2025-09-29')
  await click('查看涨停归因')
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('2025-09-29 完整原因')
  expect(host.querySelector('[role=dialog]')?.textContent).not.toContain('2025-09-30 完整原因')
  expect(calls.syncZzshareLimitReasons).not.toHaveBeenCalled()
})

it('未同步时说明先同步，读取失败时可重试且不假装没有数据', async () => {
  calls.extDataRows.mockRejectedValue(new ApiError('扩展表不存在', 404))
  await render()
  await click('查看操作')
  await selectDate('2025-09-30')
  await click('查看涨停归因')
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('请先同步该交易日')
  await click('关闭')
  calls.extDataRows.mockRejectedValue(new ApiError('服务暂时不可用', 500))
  await click('查看涨停归因')
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('服务暂时不可用')
  calls.extDataRows.mockResolvedValue({ total: 0, rows: [] })
  await click('重试')
  expect(host.querySelector('[role=dialog]')?.textContent).toContain('请先同步该交易日')
})
