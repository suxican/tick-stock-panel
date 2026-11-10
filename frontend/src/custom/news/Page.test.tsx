// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { NewsCatalystGroup, NewsCatalystItem, NewsCatalystsResult } from '@/lib/api'
import { NewsAnalysis } from './Page'
import extension from './extension'

const calls = vi.hoisted(() => ({ catalysts: vi.fn(), phases: vi.fn() }))
vi.mock('@/lib/api', async actual => ({
  ...await actual<typeof import('@/lib/api')>(),
  api: { newsCatalysts: calls.catalysts, regimePhases: calls.phases },
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, tradeDate, navList }: { symbol?: string; tradeDate?: string; navList?: { symbol: string }[] }) =>
    symbol ? <output data-stock data-nav={navList?.map(s => s.symbol).join(',')}>{symbol} · {tradeDate}</output> : null,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (value: string) => void }) => <button onClick={() => onChange('2026-11-08')}>选取历史日</button>,
}))

let host: HTMLDivElement
let root: Root

function item(symbol: string, name: string, change: number, keywords: string[]): NewsCatalystItem {
  return {
    symbol, name, change_pct: change, amount: 2e8, tag: '涨停', tag_code: 'LIMIT_UP', keywords, content: name + '的解读正文',
    anomaly_at: '2026-11-06T09:40:00+08:00', published_at: '2026-11-06T09:41:00+08:00', limit_up: true, next_change_pct: -0.01,
  }
}
function group(keyword: string, symbols: string[], extra: Partial<NewsCatalystGroup>): NewsCatalystGroup {
  return {
    keyword, symbols, stock_count: symbols.length, limit_up_count: 2, quote_count: symbols.length, avg_change_pct: 0.08, batch: true,
    leader: null, anchor: null, seen_dates: [], fresh: true, first_published_at: '2026-11-06T09:41:00+08:00', next_day: null, ...extra,
  }
}
function ready(extra: Partial<Extract<NewsCatalystsResult, { state: 'ok' }>> = {}): NewsCatalystsResult {
  return {
    state: 'ok', requested_date: '2026-11-08', trade_date: '2026-11-06', fetched_at: '2026-11-07T08:00:00+08:00', live: false,
    quote_state: 'ok', quote_asof: null, next_date: '2026-11-09', lookback: { days: 10, cached: 10 }, counts: { stocks: 3, limit_up: 3 },
    groups: [
      group('机器人', ['600001.SH', '000002.SZ', '300003.SZ'], { next_day: { date: '2026-11-09', quote_count: 3, avg_change_pct: 0.031, rising_count: 2, limit_up_count: 1 } }),
      group('减速器', ['600001.SH', '300003.SZ'], { batch: false, fresh: false, seen_dates: ['2026-11-04', '2026-11-05'] }),
    ],
    items: [item('600001.SH', '甲', 0.1, ['机器人', '减速器']), item('000002.SZ', '乙', 0.1, ['机器人']), item('300003.SZ', '丙', 0.04, ['机器人', '减速器'])],
    ...extra,
  }
}

async function render() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><NewsAnalysis /></QueryClientProvider></MemoryRouter>))
  await act(async () => { host.querySelector<HTMLButtonElement>('header button')!.click() })
  // 阶段查询在催化结果渲染后才发出，多等一轮。
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
const press = (label: string) => act(async () => {
  [...host.querySelectorAll<HTMLElement>('button, input')].find(el => el.textContent?.includes(label) || el.parentElement?.textContent === label)!.click()
})

beforeEach(() => {
  ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  calls.phases.mockResolvedValue({ total: 1, segments: [{ label: '主升', top_mainlines: [{ member: '机器人概念' }] }] })
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.clearAllMocks()
})

it('registers the page in navigation', () => {
  expect(extension.routes).toEqual([expect.objectContaining({ id: 'news', path: '/news' })])
  expect(extension.navigation).toEqual([expect.objectContaining({ routeId: 'news', label: '资讯分析' })])
})

it('shows batch catalysts first and opens a stock with the group as navigation list', async () => {
  calls.catalysts.mockResolvedValue(ready())
  await render()
  expect(calls.catalysts).toHaveBeenLastCalledWith('2026-11-08')
  expect(calls.phases).toHaveBeenLastCalledWith('2026-11-06', '2026-11-06')
  const text = host.textContent!
  expect(text).toContain('2026-11-08 暂无异动解读或不是交易日，显示 2026-11-06 的数据')
  expect(text).toContain('情绪阶段 主升 · 主线 机器人概念')
  expect(text).toContain('3 只异动 · 3 只涨停 · 1 个批量催化')
  expect(text).toContain('+8.00%')
  expect(text).toContain('次日 +3.10%')
  expect(text).not.toContain('近 10 日出现 2 日')

  await press('只看批量')
  expect(host.textContent).toContain('近 10 日出现 2 日')
  await press('减速器')
  expect(host.querySelector('section h2')!.textContent).toBe('减速器')
  expect([...host.querySelectorAll('section li > div > button')].map(b => b.textContent)).toEqual(['甲', '丙'])
  await press('丙')
  const opened = host.querySelector<HTMLElement>('[data-stock]')!
  expect(opened.textContent).toBe('300003.SZ · 2026-11-06')
  expect(opened.dataset.nav).toBe('600001.SH,300003.SZ')
})

it('explains missing quotes, missing source and upstream failure', async () => {
  calls.catalysts.mockResolvedValue(ready({ quote_state: 'unavailable' }))
  await render()
  expect(host.querySelector('[role=alert]')!.textContent).toContain('本地缺少该交易日的日线行情')

  act(() => root.unmount())
  root = createRoot(host)
  calls.catalysts.mockResolvedValue({ state: 'source_unavailable', requested_date: '2026-11-08', trade_date: '2026-11-06' })
  await render()
  expect(host.querySelector('[role=alert] a')!.getAttribute('href')).toBe('/settings?tab=data-sources&highlight=fuyao')

  act(() => root.unmount())
  root = createRoot(host)
  calls.catalysts.mockResolvedValue({ state: 'no_data', requested_date: '2026-11-08', trade_date: '2026-11-06', message: '未取得该交易日的异动解读' })
  await render()
  expect(host.querySelector('[role=alert]')!.textContent).toContain('未取得该交易日的异动解读')
  expect(calls.phases).toHaveBeenCalled()
})
