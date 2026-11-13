import { describe, expect, it } from 'vitest'
import { cnDate, cnDateTime, cnDaysAgo, cnFormat, cnMonthDayTime, cnParts, cnToday, parseCnTime, shiftDate } from './cnTime'

describe('cnTime — 北京时间工具', () => {
  it('UTC 16:00 已是北京次日 (不用 UTC 日期)', () => {
    const t = new Date('2026-11-11T16:30:00Z')
    expect(cnToday(t)).toBe('2026-11-12')
    expect(cnDateTime(t)).toBe('2026-11-12 00:30:00')
    expect(cnParts(t)).toMatchObject({ hour: 0, minute: 30, weekday: 4 })
  })

  it('无时区串按北京墙钟解析, 带时区串按其时区', () => {
    expect(parseCnTime('2026-01-05T10:00:00').toISOString()).toBe('2026-01-05T02:00:00.000Z')
    expect(parseCnTime('2026-01-05 10:00').toISOString()).toBe('2026-01-05T02:00:00.000Z')
    expect(parseCnTime('2026-01-05').toISOString()).toBe('2026-01-04T16:00:00.000Z')
    expect(cnDateTime('2026-01-05T02:00:00Z')).toBe('2026-01-05 10:00:00')
    expect(cnDate('2026-01-05')).toBe('2026-01-05')
    expect(cnMonthDayTime('2026-01-05T10:07:09', true)).toBe('01-05 10:07:09')
  })

  it('非法输入返回空串/原串', () => {
    expect(cnDate('bad')).toBe('')
    expect(cnFormat('bad')).toBe('bad')
  })

  it('cnFormat 固定北京时区', () => {
    expect(cnFormat('2026-01-05T02:00:00Z', { hour: '2-digit', minute: '2-digit', hour12: false })).toBe('10:00')
  })

  it('shiftDate 纯日历运算', () => {
    expect(shiftDate('2026-03-01', { days: -1 })).toBe('2026-02-28')
    expect(shiftDate('2026-11-12', { months: -6 })).toBe('2026-05-12')
    expect(shiftDate('2024-02-29', { years: -1 })).toBe('2023-03-01')
    expect(cnDaysAgo(1, new Date('2026-11-11T16:30:00Z'))).toBe('2026-11-11')
  })
})
