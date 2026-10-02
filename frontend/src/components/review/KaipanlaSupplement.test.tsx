// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { KaipanlaContext, KaipanlaRefreshResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { KaipanlaSupplement } from './KaipanlaSupplement'

const calls = vi.hoisted(() => ({
  context: vi.fn(),
  refresh: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: {
  kaipanlaContext: calls.context,
  kaipanlaRefresh: calls.refresh,
} }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.context.mockReset()
  calls.refresh.mockReset()
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
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function render(date: string, historical = false) {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <KaipanlaSupplement date={date} historical={historical} />
    </QueryClientProvider>,
  ))
  await settle()
}

async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.includes(text))
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

function context(date: string, count?: number): KaipanlaContext {
  return {
    source: '开盘啦', date, fetched_at: count == null ? null : `${date}T15:30:00+08:00`,
    state: count == null ? 'no_data' : 'ok',
    tables: [{
      id: 'ext_kpl_emotion', label: '市场情绪', state: count == null ? 'no_data' : 'ok',
      date, fetched_at: count == null ? null : `${date}T15:30:00+08:00`,
      columns: [{ name: 'ztjs', label: '涨停家数', type: 'float' }],
      rows: count == null ? [] : [{ ztjs: count }], total: count == null ? 0 : 1,
    }],
  }
}

it('only reads saved data on mount and fetches supplements when the user asks', async () => {
  calls.context.mockResolvedValue(context('2026-09-30'))
  calls.refresh.mockResolvedValue({
    source: '开盘啦', date: '2026-09-30',
    results: [{ id: 'ext_kpl_emotion', state: 'ok', rows: 1 }],
    context: context('2026-09-30', 35),
  } satisfies KaipanlaRefreshResult)

  await render('2026-09-30')
  expect(calls.refresh).not.toHaveBeenCalled()
  expect(host.textContent).toContain('暂无该日补充数据')
  await click('获取补充数据')
  expect(calls.refresh).toHaveBeenCalledWith('2026-09-30')
  expect(host.textContent).toContain('涨停家数')
  expect(host.textContent).toContain('35')
  expect(host.textContent).toContain('行情归属 2026-09-30')
  expect(host.textContent).toContain('2026-09-30 15:30:00')
  expect(client.getQueryData(QK.kaipanlaContext('2026-09-30'))).toEqual(context('2026-09-30', 35))
})

it('keeps each date isolated while a different date is loading', async () => {
  let resolveNext!: (value: KaipanlaContext) => void
  calls.context.mockImplementation((date: string) => date === '2026-09-29'
    ? Promise.resolve(context(date, 29))
    : new Promise<KaipanlaContext>(resolve => { resolveNext = resolve }))

  await render('2026-09-29')
  expect(host.textContent).toContain('29')
  await render('2026-09-30', true)
  expect(host.textContent).not.toContain('涨停家数')
  expect(host.textContent).toContain('读取补充数据')
  await act(async () => resolveNext(context('2026-09-30', 30)))
  await settle()
  expect(host.textContent).toContain('30')
  expect(host.textContent).toContain('后续获取的盘后或补充数据')
  expect(host.textContent).toContain('不能视为当时盘中已知')
})

it('preserves the last snapshot when refreshing or polling fails', async () => {
  calls.context.mockResolvedValue(context('2026-09-30', 35))
  calls.refresh.mockRejectedValue(new Error('supplier raw error'))
  await render('2026-09-30')
  await click('获取补充数据')
  expect(host.textContent).toContain('获取失败，正在显示上次快照')
  expect(host.textContent).toContain('35')
  expect(host.textContent).not.toContain('supplier raw error')

  calls.context.mockRejectedValue(new Error('read failed'))
  await act(async () => { await client.invalidateQueries({ queryKey: QK.kaipanlaContext('2026-09-30') }) })
  await settle()
  expect(host.textContent).toContain('35')
  expect(host.textContent).toContain('2026-09-30 15:30:00')
})

it('shows partial failures and readable fields without revealing nested payloads', async () => {
  const value = context('2026-09-30', 35)
  value.state = 'partial'
  value.tables.push({
    id: 'ext_kpl_live', label: '大盘直播', state: 'error', date: value.date,
    fetched_at: `${value.date}T14:30:00+08:00`, retrieved_after_date: true, total: 1,
    columns: [{ name: 'Comment', label: '盘面解读', type: 'str' },
      { name: 'ShareData_json', label: '附加数据', type: 'str' }],
    rows: [{ Comment: '半导体板块午后走强', ShareData_json: '{"secret":1}' }],
  })
  calls.context.mockResolvedValue(value)
  await render('2026-09-30')
  expect(host.textContent).toContain('部分可用')
  await click('查看补充明细')
  expect(host.textContent).toContain('半导体板块午后走强')
  expect(host.textContent).toContain('获取失败 · 保留上次快照')
  expect(host.textContent).not.toContain('secret')
  expect(host.textContent).not.toContain('ShareData_json')
  expect(host.textContent).not.toContain('ext_kpl_live')
})

