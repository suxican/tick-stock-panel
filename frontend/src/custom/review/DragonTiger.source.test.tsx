// @vitest-environment jsdom
// 龙虎榜卡片的来源标注: 来源名、兜底标记与原因、猫爪游资口径说明、通用的未配置提示。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DragonTigerCard } from './Page'
import type { DragonTigerPayload, DragonTigerStockItem } from '@/lib/api'

const fixtures = vi.hoisted(() => ({ payload: null as unknown }))
vi.mock('@/lib/api', () => ({
  api: { dragonTiger: vi.fn(async () => fixtures.payload) },
}))

const item: DragonTigerStockItem = {
  thscode: '600519.SH', ticker: '600519', name: '贵州茅台', change: 0.012, net_value: 93000000,
  net_rate: 0.04, buy_value: 200000000, sell_value: 107000000, range_days: 1,
}

function payload(over: Partial<DragonTigerPayload> = {}): DragonTigerPayload {
  return {
    state: 'ok', trade_date: '2026-11-18', source: 'fuyao', source_display: 'fuyao', degraded: false,
    fallback_reason: null, hot_money_basis: null,
    all: { trade_date: '2026-11-18', stock_count: 1, count: 1, stock_items: [item] },
    org: { trade_date: '2026-11-18', stock_count: 0, count: 0, stock_items: [] },
    hot_money: { trade_date: '2026-11-18', count: 1, hot_money_items: [{ name: '佛山系', buying: 2e8, rows: [item] }] },
    ...over,
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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

async function renderCard(data: DragonTigerPayload) {
  fixtures.payload = data
  await act(async () => root.render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <DragonTigerCard date="2026-11-18" onOpenStock={vi.fn()} />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 80)) })
}

async function click(predicate: (b: HTMLButtonElement) => boolean) {
  const btn = [...host.querySelectorAll('button')].find(predicate)
  expect(btn).toBeDefined()
  await act(async () => { btn!.click() })
  await act(async () => { await new Promise(r => setTimeout(r, 60)) })
}

it('shows the source name next to the title', async () => {
  await renderCard(payload({ source: 'meoz', source_display: '猫爪数据' }))
  const label = host.querySelector<HTMLElement>('[data-dt-source]')
  expect(label?.textContent).toBe('猫爪数据')
})

it('marks a fallback source and explains why on hover', async () => {
  await renderCard(payload({
    source: 'meoz', source_display: '猫爪数据', degraded: true, fallback_reason: 'fuyao：连续 3 次失败',
  }))
  const label = host.querySelector<HTMLElement>('[data-dt-source]')
  expect(label?.textContent).toBe('猫爪数据（兜底）')
  expect(label?.title).toContain('连续 3 次失败')
})

it('explains the meoz hot money basis on the hot money tab', async () => {
  await renderCard(payload({ source: 'meoz', source_display: '猫爪数据', hot_money_basis: 'meoz_seat_match' }))
  await click(b => !!b.textContent?.includes('查看三榜明细'))
  await click(b => b.textContent === '游资')
  expect(host.textContent).toContain('按猫爪游资名单匹配营业部，口径与 fuyao 不同')
})

it('omits the hot money note for fuyao data', async () => {
  await renderCard(payload())
  await click(b => !!b.textContent?.includes('查看三榜明细'))
  await click(b => b.textContent === '游资')
  expect(host.textContent).not.toContain('按猫爪游资名单匹配')
})

it('uses a source-neutral message when no source is configured', async () => {
  await renderCard({ state: 'source_unavailable' })
  expect(host.textContent).toContain('龙虎榜数据源未配置')
  expect(host.textContent).not.toContain('fuyao')
})
