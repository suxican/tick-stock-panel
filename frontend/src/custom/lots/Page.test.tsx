// @vitest-environment jsdom
import { act, Suspense } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import extension from './extension'

const mocks = vi.hoisted(() => ({ lots: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  lotsList: (...args: unknown[]) => mocks.lots(...args),
  lotDelete: vi.fn(),
} }))
vi.mock('./PositionTMonitor', () => ({ PositionTMonitor: () => <div>持仓做 T 监控</div> }))
vi.mock('./HoldingAnalysis', () => ({ HoldingAnalysis: () => <div>持仓研究工作区</div> }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.lots.mockReset().mockResolvedValue({ lots: [] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

it('opens the registered lots route with position T and retains access to batch reminders', async () => {
  const route = extension.routes![0]
  const Page = route.component
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/lots']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <QueryClientProvider client={client}>
          <Suspense fallback={<div>加载中</div>}>
            <Routes><Route path={route.path} element={<Page />} /></Routes>
          </Suspense>
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await import('./Page')
  })
  await settle()
  expect(host.textContent).toContain('持仓做 T 监控')
  expect(host.textContent).not.toContain('新增批次')
  expect(mocks.lots).not.toHaveBeenCalled()

  await click('批次提醒')
  expect(host.textContent).toContain('还没有批次')
  expect(host.textContent).toContain('新增批次')
  expect(host.textContent).not.toContain('持仓做 T 监控')
  expect(mocks.lots).toHaveBeenCalledTimes(1)
  expect(host.querySelector('button[aria-current="page"]')?.textContent).toBe('批次提醒')

  await click('持仓分析与计划')
  expect(host.textContent).toContain('持仓研究工作区')
  expect(host.textContent).not.toContain('新增批次')
  expect(host.textContent).not.toContain('持仓做 T 监控')

  await click('持仓做 T')
  expect(host.textContent).toContain('持仓做 T 监控')
  expect(host.textContent).not.toContain('新增批次')
  expect(host.querySelector('button[aria-current="page"]')?.textContent).toBe('持仓做 T')
})
