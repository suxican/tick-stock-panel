import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 可预期错误 (未配置猫爪 503 / 上游异常 502 / 日期口径 422) 静默为空, 页面显示「来源不可用」 */
export async function quiet<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load()
  } catch (err) {
    if (err instanceof ApiError && [422, 502, 503].includes(err.status)) return null
    throw err
  }
}

/**
 * 猫爪最强风口: 实时 60 秒刷新; 历史日按 tradedate 查询、不轮询。
 * 查询键含日期 (实时用 'live'), 不使用 placeholderData, 日期切换不会显示上一个日期的数据。
 */
export function meozStrongestVentOptions(date: string | undefined, live: boolean) {
  return {
    queryKey: QK.meozStrongestVent(live ? 'live' : (date ?? '')),
    queryFn: () => quiet(() => api.meozStrongestVent(live ? undefined : date)),
    enabled: live || !!date,
    refetchInterval: live ? 60_000 : (false as const),
    staleTime: 60_000,
    retry: false,
  }
}

export function useMeozStrongestVent(date: string | undefined, live: boolean) {
  return useQuery(meozStrongestVentOptions(date, live))
}

/** 猫爪新闻: 只有实时口径, 60 秒刷新; 回看历史日 (enabled=false) 不请求 */
export function meozNewsOptions(enabled: boolean) {
  return {
    queryKey: QK.meozNews,
    queryFn: () => quiet(() => api.meozNews(50)),
    enabled,
    refetchInterval: 60_000,
    staleTime: 60_000,
    retry: false,
  }
}

export function useMeozNews(enabled: boolean) {
  return useQuery(meozNewsOptions(enabled))
}
