// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EChartsIntraday } from './EChartsIntraday'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  on: vi.fn(), off: vi.fn(), setOption: vi.fn(), clear: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  getWidth: () => 320,
  getHeight: () => 320,
}))
const theme = vi.hoisted(() => ({ light: false }))
vi.mock('echarts', () => ({ init: () => chart }))
vi.mock('@/lib/theme', () => ({
  getTheme: () => theme.light ? 'light' : 'dark',
  useChartTheme: () => ({
    tooltipBg: theme.light ? 'rgba(255,255,255,0.97)' : 'rgba(24,24,27,0.95)',
    text: theme.light ? '#71717A' : '#A1A1AA',
  }),
}))
const rows = [
  { datetime: '2026-09-09 09:30:00', open: 119.77, high: 119.77, low: 119, close: 119.1, volume: 100, amount: 1191000 },
  { datetime: '2026-09-09 11:20:00', open: 118.25, high: 118.28, low: 118.24, close: 118.25, volume: 55, amount: 650375 },
]
const daily = { date: '2026-09-09', open: 119.77, high: 119.77, low: 118.16, close: 118.25 }
let cleanup = async () => {}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.handlers = {}
  theme.light = false
  vi.clearAllMocks()
  chart.on.mockImplementation((name, callback) => { chart.handlers[name] = callback })
})
afterEach(async () => { await cleanup(); vi.unstubAllGlobals() })

it('shows daily OHLC by default, labels hovered minute, restores on exit and date switch', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.on.mockImplementation((name, callback) => { chart.handlers[name] = callback })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  const render = async (date = daily.date) => {
    await act(async () => root.render(<EChartsIntraday data={rows.map(row => ({ ...row, datetime: row.datetime.replace(daily.date, date) }))}
      date={date} dailySummary={{ ...daily, date }} />))
  }
  await render()
  expect(host.textContent).toContain('日K')
  expect(host.textContent).toContain('118.16')
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 110 }] }))
  expect(host.textContent).toContain('11:20')
  expect(host.textContent).toContain('118.28')
  await act(async () => chart.handlers.globalout())
  expect(host.textContent).toContain('118.16')
  expect(host.textContent).not.toContain('11:20')
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 110 }] }))
  await render('2026-09-10')
  expect(host.textContent).toContain('118.16')
  expect(host.textContent).not.toContain('11:20')
})

it('aggregates available minutes without inventing a missing opening price', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(<EChartsIntraday data={rows.map(row => ({ ...row, open: null }))} date={daily.date} />))
  expect(host.textContent).toContain('分时汇总')
  expect(host.textContent).toContain('119.77')
  expect(host.textContent).toContain('—')
  expect(host.textContent).toContain('155')
})

it.each([
  [100_000_000, '1.00亿'],
  [1_000_000_000, '10.00亿'],
  [679_398_992_444.8, '6793.99亿'],
])('formats %s yuan in hundred-million units for totals and hovered minutes', async (amount, formatted) => {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(<EChartsIntraday data={[{ ...rows[0], amount }]} />))
  expect(host.textContent).toContain(`累计额${formatted}`)
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 0 }] }))
  expect(host.textContent).not.toContain('累计额')
  expect(host.textContent).toContain(`额${formatted}`)
})

// ================================================================
// y 轴范围: 无涨跌幅新股不被钳制到 ±10% 涨跌停带内 (C沈鼓场景)
// ================================================================
const wideRows = (day: string) => [
  { datetime: `${day} 09:30:00`, open: 15, high: 15, low: 15, close: 15, volume: 100, amount: 150000 },
  { datetime: `${day} 11:20:00`, open: 57.7, high: 57.8, low: 57.7, close: 57.77, volume: 55, amount: 318000 },
]

function lastYAxis(): any {
  const call = chart.setOption.mock.calls.at(-1)
  return call?.[0]?.yAxis?.[0]
}

it('no_limit day: adaptive y-axis covers data far beyond the ±10% band', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday
      data={wideRows('2026-09-18')}
      date="2026-09-18"
      prevClose={20.8}
      priceLimit={{ rate: 0.1, limit_up: null, limit_down: null, no_limit: true, source: 'rule' }}
    />,
  ))
  const axis = lastYAxis()
  // 旧钳制行为会把 y 轴夹到 18.72~22.88, 曲线全部出界; 现在必须覆盖 15~57.77
  expect(axis.min).toBeLessThanOrEqual(15)
  expect(axis.max).toBeGreaterThanOrEqual(57.77)
})

