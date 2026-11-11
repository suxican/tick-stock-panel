// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { RecapSnapshot } from '@/lib/api'
import { RecapSnapshotPanel } from './RecapSnapshot'

const chart = vi.hoisted(() => ({ setOption: vi.fn() }))
vi.mock('echarts', () => ({
  init: () => ({ setOption: chart.setOption, resize: vi.fn(), dispose: vi.fn() }),
}))

const snapshot: RecapSnapshot = {
  version: 1,
  as_of: '2026-11-11',
  intraday: false,
  generated_at: '2026-11-11T20:00:00',
  today_source: 'regime',
  phase: { key: 'ignite', label: '启动', days: 3, capped: false, date: '2026-11-11', state_label: '强势' },
  dates: ['2026-11-09', '2026-11-10', '2026-11-11'],
  metrics: [
    { key: 'amount_yi', label: '成交额', unit: '亿', digits: 0, today: 16812, prev: 15094, avg5: 17656, series: [14860, 15094, 16812] },
    { key: 'limit_up', label: '涨停', unit: '家', digits: 0, today: 85, prev: 93, avg5: 77, series: [55, 93, 85] },
    { key: 'seal_rate', label: '封板率', unit: '%', digits: 0, today: 78, prev: 82, avg5: 68, series: [53, 82, 78] },
    { key: 'max_boards', label: '最高连板', unit: '板', digits: 0, today: 4, prev: 7, avg5: 5, series: [5, 7, 4] },
    { key: 'promo_rate', label: '连板晋级率', unit: '%', digits: 0, today: 20.4, prev: 35.7, avg5: 25, series: [31, 36, 20] },
  ],
  mainlines: [
    { name: '特斯拉概念', rank: 1, limit_up: 10, max_boards: 4, leader: { symbol: '603366.SH', name: '龙头甲' }, streak: 3 },
    { name: '虚拟现实', rank: 2, limit_up: 8, max_boards: 3, leader: null, streak: 1 },
  ],
  sectors: { leading: [{ name: 'CPO', pct: 4.64, leader: '光一' }], lagging: [{ name: '白酒', pct: -3.08, leader: null }] },
  ladder: [
    { boards: 4, count: 3, stocks: [{ symbol: '000001.SZ', name: '四板甲' }] },
    { boards: 1, count: 66, stocks: [] },
  ],
  prev_limit_up: { count: 90, avg_pct: 2.15, up_ratio: 64 },
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.setOption.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

it('renders phase, metric comparisons, mainlines and ladder from the snapshot', () => {
  const onOpenStock = vi.fn()
  act(() => root.render(<RecapSnapshotPanel snapshot={snapshot} onOpenStock={onOpenStock} />))
  const text = host.textContent ?? ''

  expect(text).toContain('周期 · 启动 第3天')
  // 成交额较昨日 +11%, 封板率给差值 -4pp
  expect(text).toContain('16812')
  expect(text).toContain('+11%')
  expect(text).toContain('-4pp')
  expect(text).toContain('-3板')  // 连板高度给差值而非相对变化
  expect(text).toContain('特斯拉概念')
  expect(text).toContain('首日')
  expect(text).toContain('等3家')
  expect(text).toContain('晋级率 20%')
  expect(text).toContain('昨日涨停今日 +2.15%')
  expect(text).toContain('白酒 -3.08%')

  const leader = Array.from(host.querySelectorAll('button')).find(b => b.textContent === '龙头甲')!
  act(() => leader.click())
  expect(onOpenStock).toHaveBeenCalledWith('603366.SH')

  // 走势图: 成交额柱 + 涨停线
  const option = chart.setOption.mock.calls.at(-1)?.[0]
  expect(option.series.map((s: { name: string }) => s.name)).toEqual(['成交额', '涨停'])
})

it('marks intraday snapshots and missing mainline data', () => {
  act(() => root.render(
    <RecapSnapshotPanel snapshot={{ ...snapshot, intraday: true, generated_at: '2026-11-11T11:30:00', mainlines: [] }} />,
  ))
  const text = host.textContent ?? ''
  expect(text).toContain('盘中快照 11:30')
  expect(text).toContain('涨停主线尚未计算')
})
