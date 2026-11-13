/**
 * 北京时间工具 — A 股业务统一按北京时间 (Asia/Shanghai, UTC+8, 无夏令时) 处理。
 *
 * 浏览器本地时区不可靠 (海外用户、CI 的 UTC), 不能用 getHours()/getDate() 这类本地分量,
 * 也不能用 toISOString().slice(0, 10) (那是 UTC 日期, 北京 00:00-08:00 会早一天)。
 * 与后端 app/market_time.py 的 cn_now / cn_today 同口径。
 */

export const CN_TIME_ZONE = 'Asia/Shanghai'

export type DateInput = Date | number | string

// 构造 Intl.DateTimeFormat 有成本, 固定 options 提到模块级复用。
const CN_PARTS = new Intl.DateTimeFormat('en-CA', {
  timeZone: CN_TIME_ZONE,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  weekday: 'short',
  hourCycle: 'h23',
})

const WEEKDAY_INDEX: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }

export interface CnParts {
  year: number
  /** 1-12 */
  month: number
  day: number
  hour: number
  minute: number
  second: number
  /** 0=周日 … 6=周六 */
  weekday: number
}

const pad = (n: number) => String(n).padStart(2, '0')

const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i

/**
 * 解析时间输入。数字/Date 原样; 带时区的串按其时区; 无时区的串 (后端 naive 墙钟,
 * 如 '2026-01-05T10:00:00' 或 '2026-01-05') 一律按北京时间解析, 不按浏览器本地时区猜测。
 */
export function parseCnTime(value: DateInput): Date {
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value)
  const raw = value.trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return new Date(`${raw}T00:00:00+08:00`)
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(raw) && !HAS_ZONE.test(raw)) {
    return new Date(`${raw.replace(' ', 'T')}+08:00`)
  }
  return new Date(raw)
}

/** 按北京时间格式化 (等价于 toLocaleString 并固定 timeZone); 非法时间返回原串。 */
export function cnFormat(value: DateInput, options: Intl.DateTimeFormatOptions = {}, locale = 'zh-CN'): string {
  const d = parseCnTime(value)
  if (isNaN(d.getTime())) return typeof value === 'string' ? value : ''
  return d.toLocaleString(locale, { ...options, timeZone: CN_TIME_ZONE })
}

/** 某一时刻在北京时间下的年月日时分秒; 非法时间返回 null。 */
export function cnParts(value: DateInput = new Date()): CnParts | null {
  const d = parseCnTime(value)
  if (isNaN(d.getTime())) return null
  const out: Record<string, string> = {}
  for (const part of CN_PARTS.formatToParts(d)) out[part.type] = part.value
  return {
    year: Number(out.year), month: Number(out.month), day: Number(out.day),
    hour: Number(out.hour), minute: Number(out.minute), second: Number(out.second),
    weekday: WEEKDAY_INDEX[out.weekday] ?? 0,
  }
}

/** 北京时间今日 'YYYY-MM-DD'。 */
export function cnToday(now: DateInput = new Date()): string {
  return cnDate(now)
}

/** 某一时刻的北京日期 'YYYY-MM-DD'; 非法返回 ''。 */
export function cnDate(value: DateInput): string {
  const p = cnParts(value)
  return p ? `${p.year}-${pad(p.month)}-${pad(p.day)}` : ''
}

/** 某一时刻的北京日期时间 'YYYY-MM-DD HH:mm:ss'; 非法返回 ''。 */
export function cnDateTime(value: DateInput): string {
  const p = cnParts(value)
  return p ? `${p.year}-${pad(p.month)}-${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}:${pad(p.second)}` : ''
}

/** 某一时刻的北京时间 'MM-DD HH:mm' (seconds=true 时带秒); 非法返回 ''。 */
export function cnMonthDayTime(value: DateInput, seconds = false): string {
  const p = cnParts(value)
  if (!p) return ''
  const base = `${pad(p.month)}-${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}`
  return seconds ? `${base}:${pad(p.second)}` : base
}

/**
 * 日历日期加减 ('YYYY-MM-DD' → 'YYYY-MM-DD'), 纯日历运算, 与时区无关。
 * 月/年溢出语义同 Date#setMonth (如 03-31 减 1 月 → 03-03)。
 */
export function shiftDate(date: string, delta: { days?: number; months?: number; years?: number }): string {
  const [y, m, d] = date.slice(0, 10).split('-').map(Number)
  const t = new Date(Date.UTC(y, m - 1, d))
  if (delta.years) t.setUTCFullYear(t.getUTCFullYear() + delta.years)
  if (delta.months) t.setUTCMonth(t.getUTCMonth() + delta.months)
  if (delta.days) t.setUTCDate(t.getUTCDate() + delta.days)
  return t.toISOString().slice(0, 10)
}

/** 北京今日往前 n 天 'YYYY-MM-DD' (n 为负则往后)。 */
export function cnDaysAgo(n: number, now: DateInput = new Date()): string {
  return shiftDate(cnToday(now), { days: -n })
}
