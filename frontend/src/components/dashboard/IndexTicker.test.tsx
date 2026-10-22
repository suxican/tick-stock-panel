// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { expect, it } from 'vitest'
import { IndexTicker } from './widgets'

it('keeps the existing index link by default and supports a static dated snapshot', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  const item = { symbol: '000001.SH', name: '上证指数', last_price: 3813.79, change_pct: 0.05 }
  try {
    await act(async () => root.render(<MemoryRouter><IndexTicker item={item} /></MemoryRouter>))
    expect(host.querySelector('a')?.getAttribute('href')).toBe('/indices?symbol=000001.SH')
    expect(host.textContent).toContain('+0.05%')
    expect(host.textContent).toContain('3813.79')
    await act(async () => root.render(<MemoryRouter><IndexTicker item={item} linked={false} /></MemoryRouter>))
    expect(host.querySelector('a')).toBeNull()
    expect(host.textContent).toContain('+0.05%')
    await act(async () => root.render(<MemoryRouter><IndexTicker item={{ ...item, change_pct: null, last_price: null }} linked={false} /></MemoryRouter>))
    expect(host.querySelector('svg')).toBeNull()
    expect(host.textContent).toContain('—')
  } finally {
    await act(async () => root.unmount())
  }
})
