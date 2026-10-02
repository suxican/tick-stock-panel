import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Database, RefreshCw } from 'lucide-react'
import { api, type KaipanlaContextTable } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

const STATE_LABELS = { ok: '已获取', partial: '部分可用', no_data: '暂无数据', error: '获取失败' }

function fetchedTime(value: string | number | null | undefined): string {
  if (!value) return '尚未获取'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '获取时间未知'
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(parsed)
}

function displayColumns(table: KaipanlaContextTable) {
  const columns = table.columns ?? Object.keys(table.rows[0] ?? {}).map(name => ({ name, label: name }))
  return columns.filter(column => /[\u4e00-\u9fff]/.test(column.label)
    && !/_json$/i.test(column.name)
    && !/^(reserved.*|record_id|UID|Image|Type|TagID|TagShuXing|source|date|fetched_at)$/i.test(column.name)
    && (table.id !== 'ext_kpl_live' || column.name === 'Time' || column.name === 'Comment')
    && table.rows.some(row => row[column.name] != null && ['string', 'number', 'boolean'].includes(typeof row[column.name])))
    .slice(0, 6)
}

function fieldValue(value: unknown, field?: string): string {
  if (value == null || value === '') return '—'
  if ((field === 'Time' || field === 'time' || field === 'limit_up_ts') && (typeof value === 'string' || typeof value === 'number')) {
    const timestamp = String(value)
    if (/^(\d{10}|\d{13})$/.test(timestamp)) {
      return fetchedTime(Number(timestamp) * (timestamp.length === 10 ? 1000 : 1))
    }
  }
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return typeof value === 'string' ? value : '—'
}

function isNumericValue(value: unknown): boolean {
  return typeof value === 'number'
    || (typeof value === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))
}

function SupplementTable({ table }: { table: KaipanlaContextTable }) {
  const columns = displayColumns(table)
  const failed = table.state === 'error' || table.state === 'date_mismatch'
  return (
    <section className="border-t border-border/60 py-3 first:border-t-0 first:pt-0">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="text-xs font-medium text-foreground">
          {table.label}
          {(table.id === 'ext_kpl_live' || table.id === 'ext_kpl_limit_reasons')
            && <span className="ml-2 text-[10px] font-normal text-secondary">来源观点 · 开盘啦解读</span>}
        </h3>
        <span className="text-[10px] text-secondary">
          {failed ? (table.rows.length ? '获取失败 · 保留上次快照' : '获取失败') : `${table.total} 条`}
          {table.fetched_at && ` · 获取于 ${fetchedTime(table.fetched_at)}`}
        </span>
      </div>
      {!table.rows.length ? (
        <p className="text-[11px] text-secondary">{failed ? '暂时无法获取，可再次获取补充数据。' : '该日暂无数据。'}</p>
      ) : !columns.length ? (
        <p className="text-[11px] text-secondary">已保存该日数据，暂无可展示的摘要字段。</p>
      ) : (
        <div className="overflow-x-auto">
          <table aria-label={table.label} className={cn('w-full text-left text-[11px]', columns.length > 3 && 'min-w-[32rem]')}>
            <thead className="text-secondary">
              <tr>{columns.map(column => <th key={column.name} className="px-2 pb-1.5 font-normal first:pl-0">{column.label}</th>)}</tr>
            </thead>
            <tbody>{table.rows.slice(0, 3).map((row, index) => (
              <tr key={index} className="border-t border-border/40">
                {columns.map(column => <td key={column.name} className="max-w-[36rem] px-2 py-1.5 align-top text-foreground first:pl-0">
                  <span className={cn('block', isNumericValue(row[column.name])
                    ? 'whitespace-nowrap tabular-nums'
                    : 'break-words [overflow-wrap:anywhere]')}>{fieldValue(row[column.name], column.name)}</span>
                </td>)}
              </tr>
            ))}</tbody>
          </table>
          {table.total > 3 && <p className="mt-1 text-[10px] text-secondary">显示前 3 条，共 {table.total} 条。</p>}
        </div>
      )}
    </section>
  )
}

