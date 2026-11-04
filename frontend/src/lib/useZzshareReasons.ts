import { queryOptions, skipToken, useQuery } from '@tanstack/react-query'
import { api, ApiError, type ExtDataRowsResult } from './api'
import { QK } from './queryKeys'

const TABLE = 'zzshare_limit_reasons'
const LIMIT = 20000

// 三个入口共用原始行缓存。缺日期时不请求，以免扩展表接口默认返回最新分区。
export function zzshareReasonsOptions(date?: string | null) {
  return queryOptions({
    queryKey: QK.extDataRows(TABLE, date || undefined, LIMIT),
    queryFn: date ? async () => {
      let result: ExtDataRowsResult
      try {
        result = await api.extDataRows(TABLE, { date, limit: LIMIT, quiet: true })
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }
      if (result.rows.length && (result.date !== date || result.rows.some(row =>
        row.trade_date !== date || typeof row.symbol !== 'string' || typeof row.reason !== 'string'
      ))) {
        throw new Error('归因快照的交易日或字段异常，请检查扩展数据。')
      }
      return result
    } : skipToken,
    staleTime: query => query.state.data?.rows.length ? 60_000 : 0,
    retry: false,
  })
}

function bySymbol(result: ExtDataRowsResult | null): ReadonlyMap<string, string> {
  return new Map(result?.rows.map(row => [row.symbol, row.reason]))
}

export function useZzshareReasons(date?: string | null) {
  return useQuery({ ...zzshareReasonsOptions(date), select: bySymbol })
}
