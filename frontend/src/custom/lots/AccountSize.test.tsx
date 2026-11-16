// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { AccountSizeBar } from './AccountSize'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><AccountSizeBar /></QueryClientProvider>))
  await settle()
}
function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}
const button = (text: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find(el => el.textContent?.includes(text))!

it('未填写时提示填写账户规模', async () => {
  vi.spyOn(api, 'accountSize').mockResolvedValue({ history: [] })
  await render()
  expect(host.textContent).toContain('未填写账户规模')
  expect(button('填写')).toBeDefined()
})

it('显示最近一次生效的规模与日期', async () => {
  vi.spyOn(api, 'accountSize').mockResolvedValue({ history: [
    { effective_date: '2026-11-01', amount: 400000 }, { effective_date: '2026-11-16', amount: 525000 },
  ] })
  await render()
  expect(host.textContent).toContain('账户规模 52.5 万')
  expect(host.textContent).toContain('2026-11-16 起')
})

it('修改时以万为单位提交金额与生效日期', async () => {
  vi.spyOn(api, 'accountSize').mockResolvedValue({ history: [{ effective_date: '2026-11-01', amount: 400000 }] })
  const update = vi.spyOn(api, 'accountSizeUpdate').mockResolvedValue({ history: [] })
  await render()
  await act(async () => button('修改').click())
  await settle()
  const amount = document.querySelector<HTMLInputElement>('[aria-label="账户规模（万元）"]')!
  const day = document.querySelector<HTMLInputElement>('[aria-label="生效日期"]')!
  expect(amount.value).toBe('40')
  await act(async () => { setInput(amount, '55.5'); setInput(day, '2026-11-17') })
  await act(async () => button('保存').click())
  await settle()
  expect(update.mock.calls[0][0]).toEqual({ effective_date: '2026-11-17', amount: 555000 })
})

it('非法金额不提交', async () => {
  vi.spyOn(api, 'accountSize').mockResolvedValue({ history: [] })
  const update = vi.spyOn(api, 'accountSizeUpdate').mockResolvedValue({ history: [] })
  await render()
  await act(async () => button('填写').click())
  await settle()
  await act(async () => setInput(document.querySelector<HTMLInputElement>('[aria-label="账户规模（万元）"]')!, '0'))
  await act(async () => button('保存').click())
  expect(update).not.toHaveBeenCalled()
})
