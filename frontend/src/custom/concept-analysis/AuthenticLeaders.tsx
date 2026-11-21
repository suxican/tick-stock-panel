import { normThemeName, useThemeLeaders } from '@/lib/useMeozThemeAttribution'

/**
 * 概念详情的「精选板块最正宗」提示: 仅当存在与概念精确同名的精选板块时显示。
 * 只作证据, 不参与龙头分和评分; 名单只有当前快照; 无同名或来源不可用时不显示, 不当作没有龙头。
 */
export function AuthenticLeaders({ conceptName, stocks }: { conceptName: string; stocks: { symbol: string; name?: string | null }[] }) {
  const leaders = useThemeLeaders()
  const theme = leaders.data?.get(normThemeName(conceptName))
  if (!theme || !theme.symbols.length) return null
  const byCode = new Map(stocks.map(s => [s.symbol.slice(0, 6), s.name]))
  const change = theme.change_pct == null ? '' : `（精选板块当日 ${theme.change_pct >= 0 ? '+' : ''}${(theme.change_pct * 100).toFixed(2)}%）`
  return <p data-authentic-leaders className="mt-1.5 text-xs text-muted">
    精选板块最正宗：{theme.symbols.map(code => byCode.get(code) || code).join('、')}{change}
    <span className="ml-1 text-[11px]">· 当前名单，仅作参考</span>
  </p>
}
