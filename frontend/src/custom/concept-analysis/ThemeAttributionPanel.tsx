import { useMemo } from 'react'
import { cnToday } from '@/lib/cnTime'
import type { MeozThemeReason } from '@/lib/api'
import { useMeozHotRank, useMeozThemeReasons } from '@/lib/useMeozThemeAttribution'

const SOURCE_LABELS = { xgb: '选股宝', jygs: '韭菜公社' } as const

function clock(iso: string | null | undefined) {
  const m = iso?.match(/T(\d{2}:\d{2})/)
  return m ? m[1] : null
}

/**
 * 题材归因 (猫爪): 选股宝 / 韭菜公社的板块上涨原因 + 板块热搜 TOP5。
 * 只作证据展示, 不参与概念评分; 题材名称体系与同花顺概念不同, 仅精确同名时可跳转。
 * 默认收起; 缺来源显示「来源不可用」, 不当作没有题材。
 */
export function ThemeAttributionPanel({ conceptKeys, onSelectConcept }: {
  conceptKeys: ReadonlySet<string>
  onSelectConcept: (key: string) => void
}) {
  const date = cnToday()
  const reasons = useMeozThemeReasons(date)
  const hot = useMeozHotRank()
  const data = reasons.data
  const rows = data?.rows ?? []
  const grouped = useMemo(() => {
    const map = new Map<string, MeozThemeReason[]>()
    for (const row of rows) map.set(row.name, [...(map.get(row.name) ?? []), row])
    return [...map.entries()]
  }, [rows])
  const failed = data?.unavailable_sources ?? []
  const unavailable = reasons.isFetched && !data
  const themes = hot.data?.themes

  let summary: string
  if (reasons.isLoading) summary = '加载中…'
  else if (unavailable) summary = '来源不可用'
  else summary = `${grouped.length} 个题材${failed.length ? ` · ${failed.map(s => SOURCE_LABELS[s]).join('、')}不可用` : ''}`

  return (
    <details data-theme-attribution className="rounded-card border border-border bg-surface px-4 py-3 text-xs">
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 text-sm font-medium">
        <span>题材归因 · 猫爪</span>
        <span className="text-xs font-normal text-secondary">{summary}</span>
        {themes && (
          <span className="flex flex-wrap items-center gap-1 text-xs font-normal">
            <span className="text-secondary">热搜</span>
            {themes.available
              ? themes.rows.slice(0, 5).map(t => (
                <span key={t.theme_id} className="rounded bg-elevated px-1.5 py-0.5 tabular-nums">{t.rank} {t.name}</span>
              ))
              : <span className="text-secondary">来源不可用</span>}
          </span>
        )}
      </summary>
      <div className="mt-3 space-y-2">
        {unavailable && <p className="text-secondary">猫爪板块原因暂不可用（未配置或上游异常），不代表当日没有题材。</p>}
        {!unavailable && grouped.length === 0 && reasons.isFetched && !failed.length && (
          <p className="text-secondary">{date} 暂无板块原因。</p>
        )}
        {failed.length > 0 && !unavailable && (
          <p className="text-amber-500">{failed.map(s => SOURCE_LABELS[s]).join('、')}本次取数失败，下列结果不完整。</p>
        )}
        <ul className="grid grid-cols-1 gap-2 md:grid-cols-2 2xl:grid-cols-3">
          {grouped.map(([name, items]) => (
            <li key={name} data-theme-card className="flex flex-col gap-1.5 rounded-lg border border-border bg-elevated/40 p-3">
              <p className="flex items-center justify-between gap-2 text-sm font-medium">
                <span className="min-w-0 truncate" title={name}>{name}</span>
                {conceptKeys.has(name) && (
                  <button type="button" className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[11px] font-normal text-accent hover:bg-elevated"
                    onClick={() => onSelectConcept(name)}>概念榜同名</button>
                )}
              </p>
              <ul className="space-y-1.5">
                {items.map(item => (
                  <li key={`${item.source}-${item.theme_id}`} className="flex items-start gap-2 leading-relaxed text-secondary">
                    <span className="mt-0.5 shrink-0 rounded bg-surface px-1.5 py-px text-[11px] text-foreground">{SOURCE_LABELS[item.source]}</span>
                    <span className="min-w-0">{item.reason || '（无原文）'}</span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
        {(data?.fetched_at || themes?.rows[0]?.available_at) && (
          <p className="text-[11px] text-secondary">
            {data?.fetched_at && `原因获取 ${clock(data.fetched_at) ?? data.fetched_at}（接口无更新时间）`}
            {themes?.rows[0]?.available_at && ` · 热搜更新 ${clock(themes.rows[0].available_at)}`}
            {' · 选股宝/韭菜公社题材名称与同花顺概念不同，仅同名时可对应'}
          </p>
        )}
      </div>
    </details>
  )
}