it('does not attach a delayed refresh or its error to the newly selected date', async () => {
  let rejectRefresh!: (reason: Error) => void
  calls.context.mockImplementation((date: string) => Promise.resolve(context(date, date.endsWith('29') ? 29 : 30)))
  calls.refresh.mockImplementation(() => new Promise<KaipanlaRefreshResult>((_resolve, reject) => { rejectRefresh = reject }))
  await render('2026-09-29')
  await click('获取补充数据')
  await render('2026-09-30')
  await act(async () => rejectRefresh(new Error('old date failed')))
  await settle()
  expect(host.textContent).toContain('行情归属 2026-09-30')
  expect(host.textContent).not.toContain('获取失败')
  expect(host.textContent).toContain('30')
})

it('polls the saved snapshot every minute without triggering an upstream fetch', async () => {
  vi.useFakeTimers()
  try {
    calls.context.mockResolvedValue(context('2026-09-30', 35))
    await act(async () => root.render(
      <QueryClientProvider client={client}><KaipanlaSupplement date="2026-09-30" /></QueryClientProvider>,
    ))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(calls.context).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(calls.context).toHaveBeenCalledTimes(2)
    expect(calls.refresh).not.toHaveBeenCalled()
    expect(host.textContent).toContain('35')
  } finally {
    vi.useRealTimers()
  }
})

it('rejects a mismatched snapshot date and uses Beijing time across a UTC day boundary', async () => {
  const wrong = context('2026-09-29', 29)
  calls.context.mockResolvedValueOnce(wrong)
  await render('2026-09-30')
  expect(host.textContent).not.toContain('涨停家数')
  expect(host.textContent).toContain('读取或获取失败')

  const value = context('2026-09-30', 35)
  value.fetched_at = '2026-09-29T16:30:00Z'
  calls.context.mockResolvedValue(value)
  await act(async () => { await client.invalidateQueries({ queryKey: QK.kaipanlaContext('2026-09-30') }) })
  await settle()
  expect(host.textContent).toContain('2026-09-30 00:30:00')
  expect(host.textContent).toContain('35')

  calls.refresh.mockResolvedValue({
    source: '开盘啦', date: '2026-09-29', results: [], context: wrong,
  } satisfies KaipanlaRefreshResult)
  await click('获取补充数据')
  expect(host.textContent).toContain('获取失败，正在显示上次快照')
  expect(host.textContent).toContain('35')
  expect(host.textContent).toContain('行情归属 2026-09-30')
})

it('formats supplier epoch seconds and milliseconds only for time fields and labels supplier interpretation', async () => {
  const value = context('2026-09-30', 35)
  const epochMs = Date.parse('2026-09-30T07:00:00Z')
  const epochSeconds = epochMs / 1000
  value.tables.push({
    id: 'ext_kpl_live', label: '大盘直播', state: 'ok', date: value.date,
    fetched_at: value.fetched_at, total: 3,
    columns: [{ name: 'Time', label: '直播时间' }, { name: 'Comment', label: '盘面解读' }],
    rows: [{ Time: epochSeconds, Comment: '盘面走强' },
      { Time: String(epochMs), Comment: '量能放大' },
      { Time: '09:25:00', Comment: '竞价观察' }],
  }, {
    id: 'ext_kpl_limit_reasons', label: '涨停原因', state: 'ok', date: value.date,
    fetched_at: value.fetched_at, total: 3,
    columns: [{ name: 'limit_up_ts', label: '涨停时间' }, { name: 'reason', label: '涨停原因' }],
    rows: [{ limit_up_ts: String(epochSeconds), reason: '题材催化' },
      { limit_up_ts: epochMs, reason: '板块走强' },
      { limit_up_ts: '123456789', reason: String(epochSeconds) }],
  }, {
    id: 'ext_kpl_capacity', label: '市场量能', state: 'ok', date: value.date,
    fetched_at: value.fetched_at, total: 3,
    columns: [{ name: 'time', label: '来源时间' }, { name: 'last_wan', label: '最新量能(万)' }],
    rows: [{ time: String(epochSeconds), last_wan: epochSeconds },
      { time: epochMs, last_wan: epochMs },
      { time: '1790751600.5', last_wan: '1234567890' }],
  })
  calls.context.mockResolvedValue(value)
  await render('2026-09-30')
  await click('查看补充明细')

  const live = host.querySelector('table[aria-label="大盘直播"]')!
  const reasons = host.querySelector('table[aria-label="涨停原因"]')!
  const capacity = host.querySelector('table[aria-label="市场量能"]')!
  expect([...live.querySelectorAll('tbody tr')].map(row => row.firstElementChild?.textContent))
    .toEqual(['2026-09-30 15:00:00', '2026-09-30 15:00:00', '09:25:00'])
  expect([...reasons.querySelectorAll('tbody tr')].map(row => row.firstElementChild?.textContent))
    .toEqual(['2026-09-30 15:00:00', '2026-09-30 15:00:00', '123456789'])
  expect(reasons.querySelector('tbody tr:last-child td:last-child')?.textContent).toBe(String(epochSeconds))
  expect([...capacity.querySelectorAll('tbody tr')].map(row => row.firstElementChild?.textContent))
    .toEqual(['2026-09-30 15:00:00', '2026-09-30 15:00:00', '1790751600.5'])
  expect([...capacity.querySelectorAll('tbody tr')].map(row => row.lastElementChild?.textContent))
    .toEqual([String(epochSeconds), String(epochMs), '1234567890'])
  expect(value.tables[1].rows[0].Time).toBe(epochSeconds)
  expect(host.textContent?.match(/来源观点 · 开盘啦解读/g)).toHaveLength(2)
})
