import type { KplStock, KplTheme, MeozLimitReason } from '@/lib/api'
import { fmtBigNum } from '@/lib/format'

/**
 * 梯队卡片的涨停原因及来源: 猫爪 (开盘啦/韭菜公社/选股宝, 盘中即有) 为主;
 * zzshare 盘后详细解释作补充, 猫爪缺失时才作主文本; 开盘啦非官方接口只作补充。
 */
export type LadderReason = {
  text: string
  source: 'kpl' | 'zzshare' | 'meoz'
  /** 开盘啦非官方接口的题材 (与主文本不同时显示) */
  alt?: string
  /** 猫爪给出的三家原文; 主文本取其一, 展开区列出其余 */
  meoz?: Pick<MeozLimitReason, 'kpl' | 'jygs' | 'xgb'>
  /** zzshare 盘后归因原文 (主文本来自猫爪时作补充) */
  zz?: string
  /** 开盘啦全量封单 (元), 只在展开区展示, 不影响 SealedBadge */
  seal?: { amount: number; max: number }
}

export function sealText(seal: { amount: number; max: number }): string {
  return `开盘啦封单 ${fmtBigNum(seal.amount)} · 最大 ${fmtBigNum(seal.max)}`
}

export function mergeReasons(
  zz: ReadonlyMap<string, string> | undefined,
  kpl: KplStock[] | undefined,
  meoz?: ReadonlyMap<string, MeozLimitReason>,
): Map<string, LadderReason> {
  const out = new Map<string, LadderReason>()
  meoz?.forEach((row, symbol) => {
    const main = row.kpl || row.jygs || row.xgb
    if (main) out.set(symbol, { text: main, source: 'meoz', meoz: { kpl: row.kpl, jygs: row.jygs, xgb: row.xgb } })
  })
  zz?.forEach((text, symbol) => {
    if (!text) return
    const prev = out.get(symbol)
    out.set(symbol, prev ? { ...prev, zz: text } : { text, source: 'zzshare' })
  })
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

/**
 * 展开区的补充原文: zzshare 盘后解释、猫爪其余各家。与主文本相同的不重复;
 * zzshare 开头常与开盘啦主因相同 ("主因；详细解释"), 去掉重复的开头只留解释。
 */
export function detailLines(reason: LadderReason): { label: string; text: string }[] {
  const lines: { label: string; text: string | null | undefined }[] = []
  if (reason.zz) {
    const zz = reason.zz.startsWith(reason.text + '；') ? reason.zz.slice(reason.text.length + 1) : reason.zz
    lines.push({ label: 'ZZShare（盘后）', text: zz })
  }
  if (reason.meoz) {
    lines.push({ label: '开盘啦', text: reason.meoz.kpl }, { label: '韭菜公社', text: reason.meoz.jygs },
      { label: '选股宝', text: reason.meoz.xgb })
  }
  return lines.filter((line): line is { label: string; text: string } => !!line.text && line.text !== reason.text)
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
