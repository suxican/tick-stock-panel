// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, type MeozHotRank } from '@/lib/api'
import { HotSearchWidget } from './HotSearchWidget'

const calls = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { meozHotRank: calls.get } }
})

const hot: MeozHotRank = {
  fetched_at: '2026-11-20T10:35:00',
  stocks: {
    ths_normal: { available: true, rows: [
      { symbol: '600000', rank: 1, name: '浦发银行', pct_chg: 0.1 },
      { symbol: '000001', rank: 2, name: '平安银行', pct_chg: -0.0123 },
    ] },
    dfcf_renqi: { available: true, rows: [{ symbol: '300750', rank: 1, name: '宁德时代', pct_chg: 0.02 }] },
    ths_trend: { available: false, rows: [] },
  },
  themes: { available: true, rows: [
    { rank: 1, theme_id: 't1', name: '机器人概念', available_at: '2026-11-20T10:30:00' },
  ] },
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render(props: Partial<Parameters<typeof HotSearchWidget>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onKindChange = vi.fn()
  const onOpenStock = vi.fn()
  const draw = async (p: typeof props) => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={qc}>
          <HotSearchWidget isLatestDate onKindChange={onKindChange} onOpenStock={onOpenStock} {...p} />
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
  return { onKindChange, onOpenStock, rerender: draw, refetch, qc }
}

function button(text: string) {
  return Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(text))!
}

it('shows the THS popularity list by default and opens stocks with the visible list', async () => {
  calls.get.mockResolvedValue(hot)
  const { onOpenStock } = await render()
  const text = host.textContent ?? ''
  expect(text).toContain('浦发银行')
  expect(text).toContain('+10.00%')
  expect(text).toContain('-1.23%')
  // 与成交额榜一致: 名称下一行显示代码 (热搜接口没有题材, 此处不展示题材)
  const row = button('浦发银行')
  expect(row.textContent).toContain('600000')
  expect(row.querySelector('.font-mono.text-\\[9px\\]')?.textContent).toBe('600000')
  await act(async () => { button('浦发银行').click() })
  expect(onOpenStock).toHaveBeenCalledWith('600000', '浦发银行', [
    { symbol: '600000', name: '浦发银行' }, { symbol: '000001', name: '平安银行' },
  ])
})

it('reports kind changes and renders themes without a pct column', async () => {
  calls.get.mockResolvedValue(hot)
  const { onKindChange } = await render()
  await act(async () => { button('东财人气').click() })
  expect(onKindChange).toHaveBeenCalledWith('dfcf_renqi')

  act(() => root.unmount()); root = createRoot(host)
  await render({ kind: 'themes' })
  const text = host.textContent ?? ''
  expect(text).toContain('机器人概念')
  expect(text).not.toContain('%')
})

it('renders themes as card rows with the top three ranks emphasised', async () => {
  calls.get.mockResolvedValue({
    ...hot,
    themes: { available: true, rows: [1, 2, 3, 4].map(rank => ({
      rank, theme_id: `t${rank}`, name: `题材${rank}`, available_at: '2026-11-20T10:30:00',
    })) },
  })
  await render({ kind: 'themes' })
  const ranks = Array.from(host.querySelectorAll('[data-testid="theme-rank"]'))
  expect(ranks.map(e => e.textContent)).toEqual(['1', '2', '3', '4'])
  expect(ranks.map(e => e.className.includes('text-bull'))).toEqual([true, true, true, false])
  // 行版式与个股榜一致 (卡片行, 同一套网格列)
  const row = ranks[0].parentElement!
  expect(row.className).toContain('grid-cols-[18px_1fr]')
  expect(row.className).toContain('bg-elevated/40')
  expect(row.textContent).toContain('题材1')
})

it('distinguishes an unavailable source from an empty list', async () => {
  calls.get.mockResolvedValue(hot)
  await render({ kind: 'ths_trend' })
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('暂无')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockResolvedValue({ ...hot, stocks: { ...hot.stocks, ths_normal: { available: true, rows: [] } } })
  await render()
  expect(host.textContent).toContain('暂无数据')
  expect(host.textContent).not.toContain('来源不可用')
})

it('shows source unavailable when the whole request failed (null data)', async () => {
  calls.get.mockRejectedValue(new ApiError('未配置猫爪', 503))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('暂无')
})

it('does not fetch while the dashboard shows a past date', async () => {
  await render({ isLatestDate: false })
  expect(calls.get).not.toHaveBeenCalled()
  expect(host.textContent).toContain('热搜仅实时数据')
})

it('falls back to ths_normal for an unknown saved kind', async () => {
  calls.get.mockResolvedValue(hot)
  await render({ kind: 'bogus' })
  expect(host.textContent).toContain('浦发银行')
})

it('after a first failure, a refetch in flight keeps 来源不可用 instead of 加载中', async () => {
  calls.get.mockRejectedValue(new ApiError('未配置猫爪', 503))
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

it('keeps the last good list with a stale hint when a refresh gets a quiet 503', async () => {
  calls.get.mockResolvedValue(hot)
  const { refetch } = await render()
  expect(host.textContent).not.toContain('过期')
  calls.get.mockRejectedValue(new ApiError('上游异常', 503))
  await refetch()
  expect(host.textContent).toContain('浦发银行')
  expect(host.textContent).toContain('(过期)')
  expect(host.textContent).not.toContain('来源不可用')
})

it('keeps the last good list with a stale hint when a refresh errors loudly', async () => {
  calls.get.mockResolvedValue(hot)
  const { refetch } = await render()
  calls.get.mockRejectedValue(new Error('boom'))
  await refetch()
  expect(host.textContent).toContain('浦发银行')
  expect(host.textContent).toContain('(过期)')
})

it('does not let a first-load failure be masked, and never shows cached live data on a past date', async () => {
  calls.get.mockRejectedValue(new ApiError('上游异常', 502))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('过期')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockResolvedValue(hot)
  const { rerender } = await render()
  expect(host.textContent).toContain('浦发银行')
  await rerender({ isLatestDate: false })
  expect(host.textContent).toContain('热搜仅实时数据')
  expect(host.textContent).not.toContain('浦发银行')
})
