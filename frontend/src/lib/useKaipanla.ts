import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type KplSnapshot } from '@/lib/api'
import { cnToday } from '@/lib/cnTime'
import { QK } from '@/lib/queryKeys'

const NO_RETRY = new Set([404, 409, 503])

/** 开盘啦快照查询: 当日 30 秒轮询, 历史日期读留档且无档返回 null。 */
export function kaipanlaIntradayOptions(date: string | undefined, enabled: boolean, today = cnToday()) {
  const live = !date || date === today
  const day = live ? undefined : date
  return {
    queryKey: QK.kaipanla(day),
    queryFn: async (): Promise<KplSnapshot | null> => {
      try {
        return await api.kaipanlaIntraday(day)
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }
    },
    enabled,
    refetchInterval: live ? 30_000 : (false as const),
    staleTime: live ? 0 : Infinity,
    retry: (count: number, err: unknown) => !(err instanceof ApiError && NO_RETRY.has(err.status)) && count < 1,
  }
}

export function useKaipanlaStatus() {
  return useQuery({ queryKey: QK.kaipanlaStatus, queryFn: api.kaipanlaStatus, staleTime: 60_000 })
}

export function useKaipanlaIntraday(date: string | undefined, enabled: boolean) {
  return useQuery(kaipanlaIntradayOptions(date, enabled))
}
