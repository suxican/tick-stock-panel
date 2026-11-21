// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { AuthenticLeaders } from './AuthenticLeaders'

const calls = vi.hoisted(() => ({ leaders: vi.fn() }))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { meozThemeLeaders: calls.leaders },
}))

let host: HTMLDivElement
let root: Root
const stocks = [{ symbol: '688388.SH', name: '嘉元科技' }, { symbol: '300750.SZ', name: '宁德时代' }]

async function render(name: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}><AuthenticLeaders conceptName={name} stocks={stocks} /></QueryClientProvider>)
  })
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.leaders.mockReset().mockResolvedValue({ themes: [
    { theme_id: '801004k', theme_name: '锂 电池', change_pct: -0.0345, symbols: ['688388', '600000'] },
  ] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('同名概念显示最正宗股, 名称取自成分, 不在成分内的显示代码', async () => {
  await render('锂电池')
  const text = host.textContent!
  expect(text).toContain('精选板块最正宗')
  expect(text).toContain('嘉元科技')
  expect(text).toContain('600000')
  expect(text).toContain('-3.45%')
})

it('无同名精选板块或来源不可用时不显示', async () => {
  await render('芯片')
  expect(host.textContent).toBe('')
  act(() => root.unmount())
  root = createRoot(host)
  calls.leaders.mockRejectedValue(new ApiError('x', 503))
  await render('锂电池')
  expect(host.textContent).toBe('')
})
