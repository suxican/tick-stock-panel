// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import type { IntradayAnnotation } from '@/components/EChartsIntraday'
import { LiveIndexChart } from './LiveIndexChart'

const calls = vi.hoisted(() => ({ matrix: vi.fn(), minute: vi.fn(), daily: vi.fn(), chart: vi.fn(), select: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { capabilityMatrix: calls.matrix, indexMinute: calls.minute, indexDaily: calls.daily } }))
vi.mock('@/components/EChartsIntraday', () => ({ EChartsIntraday: (props: unknown) => { calls.chart(props); return <div data-intraday /> } }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
const day = '2026-09-30'
const bar = (date = day, time = '09:30') => ({ datetime: `${date}T${time}:00`, open: 3800, high: 3850, low: 3800, close: 3842, volume: 125, amount: 12345 })
const matrix = (effective = 'tickflow', usable = true) => ({ capabilities: [{ id: 'minute', effective, effective_display: effective === 'tickflow' ? 'TickFlow' : effective, usable }] })
const minute = () => ({ symbol: '000001.SH', date: day, rows: [bar(), bar(day, '15:00')], source: 'live' })
const daily = () => ({ symbol: '000001.SH', source: 'index_enriched', rows: [
  { date: day, close: 3842 }, { date: '2026-09-29', close: 3830 }, { date: '2026-09-28', close: 3790 },
] })
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.matrix.mockResolvedValue(matrix())
  calls.minute.mockResolvedValue(minute())
  calls.daily.mockResolvedValue(daily())
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 6; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(date = day, annotations: IntradayAnnotation[] = []) {
  await act(async () => root.render(<QueryClientProvider client={client}><LiveIndexChart date={date} live={false} annotations={annotations} onAnnotationClick={calls.select} /></QueryClientProvider>))
  await settle()
}
it('uses the configured minute route, exact index/date, prior trading close, and minute volume', async () => {
  await render()
  expect(calls.minute).toHaveBeenCalledWith('000001.SH', day)
  expect(calls.daily).toHaveBeenCalledWith('000001.SH', 120, { start: '2026-08-01', end: '2026-09-29' })
  expect(calls.chart.mock.lastCall?.[0]).toMatchObject({ date: day, prevClose: 3830, showLimitLines: false, showAvgLine: false, connectNulls: false, data: minute().rows })
  expect(host.textContent).toContain('TickFlow')
  expect(host.textContent).toContain('+0.31%')
  expect(host.textContent).toContain('历史分时')
  expect(host.textContent).not.toContain('实时行情')
})
it('does not query when the configured source has no minute capability', async () => {
  calls.matrix.mockResolvedValue(matrix('fuyao', false))
  await render()
  expect(calls.minute).not.toHaveBeenCalled()
  expect(calls.daily).not.toHaveBeenCalled()
  expect(host.textContent).toContain('不支持分钟数据')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/settings?tab=data-sources')
})
it('rejects wrong date or wrong index responses instead of relabeling them', async () => {
  calls.minute.mockResolvedValue({ ...minute(), date: '2026-10-01' })
  await render()
  expect(host.querySelector('[data-intraday]')).toBeNull()
  expect(host.textContent).toContain('分时读取失败')
  calls.minute.mockResolvedValue({ ...minute(), symbol: '399001.SZ' })
  await act(async () => { await client.invalidateQueries({ queryKey: QK.indexMinute('000001.SH', day) }) })
  await settle()
  expect(host.querySelector('[data-intraday]')).toBeNull()
})
it('keeps gaps and excludes other dates, out-of-session times and nonfinite prices', async () => {
  calls.minute.mockResolvedValue({ ...minute(), rows: [bar('2026-09-29'), bar(day, '12:00'), { ...bar(day, '09:31'), close: NaN }, bar(day, '10:00')] })
  await render()
  expect(calls.chart.mock.lastCall?.[0].data).toEqual([bar(day, '10:00')])
})
it('shows prices without percentages when prior close is unavailable', async () => {
  calls.daily.mockResolvedValue({ ...daily(), rows: [{ date: day, close: 9999 }] })
  await render()
  expect(calls.chart.mock.lastCall?.[0].prevClose).toBeUndefined()
  expect(host.textContent).toContain('昨收暂不可用')
  expect(host.textContent).not.toContain('+0.31%')
})
it('isolates provider caches so a newly configured source cannot inherit old minutes', async () => {
  await render()
  calls.minute.mockResolvedValue({ ...minute(), rows: [] })
  await act(async () => client.setQueryData(QK.capabilityMatrix, matrix('another-minute-source')))
  await settle()
  expect(calls.minute).toHaveBeenCalledTimes(2)
  expect(host.textContent).toContain('another-minute-source')
  expect(host.textContent).toContain('暂无该日指数分钟数据')
  expect(host.querySelector('[data-intraday]')).toBeNull()
})
it('retains the same-day plot on refresh failure and never carries it to a different day', async () => {
  await render()
  calls.minute.mockRejectedValue(new Error('offline'))
  await act(async () => { host.querySelector('button')!.click() })
  await settle()
  expect(host.querySelector('[data-intraday]')).not.toBeNull()
  expect(host.textContent).toContain('保留上次有效分时')
  await render('2026-09-29')
  expect(host.querySelector('[data-intraday]')).toBeNull()
})

it('shows sourced strength labels, toggles chart captions, and locates matched events accessibly', async () => {
  const annotations: IntradayAnnotation[] = [
    { id: 'up', time: '09:30', label: '医药板块持续走强，个股跟涨', caption: '医药板块持续走强', direction: 'up' },
    { id: 'down', time: '15:00', label: '三大指数延续弱势', caption: '三大指数延续弱势', direction: 'down' },
    { id: 'gap', time: '10:00', label: '缺失分钟事件', caption: '缺失分钟事件', direction: 'up' },
  ]
  await render(day, annotations)
  expect(calls.chart.mock.lastCall?.[0].annotations).toEqual(annotations)
  expect(host.textContent).toContain('开盘啦直播原文')
  expect(host.textContent).toContain('全部强弱标注（2）')
  expect(host.textContent).not.toContain('缺失分钟事件')
  const hide = [...host.querySelectorAll('button')].find(button => button.textContent === '隐藏文字标注')!
  expect(hide.getAttribute('aria-pressed')).toBe('true')
  await act(async () => hide.click())
  expect(calls.chart.mock.lastCall?.[0].annotations.every((item: IntradayAnnotation) => item.caption == null)).toBe(true)
  expect(calls.chart.mock.lastCall?.[0].annotations[1].direction).toBe('down')
  expect(calls.minute).toHaveBeenCalledTimes(1)
  const message = [...host.querySelectorAll('button')].find(button => button.textContent?.includes('三大指数延续弱势'))!
  await act(async () => message.click())
  expect(calls.select).toHaveBeenCalledWith('down')
})
