// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('开盘啦快照请求超时长于后端一轮抓取 (约 30 秒), 不在 30 秒时误报超时', async () => {
  vi.useFakeTimers()
  vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
  }))
  let settled = false
  const pending = api.kaipanlaIntraday().catch(err => { settled = true; return err })
  await vi.advanceTimersByTimeAsync(35_000)
  expect(settled).toBe(false)
  await vi.advanceTimersByTimeAsync(60_000)
  expect(settled).toBe(true)
  expect(String(await pending)).toContain('请求超时')
})
