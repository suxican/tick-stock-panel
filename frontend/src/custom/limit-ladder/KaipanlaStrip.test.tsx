// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { KplSnapshot } from '@/lib/api'
import { KaipanlaStrip } from './KaipanlaStrip'

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

function snapshot(over: Partial<KplSnapshot> = {}): KplSnapshot {
  return {
    date: '2026-11-17', fetched_at: '2026-11-17T10:32:15+08:00', stale_seconds: 0,
    summary: { kpl_strong: 36, limit_up: 41, limit_down: 44, max_boards: 4, sharp_drawdown: 5, first_board_broken: 10, broken_rate: 0.196 },
    tiers: [], stocks: [],
    themes: Array.from({ length: 10 }, (_, i) => ({ code: `80${i}`, name: `题材${i}`, count: 10 - i, symbols: [`S${i}`] })),
    prev_expression: null, missing: [],
    ...over,
  }
}

async function render(props: Parameters<typeof KaipanlaStrip>[0]) {
  await act(async () => root.render(<KaipanlaStrip {...props} />))
}

it('情绪条显示 summary, 缺失项隐藏', async () => {
  await render({ snapshot: snapshot({ missing: ['kpl_strong'], summary: { ...snapshot().summary, kpl_strong: null } }), onThemeSelect: vi.fn() })
  const text = host.textContent ?? ''
  expect(text).toContain('涨停 41')
  expect(text).toContain('跌停 44')
  expect(text).toContain('最高 4 板')
  expect(text).toContain('破板率 19.6%')
  expect(text).toContain('10:32 更新')
  expect(text).not.toContain('情绪')
})

it('情绪值存在时显示', async () => {
  await render({ snapshot: snapshot(), onThemeSelect: vi.fn() })
  expect(host.textContent).toContain('情绪 36')
})

it('stale_seconds>120 显示「数据已过期」', async () => {
  await render({ snapshot: snapshot({ stale_seconds: 121 }), onThemeSelect: vi.fn() })
  expect(host.textContent).toContain('数据已过期')
})

it('不可用时一行灰色提示', async () => {
  await render({ snapshot: undefined, error: '开盘啦暂不可用：超时', onThemeSelect: vi.fn() })
  expect(host.textContent).toBe('开盘啦暂不可用：超时')
})

it('无快照也无错误时不渲染', async () => {
  await render({ snapshot: null, onThemeSelect: vi.fn() })
  expect(host.textContent).toBe('')
})

it('题材行最多 8 个, 点击回调 code, 再点取消', async () => {
  const onThemeSelect = vi.fn()
  await render({ snapshot: snapshot(), onThemeSelect, selectedTheme: '801' })
  const buttons = [...host.querySelectorAll('button[data-theme]')] as HTMLButtonElement[]
  expect(buttons).toHaveLength(8)
  expect(buttons[0].textContent).toBe('题材0 10')
  await act(async () => buttons[0].click())
  expect(onThemeSelect).toHaveBeenLastCalledWith('800')
  await act(async () => buttons[1].click())
  expect(onThemeSelect).toHaveBeenLastCalledWith(undefined)
})

it('有 prev_expression 时显示「昨日二板晋级 26.7%」', async () => {
  await render({ snapshot: snapshot({ prev_expression: { date: '2026-11-16', kpl_promo_2: 0.266667 } }), onThemeSelect: vi.fn() })
  expect(host.textContent).toContain('昨日二板晋级 26.7%')
})

it('历史留档带 expression 时显示当日晋级率', async () => {
  await render({ snapshot: snapshot({ expression: { kpl_promo_2: 0.5 } }), onThemeSelect: vi.fn() })
  expect(host.textContent).toContain('二板晋级 50.0%')
})

it('与梯队不同日时隐藏题材行', async () => {
  await render({ snapshot: snapshot(), onThemeSelect: vi.fn(), showThemes: false })
  expect(host.querySelectorAll('button[data-theme]')).toHaveLength(0)
  expect(host.textContent).toContain('涨停 41')
})
