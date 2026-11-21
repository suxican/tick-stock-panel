// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { FirstBoardTemperature } from '@/lib/api'
import { TemperatureBadge } from './TemperatureBadge'

const base: FirstBoardTemperature = { version: 'temperature-v1', tier: 'attack', label: '进攻', seal_rate: .72, touch_count: 40,
  feedback_mean: .025, feedback_sample: 20, stage: 'session', as_of: '2026-11-20', observed_at: '2026-11-20T10:30:00+08:00', reason: '' }

function render(value?: FirstBoardTemperature) {
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(<TemperatureBadge value={value} />)
  return host
}
const node = (host: HTMLElement) => host.querySelector<HTMLElement>('[data-first-board-temperature]')!
const badge = (host: HTMLElement) => [...host.querySelectorAll<HTMLElement>('span')].find(item => item.textContent?.startsWith('首板温度'))!

it('shows tier and stage without repeating the key numbers', () => {
  const host = render(base)
  const text = node(host).textContent!
  expect(text).toContain('首板温度 · 进攻')
  expect(text).toContain('盘中')
  expect(text).not.toContain('封板率')
  expect(text).not.toContain('昨日首板均涨')
  expect(badge(host).className).toContain('blue')
  expect(node(host).title).toContain('只作显示，不影响买入提示与建议金额。')
})

it('marks rest in amber with the close date', () => {
  const host = render({ ...base, tier: 'rest', label: '休息', seal_rate: .68, feedback_mean: -.0448, stage: 'close' })
  const text = node(host).textContent!
  expect(text).toContain('首板温度 · 休息')
  expect(text).toContain('11/20 收盘')
  expect(badge(host).className).toContain('amber')
})

it('flags the early reading', () => {
  const host = render({ ...base, stage: 'early' })
  expect(node(host).textContent).toContain('早盘初判')
  expect(node(host).title).toContain('早盘参考性较弱。')
})

it('explains unknown in the tooltip', () => {
  const host = render({ ...base, tier: 'unknown', label: '待确认', seal_rate: null, reason: '封板率暂不可用（缺少炸板数据）' })
  expect(node(host).textContent).toContain('首板温度 · 待确认')
  expect(node(host).title).toContain('待确认原因：封板率暂不可用（缺少炸板数据）')
})

it('renders nothing for old or unknown payloads', () => {
  expect(render(undefined).innerHTML).toBe('')
  expect(render({ ...base, version: 'temperature-v9' }).innerHTML).toBe('')
})
