import { useThemeAuthenticity } from '@/lib/useMeozThemeAttribution'

function change(pct: number | null) {
  return pct == null ? '当日涨跌未知' : `当日 ${pct >= 0 ? '+' : ''}${(pct * 100).toFixed(2)}%`
}

/**
 * 精选板块正宗度 (首板评分依据展开区): 候选是否列入数据源精选板块的「最正宗」名单。
 * 只作展示, 不进入优先分; 名单只有当前快照。来源失败显示「来源不可用」, 不当作不正宗。
 */
export function MeozAuthenticEvidence({ symbol }: { symbol?: string }) {
  const authentic = useThemeAuthenticity(symbol)
  if (!symbol) return null
  if (authentic.isLoading) return <p data-meoz-authentic>精选板块最正宗加载中…</p>
  const value = authentic.data
  if (!value?.available) return <p data-meoz-authentic>精选板块最正宗 · 来源不可用</p>
  const themes = value.themes ?? []
  return <p data-meoz-authentic>
    {themes.length
      ? `精选板块最正宗 · ${themes.map(theme => `${theme.theme_name || theme.theme_id}（${change(theme.change_pct)}）`).join('；')}`
      : '未列入任何精选板块的最正宗名单'}
    <br />数据源当前名单，仅作参考，不进入优先分
  </p>
}
