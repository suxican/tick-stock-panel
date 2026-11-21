import { afterEach, expect, it, vi } from 'vitest'
import { sceneAt } from './scene'

// 北京时间 → 毫秒时间戳；2026-11-23 是周一，2026-11-21 是周六。
const beijing = (day: number, hour: number, minute = 0) => Date.UTC(2026, 10, day, hour - 8, minute)

afterEach(() => vi.useRealTimers())

it('classifies the boundaries and weekends', () => {
  expect(sceneAt(beijing(23, 9, 14))).toBe('normal')
  expect(sceneAt(beijing(23, 9, 15))).toBe('preopen')
  expect(sceneAt(beijing(23, 9, 29))).toBe('preopen')
  expect(sceneAt(beijing(23, 9, 30))).toBe('normal')
  expect(sceneAt(beijing(23, 14, 59))).toBe('normal')
  expect(sceneAt(beijing(23, 15, 0))).toBe('afterClose')
  expect(sceneAt(beijing(23, 23, 59))).toBe('afterClose')
  expect(sceneAt(beijing(21, 9, 20))).toBe('normal')
  expect(sceneAt(beijing(21, 16, 0))).toBe('normal')
})

it('uses Beijing time rather than the UTC fields', () => {
  // UTC 周一 01:20 是北京周一 09:20 → 竞价前（按 UTC 小时看会是普通）。
  expect(sceneAt(Date.UTC(2026, 10, 23, 1, 20))).toBe('preopen')
  // UTC 周五 10:00 = 北京周五 18:00 → 收盘后；UTC 周日 10:00 = 北京周日 18:00 → 周末为普通。
  expect(sceneAt(Date.UTC(2026, 10, 20, 10, 0))).toBe('afterClose')
  expect(sceneAt(Date.UTC(2026, 10, 22, 10, 0))).toBe('normal')
  // UTC 周五 17:00 已是北京周六 01:00 → 周末为普通（按 UTC 星期看会是工作日的收盘后）。
  expect(sceneAt(Date.UTC(2026, 10, 20, 17, 0))).toBe('normal')
})

it('ignores the machine time zone', () => {
  const original = process.env.TZ
  try {
    for (const zone of ['America/Los_Angeles', 'Asia/Tokyo', 'Asia/Shanghai']) {
      process.env.TZ = zone
      expect(sceneAt(beijing(23, 9, 20)), zone).toBe('preopen')
      expect(sceneAt(beijing(23, 15, 0)), zone).toBe('afterClose')
      expect(sceneAt(beijing(21, 9, 20)), zone).toBe('normal')
    }
  } finally {
    if (original === undefined) delete process.env.TZ
    else process.env.TZ = original
  }
})

it('defaults to the current time', () => {
  vi.useFakeTimers()
  vi.setSystemTime(beijing(23, 15, 5))
  expect(sceneAt()).toBe('afterClose')
})
