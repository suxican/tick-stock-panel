// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ExtDataStatCard } from './ExtDataStatCard'
import { QK } from '@/lib/queryKeys'
import type { ExtDataConfig } from '@/lib/api'

const calls = vi.hoisted(() => ({
  syncZzshareLimitReasons: vi.fn(), extDataRows: vi.fn(), extDataPullRun: vi.fn(),
  extDataPullConfig: vi.fn(), extDataUpload: vi.fn(),
}))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))
vi.mock('./ExtDataPullPanel', () => ({ ExtDataPullPanel: () => <div>通用拉取面板</div> }))
vi.mock('./ExtConfigTransfer', () => ({ ExtConfigExportButton: () => null }))

const config: ExtDataConfig = {
  id: 'zzshare_limit_reasons', label: 'ZZShare 涨停归因', mode: 'timeseries',
  fields: [{ name: 'reason', label: '涨停原因', dtype: 'string' }],
  description: '盘后涨停归因', pull: null, created_at: '', updated_at: '',
}
const day = '2025-09-30'
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, value: () => [] })
  vi.resetAllMocks()
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
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
}
async function render(next = config) {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <ExtDataStatCard config={next} onDelete={vi.fn()} deleting={false} />
    </QueryClientProvider>,
  ))
  await settle()
}
function button(label: string) {
  const result = [...host.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label || b.textContent?.trim() === label)
  expect(result, label).toBeDefined()
  return result!
}
async function click(label: string) {
  await act(async () => button(label).click())
  await settle()
}
async function selectDate(date: string) {
  const input = host.querySelector<HTMLInputElement>('input[type=date]')!
  expect(input).not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, date)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

it('归因卡片可直接按交易日同步并查看，不走通用拉取', async () => {
  calls.syncZzshareLimitReasons.mockResolvedValue({ date: day, table_id: config.id, state: 'synced', raw_rows: 17, stocks: 12 })
  calls.extDataRows.mockResolvedValue({ date: day, total: 1, rows: [{ symbol: '600000.SH', name: '浦发银行', trade_date: day, reason: '该交易日完整原因' }] })
  client.setQueryData(QK.extData, [])
  client.setQueryData(QK.dataStatus, {})
  await render()
  await click('同步 / 查看归因')
  expect(host.textContent).not.toContain('通用拉取面板')
  expect(button('同步涨停归因').disabled).toBe(true)
  await selectDate(day)
  await click('同步涨停归因')
  expect(calls.syncZzshareLimitReasons).toHaveBeenCalledWith(day)
  expect(host.textContent).toContain('17 条归因记录')
  expect(host.textContent).toContain('12 只股票')
  expect(client.getQueryState(QK.extData)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.dataStatus)?.isInvalidated).toBe(true)
  await click('查看涨停归因')
  expect(calls.extDataRows).toHaveBeenLastCalledWith(config.id, { date: day, limit: 20000, quiet: true })
  expect(host.textContent).toContain('该交易日完整原因')
  expect(calls.extDataPullRun).not.toHaveBeenCalled()
  expect(calls.extDataPullConfig).not.toHaveBeenCalled()
  expect(calls.extDataUpload).not.toHaveBeenCalled()
})

it('设置入口也显示专用同步，错误可重试且区分暂无数据和已有快照', async () => {
  calls.syncZzshareLimitReasons.mockRejectedValueOnce(new Error('请求失败，请稍后重试'))
    .mockResolvedValueOnce({ date: day, table_id: config.id, state: 'no_data', raw_rows: 0, stocks: 0 })
    .mockResolvedValueOnce({ date: day, table_id: config.id, state: 'cached', raw_rows: 17, stocks: 12 })
  client.setQueryData(QK.extData, [])
  await render()
  await click('ZZShare 涨停归因设置')
  expect(host.textContent).not.toContain('通用拉取面板')
  expect([...host.querySelectorAll('button')].map(b => b.textContent?.trim())).not.toContain('上传')
  await selectDate(day)
  await click('同步涨停归因')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('请求失败')
  await click('同步涨停归因')
  expect(host.textContent).toContain('当日暂无归因数据，未写入快照')
  expect(host.textContent).not.toContain('已保存快照')
  await click('同步涨停归因')
  expect(host.textContent).toContain('读取已有快照')
  expect(client.getQueryState(QK.extData)?.isInvalidated).toBe(false)
  await selectDate('2025-09-29')
  expect(host.querySelector('[role=status]')).toBeNull()
  expect(calls.extDataPullConfig).not.toHaveBeenCalled()
})

it('同步期间禁止重复提交和切日，重新打开面板重置日期', async () => {
  let finish!: (value: object) => void
  calls.syncZzshareLimitReasons.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  await render()
  await click('同步 / 查看归因')
  await selectDate(day)
  await click('同步涨停归因')
  expect(button('同步中…').disabled).toBe(true)
  expect(host.querySelector<HTMLInputElement>('input[type=date]')?.disabled).toBe(true)
  await click('同步中…')
  expect(calls.syncZzshareLimitReasons).toHaveBeenCalledTimes(1)
  await act(async () => finish({ date: day, table_id: config.id, state: 'cached', raw_rows: 1, stocks: 1 }))
  await settle()
  await click('关闭')
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 250)) })
  await click('同步 / 查看归因')
  expect(host.querySelector<HTMLInputElement>('input[type=date]')?.value).toBe('')
  expect(button('同步涨停归因').disabled).toBe(true)
})

it('普通扩展表保持通用拉取、推送和上传入口', async () => {
  await render({ ...config, id: 'custom_reasons', label: '其他归因' })
  expect(host.textContent).not.toContain('同步 / 查看归因')
  await click('其他归因设置')
  expect(host.textContent).toContain('通用拉取面板')
  expect(button('推送')).toBeDefined()
  expect(button('上传')).toBeDefined()
  expect(calls.syncZzshareLimitReasons).not.toHaveBeenCalled()
})