/** 复盘补充数据独立于实时行情总开关，轮询只读取本地快照。 */
export function KaipanlaSupplement({ date, historical = false }: { date?: string; historical?: boolean }) {
  const client = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const query = useQuery({
    queryKey: QK.kaipanlaContext(date),
    queryFn: () => api.kaipanlaContext(date),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: false,
  })
  const refresh = useMutation({
    mutationFn: async (targetDate?: string) => {
      const result = await api.kaipanlaRefresh(targetDate)
      if (targetDate && result.context.date !== targetDate) throw new Error('补充数据日期不匹配')
      return result
    },
    onSuccess: (result, targetDate) => {
      // 请求完成前可能已切换历史报告，只写请求所属日期的缓存。
      client.setQueryData(QK.kaipanlaContext(targetDate), result.context)
      void client.invalidateQueries({ queryKey: QK.overviewMarket() })
      if (targetDate) void client.invalidateQueries({ queryKey: QK.overviewMarket(targetDate) })
    },
  })
  // 不沿用上一日期的 placeholder；额外拒绝服务端日期不匹配的快照。
  const mismatch = !!(date && query.data && query.data.date !== date)
  const data = mismatch ? undefined : query.data
  const tables = data?.tables.filter(table => table.date === data.date) ?? []
  const hasRows = tables.some(table => table.rows.length > 0)
  const refreshing = refresh.isPending && refresh.variables === date
  const refreshError = refresh.isError && refresh.variables === date
  const refreshFailed = refresh.isSuccess && refresh.variables === date
    ? refresh.data.results.filter(result => result.state === 'error').length : 0
  const isHistorical = historical || tables.some(table => table.retrieved_after_date)
  const summaries = tables.filter(table => /市场情绪|市场量能/.test(table.label)).flatMap(table =>
    displayColumns(table).slice(0, 3).map(column => ({
      key: `${table.id}.${column.name}`, label: column.label, value: fieldValue(table.rows[0]?.[column.name], column.name),
    })),
  ).slice(0, 5)

  return (
    <section aria-label="开盘啦补充数据" className="rounded-card border border-border bg-surface/80 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Database className="h-4 w-4 shrink-0 text-accent" />
        <h2 className="text-[13px] font-semibold text-foreground">开盘啦补充数据</h2>
        <span className={cn('text-[11px]', data?.state === 'partial' || data?.state === 'error' ? 'text-warning' : 'text-secondary')}>
          {query.isLoading ? '读取补充数据…' : data ? STATE_LABELS[data.state] : '暂不可用'}
        </span>
        <button
          type="button"
          disabled={refresh.isPending || query.isLoading}
          onClick={() => refresh.mutate(date)}
          className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3 w-3', refreshing && 'animate-spin motion-reduce:animate-none')} />
          {refreshing ? '获取中…' : '获取补充数据'}
        </button>
      </div>
      <p className="mt-2 text-[11px] text-secondary">
        行情归属 {data?.date ?? date ?? '最新交易日'} · 获取时间（北京时间）{fetchedTime(data?.fetched_at)}
      </p>
      {isHistorical && <p className="mt-1 text-[11px] text-secondary">后续获取的盘后或补充数据，不能视为当时盘中已知。</p>}
      {(refreshError || query.isError || mismatch) && <p role="status" className="mt-2 text-[11px] text-warning">
        {hasRows ? '获取失败，正在显示上次快照。' : '读取或获取失败，可再次获取补充数据。'}
      </p>}
      {refreshFailed > 0 && !refreshError && <p role="status" className="mt-2 text-[11px] text-warning">{refreshFailed} 项获取失败，已保留可用数据。</p>}
      {query.isLoading && <div aria-hidden="true" className="mt-3 h-3 w-2/3 animate-pulse rounded bg-elevated motion-reduce:animate-none" />}
      {!query.isLoading && !hasRows && !query.isError && !mismatch && <p className="mt-2 text-[11px] text-secondary">
        暂无该日补充数据。点击「获取补充数据」补充情绪、量能、涨停、板块、竞价和盘面解读。
      </p>}
      {hasRows && <>
        {summaries.length > 0 && <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px]">
          {summaries.map(item => <div key={item.key} className="flex items-baseline gap-1.5">
            <dt className="text-secondary">{item.label}</dt><dd className="font-mono tabular-nums text-foreground">{item.value}</dd>
          </div>)}
        </dl>}
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}
          className="mt-2 inline-flex items-center gap-1 text-[11px] text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
          {expanded ? '收起补充明细' : '查看补充明细'}
          <ChevronDown className={cn('h-3 w-3 transition-transform motion-reduce:transition-none', expanded && 'rotate-180')} />
        </button>
        {expanded && <div className="mt-3">{tables.map(table => <SupplementTable key={table.id} table={table} />)}</div>}
      </>}
    </section>
  )
}
