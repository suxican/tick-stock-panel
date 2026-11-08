// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { WatchlistAnalysisDialog } from './WatchlistAnalysisDialog'

const store = vi.hoisted(() => ({
  reports: [
    { id: 'a', symbol: '600519.SH', created_at: '2026-11-06T10:00:00', summary: '茅台历史分析', focus: '' },
    { id: 'b', symbol: '000001.SZ', created_at: '2026-11-05T10:00:00', summary: '平安历史分析', focus: '支撑' },
  ],
  load: vi.fn(), open: vi.fn(), findToday: vi.fn(), start: vi.fn(),
}))
vi.mock('@/lib/stockAnalysisStore', () => ({
  useHistoryReports: () => ({ reports: store.reports, loaded: true }),
  loadHistory: store.load, openHistoryReport: store.open,
  findTodayReport: store.findToday, startAnalysis: store.start,
}))
vi.mock('./StockAnalysisBoard', () => ({ StockAnalysisBoard: ({ symbol }: { symbol: string }) => <div>价位 {symbol}</div> }))

let host: HTMLDivElement, root: Root
const close = vi.fn()
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(cb, 0))
  vi.stubGlobal('cancelAnimationFrame', clearTimeout)
  store.load.mockReset().mockResolvedValue(true); store.open.mockClear(); close.mockClear()
  store.findToday.mockReset().mockResolvedValue(null)
  store.start.mockReset().mockResolvedValue({ id: 'task-a' })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals() })
async function render(symbol = '600519.SH') {
  await act(async () => root.render(<WatchlistAnalysisDialog key={symbol} symbol={symbol} name="测试股票" onClose={close} />))
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.includes(label))!
  expect(button).toBeTruthy()
  await act(async () => button.click())
}

it('opens the selected stock board and only its history, then hands the report to the shared viewer', async () => {
  await render()
  expect(host.querySelector('[role=dialog]')).not.toBeNull()
  expect(host.textContent).toContain('价位 600519.SH')
  expect(store.open).not.toHaveBeenCalled()
  await click('历史报告')
  expect(host.textContent).toContain('茅台历史分析')
  expect(host.textContent).not.toContain('平安历史分析')
  await click('茅台历史分析')
  expect(store.open).toHaveBeenCalledWith('a')
  expect(close).toHaveBeenCalledOnce()
})

it('resets to the board when changing stocks or reopening', async () => {
  await render(); await click('历史报告'); await render('000001.SZ')
  expect(host.textContent).toContain('价位 000001.SZ')
  expect(host.textContent).not.toContain('茅台历史分析')
  await click('历史报告')
  expect(host.textContent).toContain('平安历史分析')
})

it('shows an explicit empty state for a stock with no history', async () => {
  await render('600000.SH'); await click('历史报告')
  expect(host.textContent).toContain('暂无该标的的历史分析报告')
})

it('shows history loading and allows retry after failure without hiding the board', async () => {
  let resolve!: (ok: boolean) => void
  store.load.mockImplementationOnce(() => new Promise<boolean>(r => { resolve = r }))
  await render()
  expect(host.textContent).toContain('价位 600519.SH')
  await click('历史报告')
  expect(host.textContent).toContain('正在加载历史报告')
  await act(async () => resolve(false))
  expect(host.textContent).toContain('历史报告加载失败')
  await click('重试')
  expect(store.load).toHaveBeenCalledTimes(2)
  expect(host.textContent).toContain('茅台历史分析')
})

it('closes with Escape and the close button', async () => {
  await render()
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(close).toHaveBeenCalledOnce()
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="关闭关键分析"]')!.click())
  expect(close).toHaveBeenCalledTimes(2)
})

it('starts AI analysis for the current stock only after clicking and hands off to the existing viewer', async () => {
  await render()
  expect(store.start).not.toHaveBeenCalled()
  await click('AI 个股分析')
  expect(store.findToday).toHaveBeenCalledWith('600519.SH')
  expect(store.start).toHaveBeenCalledWith('600519.SH', '测试股票')
  expect(close).toHaveBeenCalledOnce()
})

it('offers the existing report before generating again', async () => {
  store.findToday.mockResolvedValue(store.reports[0])
  await render(); await click('AI 个股分析')
  expect(store.start).not.toHaveBeenCalled()
  expect(host.textContent).toContain('今日已有分析报告')
  await click('查看今日报告')
  expect(store.open).toHaveBeenCalledWith('a')
  expect(close).toHaveBeenCalledOnce()
  expect(store.start).not.toHaveBeenCalled()
})

it('allows explicitly regenerating despite an existing report', async () => {
  store.findToday.mockResolvedValue(store.reports[0])
  await render(); await click('AI 个股分析'); await click('重新分析')
  expect(store.start).toHaveBeenCalledWith('600519.SH', '测试股票')
  expect(close).toHaveBeenCalledOnce()
})

it('keeps the board open and shows a recoverable error if starting is rejected', async () => {
  store.start.mockResolvedValueOnce({ error: '同时进行的个股分析任务不能超过 3 个' })
  await render(); await click('AI 个股分析')
  expect(close).not.toHaveBeenCalled()
  expect(host.querySelector('[role=alert]')?.textContent).toContain('不能超过 3 个')
  expect(host.textContent).toContain('价位 600519.SH')
  await click('AI 个股分析')
  expect(close).toHaveBeenCalledOnce()
})

it('disables duplicate clicks while checking history', async () => {
  let resolve!: (value: null) => void
  store.findToday.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  await render(); await click('AI 个股分析')
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.includes('AI 个股分析'))!
  expect(button.disabled).toBe(true)
  await act(async () => button.click())
  expect(store.findToday).toHaveBeenCalledOnce()
  await act(async () => resolve(null))
  expect(store.start).toHaveBeenCalledOnce()
})

it('does not start an old stock analysis after the dialog is closed or replaced', async () => {
  let resolve!: (value: null) => void
  store.findToday.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  await render(); await click('AI 个股分析'); await render('000001.SZ')
  await act(async () => resolve(null))
  expect(store.start).not.toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
})

it('reports unexpected failures and allows another attempt', async () => {
  store.findToday.mockRejectedValueOnce(new Error('offline'))
  await render(); await click('AI 个股分析')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('无法启动分析，请重试')
  expect(store.start).not.toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
  await click('AI 个股分析')
  expect(store.start).toHaveBeenCalledOnce()
})
