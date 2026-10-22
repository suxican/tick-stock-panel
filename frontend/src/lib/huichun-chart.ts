import type { UseQueryOptions } from '@tanstack/react-query'
import { api, type HuichunRules, type KlineDailyResponse } from './api'
import { klineDailyQueryOptions } from './kline'
import { QK } from './queryKeys'
import type { ChartMarker } from '@/components/EChartsCandlestick'

/** 来自选中候选或跟踪记录的不可变规则与形态日期，不表示真实成交。 */
export interface HuichunChartContext {
  symbol: string
  as_of: string
  rules: HuichunRules
  rule_revision: number
  g_date?: string
  d_date?: string
  signal_date?: string
}

export function huichunChartRequest(context: HuichunChartContext, range: { start: string; end: string }) {
  return {
    symbol: context.symbol,
    as_of: context.as_of,
    rules: context.rules,
    rule_revision: context.rule_revision,
    start_date: range.start,
    end_date: range.end < context.as_of ? range.end : context.as_of,
  }
}

/** 信息条和日K共用；回春失败或上下文不匹配时不能回退到另一套 MACD。 */
export function stockChartQueryOptions(
  symbol: string,
  range: { start: string; end: string },
  extColumns?: string,
  huichun?: HuichunChartContext,
): UseQueryOptions<KlineDailyResponse, Error> {
  if (!huichun) return { ...klineDailyQueryOptions(symbol, range, extColumns), enabled: !!symbol }
  const body = huichunChartRequest(huichun, range)
  return {
    queryKey: QK.huichunChart(body),
    queryFn: async () => {
      const result = await api.huichunChart(body)
      const metadata = result.huichun
      if (result.symbol !== body.symbol || result.source !== 'huichun_a0'
        || metadata?.as_of !== body.as_of || metadata?.macd?.join('/') !== '10/20/9'
        || metadata?.price_basis !== 'as_of_adjusted' || metadata?.rule_revision !== body.rule_revision
        || (Object.keys(body.rules) as Array<keyof HuichunRules>).some(key => metadata?.rules?.[key] !== body.rules[key])) {
        throw new Error('回春图表数据与所选股票、日期或规则不匹配')
      }
      return result
    },
    enabled: !!symbol && huichun.symbol === symbol && body.start_date <= body.end_date,
    // 不跨股票、形态日或规则版本显示旧序列；同一查询后台刷新仍保留原数据。
    placeholderData: undefined,
    staleTime: 30_000,
  }
}

export function huichunChartRange(context: HuichunChartContext) {
  const start = new Date(`${context.as_of}T00:00:00Z`)
  start.setUTCMonth(start.getUTCMonth() - 6)
  const defaultStart = start.toISOString().slice(0, 10)
  return { start: context.g_date && context.g_date < defaultStart ? context.g_date : defaultStart, end: context.as_of }
}

export function huichunChartMarkers(context: HuichunChartContext): ChartMarker[] {
  return ([
    [context.g_date, 'G', '#EAB308'],
    [context.d_date, 'D', '#8B5CF6'],
    [context.signal_date, 'R', '#14B8A6'],
  ] as const).flatMap(([date, label, color]) => date
    ? [{ date, label, color, kind: 'neutral' as const, above: true }]
    : [])
}
