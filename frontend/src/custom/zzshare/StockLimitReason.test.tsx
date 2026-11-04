// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockLimitReason } from './extension'

const read = vi.hoisted(() => vi.fn())
vi.mock('@/lib/useZzshareReasons', () => ({ useZzshareReasons: read }))

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => { await act(() => root.unmount()); read.mockReset() })

async function show(tradeDate: string | null, symbol = '600001.SH') {
  await act(() => root.render(<StockLimitReason symbol={symbol} name={null} view="daily" tradeDate={tradeDate} />))
  return host.textContent
}

it('显示归因来源和归属交易日，切换股票不沿用旧记录', async () => {
  read.mockReturnValue({ data: new Map([['600001.SH', '拟收购芯片业务']]) })
  expect(await show('2026-10-21')).toContain('拟收购芯片业务')
  expect(host.querySelector('time')?.dateTime).toBe('2026-10-21')
  expect(host.textContent).toContain('ZZShare')
  expect(await show('2026-10-21', '000001.SZ')).toContain('暂无本股的归因记录')
  expect(host.textContent).not.toContain('拟收购芯片业务')
  expect(await show(null)).toContain('暂无可核对的交易日')
  expect(read).toHaveBeenLastCalledWith(null)
})

it('区分加载、快照缺失和读取失败', async () => {
  read.mockReturnValue({ isLoading: true })
  expect(await show('2026-10-19')).toContain('正在读取')
  read.mockReturnValue({ data: new Map() })
  expect(await show('2026-10-19')).toContain('暂无该交易日的归因快照')
  read.mockReturnValue({ isError: true })
  expect(await show('2026-10-20')).toContain('读取失败')
})
