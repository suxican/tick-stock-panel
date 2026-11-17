import type { KplStock, KplTheme } from '@/lib/api'
import { fmtBigNum } from '@/lib/format'

/** 梯队卡片的涨停原因及来源: 盘后 zzshare 优先, 开盘啦盘中原因作补充 */
export type LadderReason = {
  text: string
  source: 'kpl' | 'zzshare'
  alt?: string
  /** 开盘啦全量封单 (元), 只在展开区展示, 不影响 SealedBadge */
  seal?: { amount: number; max: number }
}

export function sealText(seal: { amount: number; max: number }): string {
  return `开盘啦封单 ${fmtBigNum(seal.amount)} · 最大 ${fmtBigNum(seal.max)}`
}

export function mergeReasons(
  zz: ReadonlyMap<string, string> | undefined,
  kpl: KplStock[] | undefined,
): Map<string, LadderReason> {
  const out = new Map<string, LadderReason>()
  zz?.forEach((text, symbol) => { if (text) out.set(symbol, { text, source: 'zzshare' }) })
  for (const s of kpl ?? []) {
    if (!s.reason) continue
    const prev = out.get(s.symbol)
    const seal = { amount: s.seal_amount, max: s.max_seal }
    if (prev) {
      out.set(s.symbol, prev.text !== s.reason ? { ...prev, alt: s.reason, seal } : { ...prev, seal })
    } else {
      out.set(s.symbol, { text: s.reason, source: 'kpl', seal })
    }
  }
  return out
}

/** 选中题材的成分股; 开盘啦快照与梯队不是同一交易日时不生效, 避免用今天的题材筛昨天的梯队 */
export function themeSymbolsFor(
  snapshot: { date: string; themes: KplTheme[] } | null | undefined,
  code: string | undefined,
  ladderDate: string | undefined,
): string[] | undefined {
  if (!snapshot || !code || snapshot.date !== ladderDate) return undefined
  return snapshot.themes.find(t => t.code === code)?.symbols
}

/** 只保留开盘啦题材成分内的股票; symbols 为空时原样返回 */
export function filterTiersBySymbols<T extends { count: number; stocks: { symbol: string }[] }>(
  tiers: T[],
  symbols: readonly string[] | undefined,
): T[] {
  if (!symbols) return tiers
  const keep = new Set(symbols)
  return tiers
    .map(t => {
      const stocks = t.stocks.filter(s => keep.has(s.symbol))
      return { ...t, stocks, count: stocks.length }
    })
    .filter(t => t.stocks.length > 0)
}
