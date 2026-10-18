// @vitest-environment jsdom
import { act, type HTMLAttributes, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AlertEvent } from '@/lib/api'
import { AlertToastContainer, pushAlertToast } from './AlertToast'

vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => children,
  motion: {
    div: ({ layout, initial, animate, exit, transition, ...props }: HTMLAttributes<HTMLDivElement> & Record<string, unknown>) => {
      void layout; void initial; void animate; void exit; void transition
      return <div {...props} />
    },
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/notificationSound', () => ({ playNotificationSound: vi.fn() }))
vi.mock('@/lib/voiceBroadcast', () => ({ speakAlerts: vi.fn() }))

let host: HTMLDivElement
let root: Root
function Location() {
  const location = useLocation()
  return <output>{location.pathname}{location.search}</output>
}
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AlertToastContainer /><Location />
    </MemoryRouter>,
  ))
})
afterEach(async () => {
  await act(async () => { vi.advanceTimersByTime(6000) })
  await act(async () => root.unmount())
  host.remove()
  vi.useRealTimers()
})

it.each(['click', 'Enter', ' '])('opens first-board event records by %s', async action => {
  await act(async () => pushAlertToast({
    ts: 1, source: 'first_board', type: 'sealed', symbol: '600000.SH', name: '首板样本',
    message: '涨停价观察',
  } as AlertEvent))
  expect(host.textContent).toContain('首板模式')
  const notice = host.querySelector<HTMLElement>('[role="button"]')!
  await act(async () => {
    if (action === 'click') notice.click()
    else notice.dispatchEvent(new KeyboardEvent('keydown', { key: action, bubbles: true }))
  })
  expect(host.querySelector('output')?.textContent).toBe('/first-board?tab=events')
  expect(host.querySelector('[role="button"]')).toBeNull()
})

it('keeps other notifications linked to the monitor', async () => {
  await act(async () => pushAlertToast({
    ts: 1, source: 'price', type: 'price', symbol: '600000.SH', message: '价格提示',
  } as AlertEvent))
  await act(async () => host.querySelector<HTMLElement>('[role="button"]')!.click())
  expect(host.querySelector('output')?.textContent).toBe('/monitor')
})

it('labels huichun notifications and opens their monitor records', async () => {
  await act(async () => pushAlertToast({
    ts: 1, source: 'huichun', type: 'a0_confirmed', symbol: '600000.SH', name: '回春样本',
    message: '回春 A0 确认',
  } as AlertEvent))
  expect(host.textContent).toContain('回春模式')
  expect(host.textContent).toContain('回春 A0 确认')
  await act(async () => host.querySelector<HTMLElement>('[role="button"]')!.click())
  expect(host.querySelector('output')?.textContent).toBe('/monitor')
})