it('regular stock: adaptive y-axis still clamps to the limit band', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday
      data={[
        { datetime: '2026-09-18 09:30:00', open: 20, high: 20, low: 20, close: 20, volume: 100, amount: 200000 },
        { datetime: '2026-09-18 15:00:00', open: 22, high: 22, low: 22, close: 22, volume: 55, amount: 121000 },
      ]}
      date="2026-09-18"
      prevClose={20}
      priceLimit={{ rate: 0.1, limit_up: 22, limit_down: 18, no_limit: false, source: 'rule' }}
    />,
  ))
  const axis = lastYAxis()
  expect(axis.min).toBeCloseTo(18, 6)
  expect(axis.max).toBeCloseTo(22, 6)
})

it('listing day without prevClose anchors y-axis with scale, not zero', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday data={wideRows('2026-09-17')} date="2026-09-17" />,
  ))
  const axis = lastYAxis()
  expect(axis.min).toBeUndefined()
  expect(axis.max).toBeUndefined()
  expect(axis.scale).toBe(true)
})

function mountIntraday() {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  return async (props: React.ComponentProps<typeof EChartsIntraday>) => {
    await act(async () => root.render(<EChartsIntraday {...props} />))
  }
}

function annotationSeries(): any {
  return chart.setOption.mock.calls.at(-1)?.[0]?.series?.find((series: any) => series.id === 'intraday-annotations')
}

it('preserves missing-minute gaps when requested and keeps the legacy connection default', async () => {
  const render = mountIntraday()
  await render({ data: rows, connectNulls: false })
  const getLines = () => chart.setOption.mock.calls.at(-1)?.[0]?.series?.filter((series: any) => series.type === 'line')
  for (const line of getLines()) {
    expect(line.connectNulls).toBe(false)
    expect(line.data[1]).toBeNull()
    expect(line.data[0]).not.toBeNull()
    expect(line.data[110]).not.toBeNull()
  }
  await render({ data: rows })
  expect(getLines().map((series: any) => series.connectNulls)).toEqual([true, true])
})

it('anchors events only to valid matching minutes and renders source text safely', async () => {
  const render = mountIntraday()
  const annotations = [
    { id: 'open', time: '09:30', label: '开盘上涨 <img src=x onerror=alert(1)>' },
    { id: 'present', time: '11:20', label: '银行异动' },
    { id: 'gap', time: '10:00', label: '缺失分钟' },
    { id: 'pre', time: '09:25', label: '集合竞价' },
    { id: 'lunch', time: '12:00', label: '午休' },
    { id: 'post', time: '15:01', label: '盘后' },
    { id: 'invalid', time: '14:00', label: '无有效价格' },
  ]
  await render({ data: [...rows, { ...rows[1], datetime: '2026-09-09 14:00:00', close: 0 }], annotations })
  const series = annotationSeries()
  expect(series.data.map((item: any) => [item.annotationId, item.value])).toEqual([
    ['open', ['09:30', 119.1]], ['present', ['11:20', 118.25]],
  ])
  const tooltip = series.tooltip.formatter({ data: series.data[0] }) as HTMLElement
  expect(tooltip.textContent).toBe('09:30\n开盘上涨 <img src=x onerror=alert(1)>')
  expect(tooltip.querySelector('img')).toBeNull()
})

it('keeps separate click targets for messages sharing a minute without altering their price', async () => {
  const render = mountIntraday()
  const click = vi.fn()
  await render({ data: rows, annotations: [
    { id: 'one', time: '11:20', label: '第一条' },
    { id: 'two', time: '11:20', label: '第二条' },
  ], onAnnotationClick: click })
  const series = annotationSeries()
  expect(series.data.map((item: any) => item.value)).toEqual([['11:20', 118.25], ['11:20', 118.25]])
  expect(series.data[0].symbolOffset).not.toEqual(series.data[1].symbolOffset)
  for (const data of series.data) {
    await act(async () => chart.handlers.click({ componentType: 'series', seriesId: series.id, data }))
  }
  expect(click.mock.calls).toEqual([['one'], ['two']])
})

