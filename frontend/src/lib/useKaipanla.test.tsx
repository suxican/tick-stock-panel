import { beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { kaipanlaIntradayOptions } from './useKaipanla'

const calls = vi.hoisted(() => ({ kaipanlaIntraday: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))

beforeEach(() => vi.resetAllMocks())

it('未启用时不请求快照', () => {
  expect(kaipanlaIntradayOptions(undefined, false, '2026-11-17').enabled).toBe(false)
})

it('当日 30 秒轮询, 历史不轮询', () => {
  const live = kaipanlaIntradayOptions('2026-11-17', true, '2026-11-17')
  expect(live.refetchInterval).toBe(30_000)
  expect(live.queryKey).toEqual(['kaipanla', 'intraday', 'today'])
  const past = kaipanlaIntradayOptions('2026-11-16', true, '2026-11-17')
  expect(past.refetchInterval).toBe(false)
  expect(past.queryKey).toEqual(['kaipanla', 'intraday', '2026-11-16'])
})

it('历史无留档 (404) 返回 null 而不是报错', async () => {
  calls.kaipanlaIntraday.mockRejectedValue(new ApiError('该日没有开盘啦留档', 404))
  await expect(kaipanlaIntradayOptions('2026-11-13', true, '2026-11-17').queryFn()).resolves.toBeNull()
  expect(calls.kaipanlaIntraday).toHaveBeenCalledWith('2026-11-13')
})

it('当日请求不带日期; 其他错误照常抛出且不重试 503', async () => {
  const err = new ApiError('开盘啦暂不可用：超时', 503)
  calls.kaipanlaIntraday.mockRejectedValue(err)
  const opts = kaipanlaIntradayOptions(undefined, true, '2026-11-17')
  await expect(opts.queryFn()).rejects.toBe(err)
  expect(calls.kaipanlaIntraday).toHaveBeenCalledWith(undefined)
  expect(opts.retry(0, err)).toBe(false)
})
