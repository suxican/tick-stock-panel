import { lazy, Suspense } from 'react'
import type { DimensionIntradayResult, DimensionMembersResult } from '@/lib/api'
import type { NavItem } from '@/lib/listNav'

export type DimensionKind = 'concept' | 'industry'

interface ExtensionFieldTarget {
  kind: DimensionKind
  value: string
  /** 扩展字段完整标识，例如 ext_gn_ths.所属概念。 */
  sourceField: string
  date?: string
}

export interface DimensionQuery<T> {
  queryKey: readonly unknown[]
  queryFn: () => Promise<T>
}

/** 指定交易日的题材成员及行情由调用方一并提供。 */
export interface DatedThemeTarget {
  kind: 'theme'
  value: string
  date: string
  members: DimensionQuery<Pick<DimensionMembersResult, 'date' | 'rows' | 'total' | 'label'>>
  intraday: DimensionQuery<DimensionIntradayResult>
}
export type DimensionMembersTarget = ExtensionFieldTarget | DatedThemeTarget

export function dimensionTargetKey(target: DimensionMembersTarget) {
  return JSON.stringify(target.kind === 'theme' ? target.members.queryKey : [target.sourceField, target.value, target.date])
}

export interface Props {
  target: DimensionMembersTarget | null
  onClose: () => void
  onStockClick?: (symbol: string, name?: string, navList?: NavItem[]) => void
}

// 由 sourceField 判定维度类别 — 纯函数留在壳层, widgets/ScreenerTable/Watchlist
// 对本模块的静态引用因此不含 lightweight-charts
export function dimensionKindForSourceField(sourceField: string): DimensionKind | null {
  const separator = sourceField.indexOf('.')
  const field = (separator >= 0 ? sourceField.slice(separator + 1) : sourceField).trim().toLowerCase()
  if (/(概念|题材)|(?:^|[_\s])(concept|theme)(?:$|[_\s])/i.test(field)) return 'concept'
  if (/(行业|申万|中信)|(?:^|[_\s])(industry|sector)(?:$|[_\s])/i.test(field)) return 'industry'
  return null
}

// 懒加载壳: target 为空时不挂载内容 → lightweight-charts 不随各列表页首屏加载
const DimensionMembersDialogContent = lazy(() =>
  import('./DimensionMembersDialogContent').then(m => ({ default: m.DimensionMembersDialogContent })),
)

export function DimensionMembersDialog({ target, onClose, onStockClick }: Props) {
  if (!target) return null
  return (
    <Suspense fallback={null}>
      <DimensionMembersDialogContent
        key={dimensionTargetKey(target)}
        target={target}
        onClose={onClose}
        onStockClick={onStockClick}
      />
    </Suspense>
  )
}