it.each([false, true])('uses legible directional captions and leader lines in light theme=%s', async light => {
  theme.light = light
  const render = mountIntraday()
  const click = vi.fn()
  await render({ data: rows, onAnnotationClick: click, annotations: [
    { id: 'up', time: '09:30', label: '银行板块上涨完整文案', caption: '银行板块上涨', direction: 'up' },
    { id: 'down', time: '11:20', label: '医药板块下跌完整文案', caption: '医药板块下跌', direction: 'down' },
    { id: 'neutral', time: '11:20', label: '其他消息', direction: 'neutral' },
    { id: 'legacy', time: '11:20', label: '旧消费者' },
  ] })
  const series = annotationSeries()
  const [up, down, neutral, legacy] = series.data
  expect(up.itemStyle.color).toBe(light ? '#B91C1C' : '#F87171')
  expect(down.itemStyle.color).toBe(light ? '#15803D' : '#4ADE80')
  expect(up.label.position).toBe('top')
  expect(down.label.position).toBe('bottom')
  expect(neutral.itemStyle.color).toBe(light ? '#71717A' : '#A1A1AA')
  for (const point of [up, down]) {
    expect(point.label.show).toBe(true)
    expect(point.label.color).toBe(point.itemStyle.color)
    expect(point.label.borderColor).toBe(point.itemStyle.color)
    expect(point.label.backgroundColor).toBe(light ? 'rgba(255,255,255,0.97)' : 'rgba(24,24,27,0.95)')
    expect(point.labelLine).toEqual({ show: true, lineStyle: { color: point.itemStyle.color, width: 1 } })
    await act(async () => chart.handlers.click({ componentType: 'series', seriesId: series.id, data: point, targetType: 'label' }))
  }
  expect(click.mock.calls).toEqual([['up'], ['down']])
  expect(neutral.label.show).toBe(false)
  expect(legacy.itemStyle.color).toBe('#EF4444')
  expect(legacy.label.show).toBe(false)
  expect(legacy.labelLine.show).toBe(false)
})

it('keeps literal captions safe and clears label decoration when caption is removed', async () => {
  const render = mountIntraday()
  const caption = '{b} <img src=x onerror=alert(1)>'
  await render({ data: rows, annotations: [{ id: 'live', time: '11:20', label: caption, caption, direction: 'down' }] })
  const series = annotationSeries()
  const point = series.data[0]
  expect(point.label.formatter()).toBe(caption)
  expect(point.label.rich).toBeUndefined()
  expect(series.tooltip.formatter({ data: point }).querySelector('img')).toBeNull()
  const left = series.labelLayout({ labelRect: { x: -60, y: -8, width: 120, height: 24 } })
  const right = series.labelLayout({ labelRect: { x: 300, y: 280, width: 120, height: 24 } })
  expect(left.x).toBeGreaterThanOrEqual(5)
  expect(left.y).toBeGreaterThanOrEqual(24)
  expect(right.x + 120).toBeLessThanOrEqual(320)
  expect(right.y + 24).toBeLessThanOrEqual(320 * 0.66)
  expect(right).toMatchObject({ hideOverlap: true, verticalAlign: 'top' })
  expect(right.moveOverlap).toBeUndefined()
  const nearPoint = series.labelLayout({ labelRect: { x: 80, y: 80, width: 120, height: 24 } })
  expect(nearPoint.y).toBe(83)
  await render({ data: rows, annotations: [{ id: 'live', time: '11:20', label: caption, direction: 'neutral' }] })
  expect(annotationSeries().data[0].label.show).toBe(false)
  expect(annotationSeries().data[0].labelLine.show).toBe(false)
})

it('uses the latest event callback and ignores ordinary, removed and unmatched event clicks', async () => {
  const render = mountIntraday()
  const firstClick = vi.fn()
  const latestClick = vi.fn()
  const annotations = [{ id: 'live', time: '11:20', label: '消息' }, { id: 'gap', time: '10:00', label: '缺口' }]
  await render({ data: rows, annotations, onAnnotationClick: firstClick })
  const event = { componentType: 'series', seriesId: 'intraday-annotations', data: annotationSeries().data[0] }
  await render({ data: rows, annotations, onAnnotationClick: latestClick })
  await act(async () => {
    chart.handlers.click({ ...event, seriesId: '价格' })
    chart.handlers.click({ ...event, componentType: 'markLine' })
    chart.handlers.click({ ...event, data: { annotationId: 'gap' } })
    chart.handlers.click(event)
  })
  expect(firstClick).not.toHaveBeenCalled()
  expect(latestClick.mock.calls).toEqual([['live']])
  await render({ data: rows, annotations: [], onAnnotationClick: latestClick })
  expect(annotationSeries()).toBeUndefined()
  await act(async () => chart.handlers.click(event))
  expect(latestClick).toHaveBeenCalledTimes(1)
  expect(chart.on.mock.calls.filter(([name]) => name === 'click')).toHaveLength(1)
})

it('clears event targets with empty data and releases the click handler on unmount', async () => {
  const render = mountIntraday()
  const click = vi.fn()
  const annotations = [{ id: 'live', time: '11:20', label: '消息' }]
  await render({ data: rows, annotations, onAnnotationClick: click })
  const event = { componentType: 'series', seriesId: 'intraday-annotations', data: annotationSeries().data[0] }
  await render({ data: [], annotations, onAnnotationClick: click })
  expect(chart.clear).toHaveBeenCalledTimes(1)
  await act(async () => chart.handlers.click(event))
  expect(click).not.toHaveBeenCalled()
  await cleanup()
  cleanup = async () => {}
  expect(chart.off).toHaveBeenCalledWith('click')
  expect(chart.dispose).toHaveBeenCalledTimes(1)
})
