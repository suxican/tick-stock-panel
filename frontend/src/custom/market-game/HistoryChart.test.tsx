// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EChartsOption } from 'echarts'
import { HistoryChart } from './HistoryChart'

const calls = vi.hoisted(() => ({ init: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), observe: vi.fn(), disconnect: vi.fn() }))
vi.mock('echarts', () => ({ init: calls.init }))
let host: HTMLDivElement
let root: Root
let resize: () => void
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.init.mockReturnValue({ setOption: calls.setOption, resize: calls.resize, dispose: calls.dispose })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
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

it('updates null-gapped history and theme without recreating the chart and releases it when leaving the tab', async () => {
  const option: EChartsOption = { series: [{ type: 'line', data: [55, null, 60], connectNulls: false }] }
  await act(async () => root.render(<HistoryChart option={option} label="心理刻度历史" height={290} />))
  expect(calls.init).toHaveBeenCalledWith(host.firstElementChild, undefined, { renderer: 'svg' })
  expect(calls.setOption).toHaveBeenLastCalledWith(option, { notMerge: true })
  expect(host.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('心理刻度历史')
  resize()
  expect(calls.resize).toHaveBeenCalledTimes(1)
  const updated = { ...option, backgroundColor: '#000' }
  await act(async () => root.render(<HistoryChart option={updated} label="心理刻度历史" height={290} />))
  expect(calls.init).toHaveBeenCalledTimes(1)
  expect(calls.setOption).toHaveBeenLastCalledWith(updated, { notMerge: true })
  await act(async () => root.render(null))
  expect(calls.disconnect).toHaveBeenCalledTimes(1)
  expect(calls.dispose).toHaveBeenCalledTimes(1)
})
