import { navItemKey, uniqueBy, type NavItem } from '@/lib/listNav'

export type StockFields = { code: string; name: string }
export type OpenStock = (stock: NavItem, items: NavItem[]) => void

/** 仅用于已声明为个股的字段；不将板块或指数代码交给股票详情。 */
export function previewStock(raw: unknown, name: unknown): NavItem | null {
  if (typeof raw !== 'string') return null
  const match = /^(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?$/.exec(raw.trim().toUpperCase())
  if (!match) return null
  const [, prefix, code, suffix] = match
  const exchange = /^(60|68)/.test(code) ? 'SH' : /^(00|30)/.test(code) ? 'SZ'
    : /^(43|83|87|88|92)/.test(code) ? 'BJ' : null
  if (!exchange || prefix && prefix !== exchange || suffix && suffix !== exchange) return null
  return { symbol: `${code}.${exchange}`, name: typeof name === 'string' && name.trim() ? name.trim() : undefined }
}

export function previewItems(rows: Record<string, unknown>[], fields: StockFields): NavItem[] {
  return uniqueBy(rows.flatMap(row => {
    const stock = previewStock(row[fields.code], row[fields.name])
    return stock ? [stock] : []
  }), navItemKey)
}
