// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { StockDailyKChart } from './StockDailyKChart'

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({
    data: { rows: [{ date: '2026-09-30', open: 10, high: 11, low: 9, close: 10, volume: 100 }] },
    isLoading: false,
    isError: false,
  }),
}))

vi.mock('@/components/EChartsCandlestick', async importOriginal => ({
  ...await importOriginal<typeof import('./EChartsCandlestick')>(),
  EChartsCandlestick: ({ activeIndicators }: { activeIndicators: string[] }) => (
    <div data-testid="indicators">{activeIndicators.join(',')}</div>
  ),
}))

it('默认显示成交量和 MACD，且允许关闭并重新开启 MACD', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(<StockDailyKChart symbol="600000.SH" />))
    const indicators = () => host.querySelector('[data-testid="indicators"]')?.textContent
    const macd = Array.from(host.querySelectorAll('button')).find(button => button.textContent === 'MACD')!
    expect(indicators()).toBe('vol,macd')
    await act(async () => macd.click())
    expect(indicators()).toBe('vol')
    await act(async () => macd.click())
    expect(indicators()).toBe('vol,macd')
  } finally {
    await act(async () => root.unmount())
    host.remove()
  }
})
