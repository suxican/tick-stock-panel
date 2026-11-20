import type { MeozHotStockKind } from '@/lib/api'
import { HOT_STOCK_LABELS, hotStockEvidence, hotThemeByName, useMeozHotRank } from '@/lib/useMeozThemeAttribution'

const KINDS: MeozHotStockKind[] = ['dfcf_renqi', 'ths_trend', 'ths_normal']

function clock(iso: string | null | undefined) {
  const m = iso?.match(/T(\d{2}:\d{2}:\d{2})/)
  return m ? m[1] : '—'
}

/**
 * 猫爪热榜补充证据 (首板评分依据展开区): 东财人气 / 同花顺飙升 / 同花顺人气名次 + 所属板块热搜名次。
 * 只作展示, 不进入优先分; 个股热榜无上游时间戳, 只标获取时间。来源失败显示「来源不可用」, 不当作未上榜。
 */
export function MeozHotEvidence({ symbol, boardName }: { symbol?: string; boardName?: string | null }) {
  const hot = useMeozHotRank(!!symbol)
  if (!symbol) return null
  if (hot.isLoading) return <p data-meoz-hot>猫爪热榜加载中…</p>
  if (!hot.data) return <p data-meoz-hot>猫爪热榜 · 来源不可用</p>
  const theme = hotThemeByName(hot.data, boardName)
  return <div data-meoz-hot className="space-y-0.5">
    {KINDS.map(kind => {
      const ev = hotStockEvidence(hot.data, kind, symbol)
      const text = ev.state === 'ranked' ? `第 ${ev.rank} 名` : ev.state === 'unranked' ? '未上榜' : '来源不可用'
      return <p key={kind}>猫爪{HOT_STOCK_LABELS[kind]} · {text}</p>
    })}
    {theme && <p>所属板块 {theme.name} · 猫爪热搜第 {theme.rank} 名{theme.available_at && `（更新 ${clock(theme.available_at)}）`}</p>}
    <p>热榜获取 {clock(hot.data.fetched_at)}（无上游时间戳，仅作参考）</p>
  </div>
}
