// @vitest-environment jsdom
import { act, Suspense } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import { LimitUpLadder } from './extension'

const calls = vi.hoisted(() => ({ ladder: vi.fn(), rows: vi.fn() }))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { limitLadder: calls.ladder, extDataRows: calls.rows, monitorRulesList: async () => ({ rules: [] }) },
}))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilities: () => ({}), usePreferences: () => ({}) }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, tradeDate }: { symbol?: string; tradeDate?: string }) =>
    symbol ? <output data-preview>{symbol} {tradeDate}</output> : null,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (date: string) => void }) =>
    <button onClick={() => onChange('2025-03-09')}>选择历史日期</button>,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const recent = '2025-03-10'
const prior = '2025-03-07'
const stock = { symbol: '600000.SH', name: '浦发银行', consecutive_limit_ups: 1 }
const other = { symbol: '000001.SZ', name: '平安银行', consecutive_limit_ups: 1 }
function payload(date: string, reason: string) {
  return { id: 'zzshare_limit_reasons', mode: 'timeseries', date, total: 1,
    rows: [{ symbol: stock.symbol, trade_date: date, reason, source: 'zzshare' }] }
}
beforeAll(async () => {
  // Await module transformation before rendering through the lazy extension.
  await import('./Page')
})
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  localStorage.clear()
  calls.ladder.mockReset().mockImplementation(async (requested?: string) => ({
    as_of: requested ? prior : recent, tiers: [{ boards: 1, count: 2, stocks: [stock, other] }],
  }))
  calls.rows.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  client.clear()
  vi.restoreAllMocks()
})
async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}
async function render() {
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}>
        <Suspense fallback={<span>加载中</span>}><LimitUpLadder /></Suspense>
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
  await settle()
}
async function click(element: HTMLElement) {
  await act(async () => element.click())
  await settle()
}

it('一次读取梯队实际返回的当日快照并按代码匹配，切换历史日与个股详情保持一致', async () => {
  calls.rows.mockImplementation(async (_id, { date }) => payload(date, date === recent ? '当日原因' : '历史原因'))
  await render()
  expect(calls.rows).toHaveBeenCalledTimes(1)
  expect(host.textContent).not.toContain('ZZShare')
  expect(host.textContent).not.toContain('点击「涨停归因」展开全文')
  const details = host.querySelector('details')!
  expect(details.parentElement?.textContent).toContain('浦发银行')
  expect(details.parentElement?.textContent).not.toContain('平安银行')
  expect(details.textContent).toContain('当日原因')
  await click(details.querySelector('summary')!)
  expect(details.open).toBe(true)
  expect(host.querySelector('[data-preview]')).toBeNull()

  await click([...host.querySelectorAll('button')].find(el => el.textContent === '选择历史日期')!)
  await settle()
  expect(calls.rows.mock.calls.map(([, opts]) => opts.date)).toEqual([recent, prior])
  expect(host.textContent).not.toContain('当日原因')
  expect(host.textContent).toContain('历史原因')
  const card = [...host.querySelectorAll<HTMLElement>('[role=button]')].find(el => el.textContent?.includes('浦发银行'))!
  await click(card)
  expect(host.querySelector('[data-preview]')?.textContent).toBe('600000.SH ' + prior)
})

it('归因请求失败与空快照都保留两只梯队股票', async () => {
  calls.rows.mockRejectedValueOnce(new Error('归因请求失败')).mockResolvedValue({
    id: 'zzshare_limit_reasons', mode: 'timeseries', date: prior, total: 0, rows: [],
  })
  await render()
  expect(host.textContent).toContain('归因暂不可用')
  expect(host.textContent).toContain('浦发银行')
  await click([...host.querySelectorAll('button')].find(el => el.textContent === '选择历史日期')!)
  await settle()
  expect(host.textContent).not.toContain('ZZShare')
  expect(host.textContent).toContain('平安银行')
  expect(host.querySelector('details')).toBeNull()
})

it('归因加载期间可以打开个股，切至跌停梯队不带入涨停原因', async () => {
  let finish!: () => void
  calls.rows.mockImplementation(() => new Promise(resolve => {
    finish = () => resolve(payload(recent, '仅涨停归因'))
  }))
  await render()
  expect(host.textContent).not.toContain('ZZShare')
  const card = [...host.querySelectorAll<HTMLElement>('[role=button]')].find(el => el.textContent?.includes('浦发银行'))!
  await click(card)
  expect(host.querySelector('[data-preview]')?.textContent).toBe('600000.SH ' + recent)
  await click([...host.querySelectorAll('button')].find(el => el.textContent === '跌停0')!)
  await act(async () => finish())
  await settle()
  expect(host.textContent).toContain('连跌梯队')
  expect(host.textContent).not.toContain('仅涨停归因')
  expect(calls.rows).toHaveBeenCalledTimes(1)
})

it('只有断板数据时显示筛选空态并保留日期，开启断板筛选后恢复股票与归因', async () => {
  calls.ladder.mockResolvedValue({
    as_of: recent, counts: { up: 0, down: 0 },
    tiers: [{ boards: 2, count: 1, stocks: [{ ...stock, status: 'failed' }] }],
  })
  calls.rows.mockResolvedValue(payload(recent, '当日原因'))
  await render()
  expect(host.textContent).toContain('当前筛选条件下暂无股票')
  expect(host.textContent).toContain('调整上方状态、板块筛选或配置，或切换日期查看')
  expect(host.textContent).toContain('选择历史日期')
  expect(host.textContent).not.toContain('浦发银行')

  await click([...host.querySelectorAll('button')].find(el => el.textContent === '断板')!)
  expect(host.textContent).not.toContain('当前筛选条件下暂无股票')
  expect(host.textContent).toContain('浦发银行')
  expect(host.querySelector('details')?.textContent).toContain('当日原因')
  expect(calls.rows).toHaveBeenCalledTimes(1)
})

it('当日没有原始梯队时仍可切换日期和涨跌方向', async () => {
  calls.ladder.mockResolvedValue({ as_of: recent, tiers: [], counts: { up: 0, down: 0 } })
  await render()
  expect(host.textContent).toContain('暂无连板数据')
  expect(host.textContent).toContain('选择历史日期')
  expect(calls.rows).not.toHaveBeenCalled()

  await click([...host.querySelectorAll('button')].find(el => el.textContent === '跌停0')!)
  expect(host.textContent).toContain('暂无连跌数据')
  expect(host.textContent).toContain('选择历史日期')
})
