// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ThemeRadar } from './ThemeRadar'
import extension from './extension'

const calls = vi.hoisted(() => ({ research: vi.fn(), themes: vi.fn(), sync: vi.fn(), status: vi.fn() }))
vi.mock('@/lib/api', async actual => ({
  ...await actual<typeof import('@/lib/api')>(),
  api: { zzshareResearch: calls.research, zzshareThemes: calls.themes, syncZzshareThemes: calls.sync, quoteStatus: calls.status },
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, tradeDate }: { symbol?: string; tradeDate?: string }) =>
    symbol ? <output data-stock>{symbol} · {tradeDate}</output> : null,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (value: string) => void }) => <button onClick={() => onChange('2025-02-05')}>选取历史日</button>,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const observed = '2025-02-05'
const leader = { symbol: '600000.SH', name: '样例龙头', change_pct: 5.5, amount_yuan: 1e8 }
function model(date: string, ready = false) {
  return { date, state: ready ? 'ready' : 'idle', scope: '指定日期热度候选', message: '', coverage: { candidates: 10, completed: 10, eligible: 1 }, progress: { done: 10, total: 10 }, computed_at: null, quote_asof: date,
    rows: ready ? [{ plate_code: 'A1', plate_name: '候选内排名第一', score: 73.2, member_count: 10, valid_count: 8,
      coverage_pct: 80, avg_change_pct: 3.56, rising_pct: 80, strong_pct: 50, limit_up_count: 1, limit_down_count: 0, leader, anchor: null }] : [] }
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  calls.themes.mockImplementation(async date => model(date))
  calls.sync.mockImplementation(async date => model(date, true))
  calls.status.mockResolvedValue({ is_trading_hours: false })
  calls.research.mockImplementation(async (kind, date) => ({ kind, date, source: 'zzshare', fetched_at: '2025-02-05T15:20:00+08:00',
    stale: false, rows: kind === 'plates_rank' ? [{ plate_code: 'BP', plate_name: '供应商热榜第一', source_score: 98, change_pct: 3.6 }] : [] }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.useRealTimers() })
async function settle() {
  await act(async () => {
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(20)
    else await new Promise(resolve => setTimeout(resolve, 20))
  })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><ThemeRadar /></QueryClientProvider>))
  await settle()
}
async function click(name: string) {
  const target = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === name)
  expect(target, name).toBeDefined()
  await act(async () => target!.click())
  await settle()
}
it('保留个股插槽并注册独立入口，模型与供应商热榜分开且按选定日钻取', async () => {
  expect(extension.navigation?.find(n => n.label === '题材雷达')?.routeId).toBe(extension.routes?.[0].id)
  expect(extension.slots?.some(s => s.name === 'stock-preview.footer')).toBe(true)
  await render()
  await click('选取历史日')
  expect(host.textContent).toContain('供应商热榜第一')
  expect(calls.sync).not.toHaveBeenCalled()
  await click('计算题材排名')
  expect(calls.sync).toHaveBeenCalledWith(observed)
  expect(host.textContent).toContain('候选内排名第一')
  await click('样例龙头')
  expect(host.querySelector('[data-stock]')?.textContent).toContain('600000.SH · ' + observed)
})

it('页签按需加载，龙虎榜按所选个股独立查询席位与历史', async () => {
  calls.research.mockImplementation(async (kind, date) => ({ kind, date, source: 'zzshare', fetched_at: null, stale: kind === 'ths_hot_top',
    warning: kind === 'ths_hot_top' ? '来源延迟' : null,
    rows: kind === 'lhb_list' ? [{ trade_date: date, stock_code: '600000', stock_name: '上榜股票', symbol: '600000.SH', reason: '样本原因' }]
      : kind === 'lhb_detail' ? [{ rank: 1, side: 'buy', seat_name: '机构专用', buy_yuan: 150000000 }]
      : kind === 'lhb_stock_history' ? [{ trade_date: '2025-01-20', change_pct: -7.32 }]
      : kind === 'ths_hot_top' ? [{ rank: 1, stock_name: '热门样本', stock_code: '000001', change_pct: 5.55 }] : [] }))
  await render()
  await click('选取历史日')
  await click('行业')
  expect(calls.research).toHaveBeenLastCalledWith('plates_rank', observed, undefined, 14)
  await click('当前板块目录')
  await click('概念')
  expect(calls.research).toHaveBeenLastCalledWith('plates_list', observed, undefined, 15)
  await click('市场情绪')
  await click('日内情绪走势')
  await click('个股热榜')
  expect(host.textContent).toContain('+5.55%')
  expect(host.textContent).toContain('使用缓存')
  await click('盘后龙虎榜')
  expect(calls.research.mock.calls.map(([kind]) => kind)).not.toContain('lhb_detail')
  await click('席位')
  expect(calls.research).toHaveBeenLastCalledWith('lhb_detail', observed, '600000')
  expect(host.textContent).toContain('机构专用')
  expect(host.textContent).toContain('1.50亿元')
  await click('历史')
  expect(calls.research).toHaveBeenLastCalledWith('lhb_stock_history', observed, '600000')
  expect(host.textContent).toContain('2025-01-20')
  expect(host.textContent).not.toContain('机构专用')
  expect(new Set(calls.research.mock.calls.map(([kind]) => kind)).size).toBe(8)
})

it('今日盘中已有已完成排名时，停留五分钟才重算；历史页不触发', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-04T10:00:00+08:00'))
  calls.status.mockResolvedValue({ is_trading_hours: true })
  calls.themes.mockImplementation(async date => model(date, true))
  await render()
  expect(host.textContent).toContain('盘中每 5 分钟重算')
  await act(async () => vi.advanceTimersByTimeAsync(299000))
  expect(calls.sync).not.toHaveBeenCalled()
  await act(async () => vi.advanceTimersByTimeAsync(1010))
  expect(calls.sync).toHaveBeenCalledTimes(1)
  await click('选取历史日')
  await act(async () => vi.advanceTimersByTimeAsync(300000))
  expect(calls.sync).toHaveBeenCalledTimes(1)
})

it('错日和失败均不绘制旧榜单，仍可切换视图', async () => {
  calls.research.mockResolvedValue({ kind: 'plates_rank', date: '2001-01-01', rows: [{ plate_name: '过期榜单' }] })
  await render()
  expect(host.textContent).toContain('数据与所选日期不一致')
  expect(host.textContent).not.toContain('过期榜单')
  calls.research.mockRejectedValue(new Error('行情服务不可用'))
  await click('个股热榜')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('行情服务不可用')
})
