// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { NewsAnnouncementRow, NewsAnnouncementsResult } from '@/lib/api'
import { NewsAnalysis } from './Page'

const calls = vi.hoisted(() => ({ announcements: vi.fn(), sync: vi.fn(), catalysts: vi.fn() }))
vi.mock('@/lib/api', async actual => ({
  ...await actual<typeof import('@/lib/api')>(),
  api: { newsAnnouncements: calls.announcements, syncNewsAnnouncements: calls.sync, newsCatalysts: calls.catalysts },
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, tradeDate, navList }: { symbol?: string; tradeDate?: string; navList?: { symbol: string }[] }) =>
    symbol ? <output data-stock data-nav={navList?.map(s => s.symbol).join(',')}>{symbol} · {tradeDate}</output> : null,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (value: string) => void }) => <button onClick={() => onChange('2026-11-10')}>选取历史日</button>,
}))

let host: HTMLDivElement
let root: Root

function row(id: string, symbol: string, name: string, type: string, label: string, level: NewsAnnouncementRow['level'], at: string, hasTime = true): NewsAnnouncementRow {
  return { id, symbol, name, title: name + label + '公告', published_at: at, has_time: hasTime, url: `https://static.cninfo.com.cn/${id}.PDF`, type, label, level }
}
function result(extra: Partial<NewsAnnouncementsResult> = {}): NewsAnnouncementsResult {
  const rows = [
    row('1', '600519.SH', '茅台', 'inquiry', '监管问询', 'high', '2026-11-10T00:00:00+08:00', false),
    row('2', '300033.SZ', '同花顺', 'reduce_plan', '减持计划', 'high', '2026-11-06T18:20:00+08:00'),
    row('3', '300033.SZ', '同花顺', 'abnormal', '异常波动', 'medium', '2026-11-05T17:00:00+08:00'),
  ]
  return {
    date: '2026-11-10', start: '2026-10-14', sync: null, symbols: 3, missing: [], partial: [],
    types: [{ type: 'inquiry', label: '监管问询', level: 'high' }, { type: 'reduce_plan', label: '减持计划', level: 'high' },
      { type: 'abnormal', label: '异常波动', level: 'medium' }, { type: 'loss', label: '业绩预亏', level: 'medium' }],
    counts: { inquiry: 1, reduce_plan: 1, abnormal: 1, loss: 0 }, rows, ...extra,
  }
}

const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
const button = (label: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.startsWith(label))!

async function render() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(<MemoryRouter><QueryClientProvider client={client}><NewsAnalysis /></QueryClientProvider></MemoryRouter>))
  await act(async () => { host.querySelector<HTMLButtonElement>('header button')!.click() })
  await act(async () => { button('公告排雷').click() })
  await flush()
  await flush()
}

beforeEach(() => {
  ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  calls.catalysts.mockResolvedValue({ state: 'no_data', requested_date: '2026-11-10', trade_date: '2026-11-10', message: 'x' })
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.clearAllMocks()
})

it('lists watchlist risks, filters by type and opens the stock on its announcement date', async () => {
  calls.announcements.mockResolvedValue(result())
  await render()
  expect(calls.announcements).toHaveBeenLastCalledWith('2026-11-10')
  const text = host.textContent!
  expect(text).toContain('自选 3 只 · 2026-10-14 至 2026-11-10 · 风险公告 3 条')
  // 计数为 0 的类型不显示
  expect([...host.querySelectorAll('[role=group] button')].map(b => b.textContent)).toEqual(['全部 3', '监管问询 1', '减持计划 1', '异常波动 1'])
  const times = [...host.querySelectorAll('li time')].map(t => t.textContent)
  expect(times).toEqual(['11-10', '11-06 18:20', '11-05 17:00'])
  expect(host.querySelector('li a')!.getAttribute('href')).toBe('https://static.cninfo.com.cn/1.PDF')

  await act(async () => { button('减持计划').click() })
  expect([...host.querySelectorAll('li a')].map(a => a.textContent)).toEqual(['同花顺减持计划公告'])
  await act(async () => { button('全部').click() })
  await act(async () => { button('同花顺').click() })
  const opened = host.querySelector<HTMLElement>('[data-stock]')!
  expect(opened.textContent).toBe('300033.SZ · 2026-11-06')
  expect(opened.dataset.nav).toBe('600519.SH,300033.SZ')
})

it('starts a sync, polls while running and warns about uncovered symbols', async () => {
  calls.announcements.mockResolvedValue(result({ rows: [], counts: {}, missing: ['600519.SH'], partial: ['300033.SZ'] }))
  calls.sync.mockResolvedValue({ started: true, message: '开始同步' })
  await render()
  expect(host.querySelector('[role=alert]')!.textContent).toContain('1 只尚未取得公告，1 只未覆盖完整回看期')
  calls.announcements.mockResolvedValue(result({ sync: { state: 'running', message: '', done: 1, total: 2, failed: 0 } }))
  await act(async () => { button('同步公告').click() })
  await flush()
  expect(calls.sync).toHaveBeenCalledWith('2026-11-10')
  expect(host.textContent).toContain('正在同步 1/2')
  expect(button('正在同步').disabled).toBe(true)
})

it('explains an empty watchlist and a failed sync', async () => {
  calls.announcements.mockResolvedValue(result({ symbols: 0, rows: [] }))
  await render()
  expect(host.querySelector('a[href="/watchlist"]')).not.toBeNull()

  act(() => root.unmount())
  root = createRoot(host)
  calls.announcements.mockResolvedValue(result({ sync: { state: 'error', message: '巨潮暂时无法访问，请稍后重试。', done: 0, total: 3, failed: 0 } }))
  await render()
  expect(host.querySelector('[role=alert]')!.textContent).toContain('巨潮暂时无法访问')
})
