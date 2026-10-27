// @vitest-environment jsdom
import { beforeAll, expect, it, vi } from 'vitest'

let registry: typeof import('./registry')

beforeAll(async () => {
  vi.resetModules()
  const { initializeFrontendExtensions } = await import('./bootstrap')
  registry = await import('./registry')

  await initializeFrontendExtensions()
  registry.finalizeFrontendExtensions(new Set(['/watchlist', '/regime', '/settings']))
}, 60_000)

it('loads the remaining menus without the retired market emotion route', () => {
  expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  const routes = registry.getFrontendExtensionRoutes()
  const navigation = registry.getFrontendExtensionNavigation()
  expect(routes.some(route => route.path === '/market-emotion')).toBe(false)
  expect(navigation.some(item => item.label === '市场情绪')).toBe(false)
  for (const path of ['/limit-ladder', '/first-board', '/market-game']) {
    expect(routes.some(route => route.path === path)).toBe(true)
    expect(navigation.some(item => item.route.path === path)).toBe(true)
  }
})
