// @vitest-environment node
import { beforeEach, expect, it, vi } from 'vitest'
import extension from './extension'

beforeEach(() => vi.resetModules())

it('registers the migrated page through the current static route and navigation registry', async () => {
  const registry = await import('@/extensions/registry')
  await registry.loadFrontendExtensions({ './market-game/extension.tsx': async () => ({ default: extension }) })
  registry.finalizeFrontendExtensions(new Set(['/overview', '/settings']))
  expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  expect(registry.getFrontendExtensionRoutes()).toMatchObject([{ path: '/market-game', extensionId: 'market-game.workbench' }])
  expect(registry.getFrontendExtensionNavigation()).toMatchObject([{ label: '超短线博弈', route: { path: '/market-game' } }])
})

it('disables only this extension if its path conflicts with a future core route', async () => {
  const registry = await import('@/extensions/registry')
  await registry.loadFrontendExtensions({ './market-game/extension.tsx': async () => ({ default: extension }) })
  registry.finalizeFrontendExtensions(new Set(['/market-game']))
  expect(registry.getFrontendExtensionRoutes()).toEqual([])
  expect(registry.getFrontendExtensionNavigation()).toEqual([])
  expect(registry.getFrontendExtensionLoadErrors()[0]).toMatchObject({ extensionId: 'market-game.workbench', error: '试图覆盖核心路由 /market-game' })
})

it('keeps route access when navigation is hidden and isolates an incompatible extension', async () => {
  const registry = await import('@/extensions/registry')
  await registry.loadFrontendExtensions({
    './hidden': async () => ({ default: { ...extension, navigation: [] } }),
    './incompatible': async () => ({ default: { ...extension, id: 'unsupported.extension', apiVersion: 2 } as unknown as typeof extension }),
  })
  registry.finalizeFrontendExtensions(new Set())
  expect(registry.getFrontendExtensionRoutes()).toHaveLength(1)
  expect(registry.getFrontendExtensionNavigation()).toEqual([])
  expect(registry.getFrontendExtensionLoadErrors()[0].extensionId).toBe('unsupported.extension')
})
