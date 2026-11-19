import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type MeozLimitReason, type MeozLimitReasons } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 猫爪归因: 盘中 60 秒刷新, 定版后不再请求; 插件未就绪 (503) 等可预期错误静默为空。 */
export function meozReasonsOptions(date: string | undefined) {
  return {
    queryKey: QK.meozLimitReasons(date ?? ''),
    queryFn: async (): Promise<MeozLimitReasons | null> => {
      try {
        return await api.meozLimitReasons(date!)
      } catch (err) {
        if (err instanceof ApiError && [422, 502, 503].includes(err.status)) return null
        throw err
      }
    },
    enabled: !!date,
    refetchInterval: (query: { state: { data?: MeozLimitReasons | null } }) =>
      query.state.data && !query.state.data.final ? 60_000 : (false as const),
    staleTime: 60_000,
    retry: false,
  }
}

function bySymbol(result: MeozLimitReasons | null): ReadonlyMap<string, MeozLimitReason> {
  return new Map((result?.rows ?? []).map(row => [row.symbol, row]))
}

export function useMeozReasons(date: string | undefined) {
  return useQuery({ ...meozReasonsOptions(date), select: bySymbol })
}
