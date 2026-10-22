// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EChartsOption } from 'echarts'
import { EmotionHistoryChart } from './EmotionHistoryChart'

const calls = vi.hoisted(() => ({ init: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), observe: vi.fn(), disconnect: vi.fn() }))
vi.mock('echarts', () => ({ init: calls.init }))
let host: HTMLDivElement
let root: Root
let onResize: () => void

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.init.mockReturnValue({ setOption: calls.setOption, resize: calls.resize, dispose: calls.dispose })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { onResize = callback }
    observe = calls.observe
    disconnect = calls.disconnect
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

it('updates series and theme without recreating the chart, and releases the canvas on unmount', async () => {
  const initial: EChartsOption = { series: [{ type: 'line', data: [50, null, 62], connectNulls: false }] }
  await act(async () => root.render(<EmotionHistoryChart option={initial} label="综合强度历史" />))
  expect(calls.init).toHaveBeenCalledTimes(1)
  expect(calls.init).toHaveBeenCalledWith(host.firstElementChild, undefined, { renderer: 'canvas' })
  expect(calls.setOption).toHaveBeenLastCalledWith(initial, { notMerge: true })
  expect(calls.observe).toHaveBeenCalledWith(host.firstElementChild)
  onResize()
  expect(calls.resize).toHaveBeenCalledTimes(1)

  const updated: EChartsOption = { textStyle: { color: '#27272A' }, series: [{ type: 'bar', data: [4, 0, 8] }] }
  await act(async () => root.render(<EmotionHistoryChart option={updated} label="大幅回撤家数历史" />))
  expect(calls.init).toHaveBeenCalledTimes(1)
  expect(calls.setOption).toHaveBeenLastCalledWith(updated, { notMerge: true })
  expect(host.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('大幅回撤家数历史')

  await act(async () => root.render(null))
  expect(calls.disconnect).toHaveBeenCalledTimes(1)
  expect(calls.dispose).toHaveBeenCalledTimes(1)
})
