// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { RecapExecution, RecapPositionCheck } from '@/lib/api'
import { RecapExecutionPanel } from './RecapExecution'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

function position(overrides: Partial<RecapPositionCheck> = {}): RecapPositionCheck {
  return {
    date: '2026-11-17', position_value: 312000, account_size: 500000, ratio_pct: 62.4,
    cap_pct: 80, cap_date: '2026-11-16', cell: 'strong_expand', cell_label: '强·放量',
    exceeded: false, inferred_lots: 0, unknown_lots: 0, reason: null, ...overrides,
  }
}

function execution(overrides: Partial<RecapExecution> = {}): RecapExecution {
  return { as_of: '2026-11-17', opened: [], closed: [], stop_breached: [], stats: null,
    position_check: position(), position_stats: null, ...overrides }
}

async function render(data: RecapExecution) {
  await act(async () => root.render(<RecapExecutionPanel execution={data} />))
}
const row = () => host.querySelector<HTMLElement>('[aria-label="仓位核对"]')!

it('显示收盘占比、上限、格子与未超限', async () => {
  await render(execution())
  expect(row().textContent).toContain('仓位 62%')
  expect(row().textContent).toContain('上限 80%')
  expect(row().textContent).toContain('强·放量')
  expect(row().textContent).toContain('未超限')
})

it('超限用警示色, 推断买入日的批次附注', async () => {
  await render(execution({ position_check: position({ ratio_pct: 91, exceeded: true, inferred_lots: 2 }) }))
  const verdict = [...row().querySelectorAll('span')].find(el => el.textContent === '超限')!
  expect(verdict.className).toContain('text-warning')
  expect(row().textContent).toContain('2 个批次买入日按登记日推断')
})

it('不可判定时只显示原因, 不显示未超限', async () => {
  await render(execution({ position_check: position({ exceeded: null, cap_pct: null, reason: '前一交易日无结论存档' }) }))
  expect(row().textContent).toContain('前一交易日无结论存档')
  expect(row().textContent).not.toContain('未超限')
})

it('累计纪律可展开按格查看', async () => {
  await render(execution({ position_stats: { checked: 5, exceeded: 1, by_cell: [
    { cell: 'strong_expand', label: '强·放量', checked: 3, exceeded: 0 },
    { cell: 'weak_flat', label: '弱·平量', checked: 2, exceeded: 1 },
  ] } }))
  const toggle = [...host.querySelectorAll('button')].find(el => el.textContent?.includes('已核对 5 天'))!
  expect(toggle.textContent).toContain('超限 1 天')
  await act(async () => toggle.click())
  expect(host.textContent).toContain('弱·平量')
  expect(host.textContent).toContain('2 天 · 超限 1')
})

it('未开始使用仓位核对时不显示该行, 也不显示金额', async () => {
  await render(execution({ position_check: null }))
  expect(host.querySelector('[aria-label="仓位核对"]')).toBeNull()
  await render(execution())
  expect(host.textContent).not.toMatch(/312000|500000|31\.2万|50万/)
})
