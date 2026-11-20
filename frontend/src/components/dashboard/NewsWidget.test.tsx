// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, type MeozNews } from '@/lib/api'
import { meozNewsOptions } from '@/lib/useMeozWidgets'
import { NewsWidget } from './NewsWidget'

const calls = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { meozNews: calls.get } }
})

const row = (o: Partial<MeozNews['rows'][number]>): MeozNews['rows'][number] => ({
  id: 'x:1', published_at: null, display_at: '2026-11-20T21:01:05+08:00', time_label: '21:01',
  source: 'yicai_home', source_name: '第一财经头条', title: '标题一', summary: null,
  url: 'https://www.yicai.com/news/1.html', item_type: 'link', ...o,
})

const data: MeozNews = {
  fetched_at: '2026-11-20T21:05:00+08:00',
  sources: [{ source: 'yicai_home', source_name: '第一财经头条' }, { source: 'cls_depth_1000', source_name: '财联社头条' }],
  rows: [
    row({ id: 'yicai_home:1', title: '逼近20万亿元', summary: '财政数据摘要内容' }),
    row({ id: 'cls_depth_1000:2', source: 'cls_depth_1000', source_name: '财联社头条', title: '财联社新闻', time_label: '20:59',
      url: 'javascript:alert(1)' }),
    row({ id: 'cls_depth_1000:3', source: 'cls_depth_1000', source_name: '财联社头条', title: '无链接新闻', time_label: '20:50', url: null }),
  ],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render(props: Partial<Parameters<typeof NewsWidget>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSourceChange = vi.fn()
  const draw = async (p: typeof props) => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={qc}>
          <NewsWidget isLatestDate onSourceChange={onSourceChange} {...p} />
        </QueryClientProvider>,
      )
    })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  await draw(props)
  const refetch = async () => {
    await act(async () => { await qc.refetchQueries() })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  return { onSourceChange, rerender: draw, refetch, qc }
}

const clickText = async (text: string) => {
  const el = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.trim() === text)!
  await act(async () => { el.click() })
}

it('renders time label, source name and title; safe links open in a new tab', async () => {
  calls.get.mockResolvedValue(data)
  await render()
  expect(calls.get).toHaveBeenCalledWith(50)
  const text = host.textContent ?? ''
  expect(text).toContain('21:01')
  expect(text).toContain('逼近20万亿元')
  expect(text).toContain('财联社新闻')
  // 时间与来源同列: 时间在上、来源在下, 标题在右侧另起一列
  const meta = host.querySelector('[data-testid="news-meta"]')!
  const parts = Array.from(meta.children).map(c => c.textContent)
  expect(parts).toEqual(['21:01', '第一财经头条'])
  expect(meta.textContent).not.toContain('逼近20万亿元')
  expect(meta.parentElement!.className).toContain('grid-cols-[3.5rem_1fr_auto]')
  const a = host.querySelector('a')!
  expect(a.getAttribute('href')).toBe('https://www.yicai.com/news/1.html')
  expect(a.getAttribute('target')).toBe('_blank')
  expect(a.getAttribute('rel')).toBe('noopener noreferrer')
})

it('renders the title as plain text (no anchor) for javascript: or empty url', async () => {
  calls.get.mockResolvedValue(data)
  await render()
  expect(host.querySelectorAll('a')).toHaveLength(1)
  expect(host.textContent).toContain('财联社新闻')
  expect(host.textContent).toContain('无链接新闻')
  expect(host.innerHTML).not.toContain('javascript:')
})

it('builds source tabs from sources, filters, and reports the change', async () => {
  calls.get.mockResolvedValue(data)
  const { onSourceChange } = await render()
  const tabs = Array.from(host.querySelectorAll('[role="tab"]')).map(t => t.textContent)
  expect(tabs).toEqual(['全部', '第一财经头条', '财联社头条'])
  await clickText('财联社头条')
  expect(onSourceChange).toHaveBeenCalledWith('cls_depth_1000')
})

it('filters by the given source and falls back to all when it is unknown', async () => {
  calls.get.mockResolvedValue(data)
  await render({ source: 'cls_depth_1000' })
  expect(host.textContent).toContain('财联社新闻')
  expect(host.textContent).not.toContain('逼近20万亿元')

  act(() => root.unmount()); root = createRoot(host)
  await render({ source: 'gone_source' })
  expect(host.textContent).toContain('逼近20万亿元')
  expect(host.textContent).toContain('财联社新闻')
  expect(host.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('全部')
})

it('shows an expand arrow only for rows with a summary and toggles the summary', async () => {
  calls.get.mockResolvedValue(data)
  await render()
  const arrows = host.querySelectorAll('[data-testid="news-expand"]')
  expect(arrows).toHaveLength(1)
  expect(host.textContent).not.toContain('财政数据摘要内容')
  await act(async () => { (arrows[0] as HTMLElement).click() })
  expect(host.textContent).toContain('财政数据摘要内容')
  await act(async () => { (host.querySelector('[data-testid="news-expand"]') as HTMLElement).click() })
  expect(host.textContent).not.toContain('财政数据摘要内容')
})

it('history date: shows 新闻仅实时数据 and never calls the API', async () => {
  calls.get.mockResolvedValue(data)
  await render({ isLatestDate: false })
  expect(host.textContent).toContain('新闻仅实时数据')
  expect(calls.get).not.toHaveBeenCalled()
  expect(host.textContent).not.toContain('逼近20万亿元')
})

it('history date never shows cached live rows after switching away', async () => {
  calls.get.mockResolvedValue(data)
  const { rerender } = await render()
  expect(host.textContent).toContain('逼近20万亿元')
  await rerender({ isLatestDate: false })
  expect(host.textContent).toContain('新闻仅实时数据')
  expect(host.textContent).not.toContain('逼近20万亿元')
})

it('failure shows 来源不可用 and empty shows 暂无新闻 (different UIs)', async () => {
  calls.get.mockRejectedValue(new ApiError('x', 503))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('暂无新闻')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockResolvedValue({ ...data, rows: [], sources: [] })
  await render()
  expect(host.textContent).toContain('暂无新闻')
  expect(host.textContent).not.toContain('来源不可用')
})

it('after a first failure, a refetch in flight keeps 来源不可用 instead of 加载中', async () => {
  calls.get.mockRejectedValue(new ApiError('x', 503))
  const { qc } = await render()
  expect(host.textContent).toContain('来源不可用')
  let release!: (e: unknown) => void
  calls.get.mockReturnValue(new Promise((_, reject) => { release = reject }))
  let pending!: Promise<unknown>
  act(() => { pending = qc.refetchQueries() })
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('加载中')
  release(new ApiError('x', 503))
  await pending
})

it('keeps last good rows with a stale hint when a refresh fails', async () => {
  calls.get.mockResolvedValue(data)
  const { refetch } = await render()
  expect(host.textContent).not.toContain('过期')
  calls.get.mockRejectedValue(new ApiError('x', 502))
  await refetch()
  expect(host.textContent).toContain('逼近20万亿元')
  expect(host.textContent).toContain('(过期)')
})

it('polls every 60s only when enabled', () => {
  expect(meozNewsOptions(true).refetchInterval).toBe(60_000)
  expect(meozNewsOptions(true).enabled).toBe(true)
  expect(meozNewsOptions(false).enabled).toBe(false)
})
