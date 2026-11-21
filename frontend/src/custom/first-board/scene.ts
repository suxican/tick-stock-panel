import { cnParts } from '@/lib/cnTime'

export type Scene = 'preopen' | 'afterClose' | 'normal'

/**
 * 进入首板页时的使用场景（北京时间，与浏览器时区无关）：
 * 工作日 09:15–09:30 为竞价前，工作日 15:00 以后为收盘后，其余（含周末）为普通。不判断节假日。
 */
export function sceneAt(now: number = Date.now()): Scene {
  const parts = cnParts(now)
  if (!parts || parts.weekday === 0 || parts.weekday === 6) return 'normal'
  const minutes = parts.hour * 60 + parts.minute
  if (minutes >= 9 * 60 + 15 && minutes < 9 * 60 + 30) return 'preopen'
  return minutes >= 15 * 60 ? 'afterClose' : 'normal'
}
