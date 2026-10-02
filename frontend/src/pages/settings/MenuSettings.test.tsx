// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { DragEndEvent } from '@dnd-kit/core'
import extension from '@/custom/kaipanla/extension'
import { SettingsMenuSettingsPanel } from './MenuSettings'

const calls = vi.hoisted(() => ({
  preferences: vi.fn(), menus: vi.fn(), hidden: vi.fn(), order: vi.fn(),
  dragEnd: undefined as undefined | ((event: DragEndEvent) => void),
}))
vi.mock('@/lib/api', () => ({ api: {
  preferences: calls.preferences, analysisMenus: calls.menus,
  saveNavHidden: calls.hidden, saveNavOrder: calls.order,
} }))
vi.mock('@/extensions/registry', () => ({ getFrontendExtensionNavigation: () =>
  extension.navigation!.map(item => ({ ...item, route: extension.routes!.find(route => route.id === item.routeId)! })),
}))
// Drive the drag-end boundary without relying on jsdom's absent layout measurements.
vi.mock('@dnd-kit/core', async importOriginal => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return { ...actual, DndContext: ({ children, onDragEnd }: {
    children: ReactNode; onDragEnd: (event: DragEndEvent) => void
  }) => { calls.dragEnd = onDragEnd; return <>{children}</> } }
})

let host: HTMLDivElement
let root: Root
let client: QueryClient
let prefs: { nav_order: string[]; nav_hidden: string[] }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  prefs = { nav_order: [], nav_hidden: [] }
  calls.preferences.mockReset().mockImplementation(async () => ({ ...prefs }))
  calls.menus.mockReset().mockResolvedValue({ items: [{ id: 'custom-report', label: '自定义分析', visible: true }] })
  calls.hidden.mockReset().mockImplementation(async (hidden: string[]) => { prefs = { ...prefs, nav_hidden: hidden }; return {} })
  calls.order.mockReset().mockImplementation(async (order: string[]) => { prefs = { ...prefs, nav_order: order }; return {} })
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
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter>
    <SettingsMenuSettingsPanel />
  </MemoryRouter></QueryClientProvider>))
  await settle()
}
function row(label = '市场情绪') {
  const title = [...host.querySelectorAll('span')].find(item => item.textContent === label)!
  expect(title).toBeDefined()
  return title.parentElement!.parentElement!
}
function ids() {
  return [...host.querySelectorAll<HTMLAnchorElement>('a[title="打开页面"], a[title="编辑扩展页面"]')]
    .map(link => link.closest('.grid')!.querySelector('.font-mono')!.textContent)
}

it('includes registered 市场情绪 after analysis menus and opens its actual route', async () => {
  await render()
  expect(ids().slice(-2)).toEqual(['custom-report', '/market-emotion'])
  expect(ids().filter(id => id === '/market-emotion')).toHaveLength(1)
  expect(row().querySelector('a')?.getAttribute('href')).toBe('/market-emotion')
  expect(row().querySelector('a')?.title).toBe('打开页面')
  expect(row().textContent).toContain('扩展')
  expect(row('自定义分析').querySelector('a')?.getAttribute('href')).toBe('/settings?tab=ext-pages')
})

it('retains saved extension order and ignores stale menu ids', async () => {
  prefs.nav_order = ['/market-emotion', '/', 'removed-extension', 'custom-report']
  await render()
  expect(ids()[0]).toBe('/market-emotion')
  expect(ids()).not.toContain('removed-extension')
  expect(ids().filter(id => id === '/market-emotion')).toHaveLength(1)
})

it('saves extension visibility by route path and preserves other hidden menus', async () => {
  prefs.nav_hidden = ['/review']
  await render()
  await act(async () => row().querySelector<HTMLButtonElement>('button[title="隐藏"]')!.click())
  await settle()
  expect(calls.hidden).toHaveBeenLastCalledWith(['/review', '/market-emotion'])
  expect(row().textContent).toContain('已隐藏')
  await act(async () => row().querySelector<HTMLButtonElement>('button[title="显示"]')!.click())
  await settle()
  expect(calls.hidden).toHaveBeenLastCalledWith(['/review'])
  expect(row().textContent).not.toContain('已隐藏')
})

it('persists a dragged extension alongside builtin and analysis menu ids', async () => {
  await render()
  const original = ids()
  await act(async () => calls.dragEnd!({ active: { id: '/market-emotion' }, over: { id: '/' } } as DragEndEvent))
  await settle()
  expect(calls.order).toHaveBeenLastCalledWith(['/market-emotion', ...original.filter(id => id !== '/market-emotion')])
  expect(ids()[0]).toBe('/market-emotion')
})
