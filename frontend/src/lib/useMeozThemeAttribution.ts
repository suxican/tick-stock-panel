import { useQuery } from '@tanstack/react-query'
import {
  api, ApiError,
  type MeozHotRank, type MeozHotStockKind, type MeozHotTheme, type MeozThemeReasons, type ThemeLeaders,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 可预期错误 (未配置猫爪 503 / 上游异常 502 / 日期口径 422) 静默为空, 页面显示「来源不可用」 */
async function quiet<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load()
  } catch (err) {
    if (err instanceof ApiError && [422, 502, 503].includes(err.status)) return null
    throw err
  }
}

/** 猫爪板块原因: 盘中 60 秒刷新, 定版后不再请求 */
export function meozThemeReasonsOptions(date: string | undefined) {
  return {
    queryKey: QK.meozThemeReasons(date ?? ''),
    queryFn: () => quiet(() => api.meozThemeReasons(date!)),
    enabled: !!date,
    refetchInterval: (query: { state: { data?: MeozThemeReasons | null } }) =>
      query.state.data && !query.state.data.final ? 60_000 : (false as const),
    staleTime: 60_000,
    retry: false,
  }
}

export function useMeozThemeReasons(date: string | undefined) {
  return useQuery(meozThemeReasonsOptions(date))
}

/** 精选板块最正宗名单 (只有当前快照, 后端缓存 30 分钟, 页面不轮询) */
export function useThemeAuthenticity(symbol: string | undefined) {
  return useQuery({
    queryKey: QK.meozThemeAuthenticity(symbol ?? ''),
    queryFn: () => quiet(() => api.meozThemeAuthenticity(symbol!)),
    enabled: !!symbol,
    staleTime: 30 * 60_000,
    retry: false,
  })
}

/** 同花顺概念名与精选板块名的精确同名键: 去掉全部空白 */
export const normThemeName = (name: string | null | undefined) => (name ?? '').replace(/\s+/g, '')

/** 精选板块最正宗名单, 按规整名称索引 (只有当前快照, 页面不轮询); 来源不可用为 null */
export function useThemeLeaders() {
  return useQuery({
    queryKey: QK.meozThemeLeaders,
    queryFn: async () => {
      const res = await quiet(() => api.meozThemeLeaders())
      if (!res) return null
      const map = new Map<string, ThemeLeaders['themes'][number]>()
      for (const theme of res.themes) if (theme.theme_name) map.set(normThemeName(theme.theme_name), theme)
      return map
    },
    staleTime: 30 * 60_000,
    retry: false,
  })
}

/** 猫爪热榜 (实时, 60 秒刷新, 保留上一份有效数据) */
export function useMeozHotRank(enabled = true) {
  return useQuery({
    queryKey: QK.meozHotRank,
    queryFn: () => quiet(() => api.meozHotRank()),
    enabled,
    refetchInterval: 60_000,
    staleTime: 60_000,
    retry: false,
  })
}

export const HOT_STOCK_LABELS: Record<MeozHotStockKind, string> = {
  ths_normal: '同花顺人气', dfcf_renqi: '东财人气', ths_trend: '同花顺飙升',
}

export type HotStockEvidence =
  | { state: 'unavailable' }
  | { state: 'unranked' }
  | { state: 'ranked'; rank: number }

/** 个股在某榜单的名次: 榜单取数失败是「来源不可用」, 与「未上榜」区分 */
export function hotStockEvidence(hot: MeozHotRank | null | undefined, kind: MeozHotStockKind, symbol: string): HotStockEvidence {
  const list = hot?.stocks?.[kind]
  if (!list || !list.available) return { state: 'unavailable' }
  const hit = list.rows.find(row => row.symbol === symbol)
  return hit ? { state: 'ranked', rank: hit.rank } : { state: 'unranked' }
}

/** 板块名与热搜精确同名才算命中; 名称体系不同, 不做模糊匹配, 未命中不代表没热度 */
export function hotThemeByName(hot: MeozHotRank | null | undefined, name: string | null | undefined): MeozHotTheme | null {
  if (!name || !hot?.themes?.available) return null
  return hot.themes.rows.find(row => row.name === name) ?? null
}
