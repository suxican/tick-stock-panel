import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ExtDataConfig, type ExtDataRowsResult } from '@/lib/api'
import { resolveDimension } from '@/lib/analysis-adapter'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'

// 与概念、行业分析页共用当前扩展数据及查询缓存；不写入历史信号快照。
const ROW_LIMIT = 12000
const keywords = {
  concept: ['concept', '概念', 'theme', '题材', '板块'],
  industry: ['industry', '行业', 'sector', '申万', '中信'],
}
type Source = { config: ExtDataConfig; field: string }
export type Membership = { concepts: string[]; industries: string[] }

function selectSource(configs: ExtDataConfig[], kind: keyof typeof keywords): Source | undefined {
  const saved = kind === 'concept' ? storage.conceptAnalysisConfig.get({}) : storage.industryAnalysisConfig.get({})
  const available = configs.filter(config => !(config as ExtDataConfig & { market_level?: boolean }).market_level)
  const preferred = available.find(config => config.id === saved?.configId)
  const ordered = preferred ? [preferred, ...available.filter(config => config !== preferred)] : available
  for (const config of ordered) {
    const explicit = config === preferred && config.fields.find(field => field.name === saved?.dimensionField)
    const field = explicit || config.fields.find(field =>
      !['int', 'float', 'bool'].includes(field.dtype) && keywords[kind].some(word =>
        `${field.name} ${field.label ?? ''}`.toLowerCase().includes(word)))
    if (field) return { config, field: field.name }
  }
}

function membershipMap(data: ExtDataRowsResult | undefined, source: Source | undefined) {
  const map = new Map<string, Set<string>>()
  if (!data || !source || !data.rows.length) return { map, fieldMismatch: false }
  if (!data.fields.some(field => field.name === source.field)) return { map, fieldMismatch: true }
  // 优先使用配置中的完整字段名，避免 concept 与 concept_name 等字段互相误选。
  const fields = [...data.fields].sort((a, b) => Number(b.name === source.field) - Number(a.name === source.field))
  const resolved = resolveDimension({ ...data, fields }, source.config, [source.field])
  // 同 ID 的配置更新可能早于行缓存更新，禁止适配器回退到旧字段。
  if (resolved.dimensionField !== source.field) return { map, fieldMismatch: true }
  for (const group of resolved.groups) {
    for (const stock of group.stocks) {
      const symbol = String(stock.symbol || stock.code || '').trim().toUpperCase()
      if (!/^\d{6}(?:\.(?:SH|SZ|BJ))?$/.test(symbol)) continue
      const values = map.get(symbol) ?? new Set<string>()
      values.add(group.key)
      map.set(symbol, values)
    }
  }
  return { map, fieldMismatch: false }
}

export function useSectorMembership(enabled: boolean) {
  const configs = useQuery({ queryKey: QK.extData, queryFn: api.extDataList, enabled })
  const concept = selectSource(configs.data?.items ?? [], 'concept')
  const industry = selectSource(configs.data?.items ?? [], 'industry')
  const concepts = useQuery({
    queryKey: QK.extDataRows(concept?.config.id ?? '', undefined, ROW_LIMIT),
    queryFn: () => api.extDataRows(concept!.config.id, { limit: ROW_LIMIT }),
    enabled: enabled && !!concept,
  })
  const industries = useQuery({
    queryKey: QK.extDataRows(industry?.config.id ?? '', undefined, ROW_LIMIT),
    queryFn: () => api.extDataRows(industry!.config.id, { limit: ROW_LIMIT }),
    enabled: enabled && !!industry,
  })
  const conceptMembership = useMemo(() => membershipMap(concepts.data, concept), [concepts.data, concept?.config, concept?.field])
  const industryMembership = useMemo(() => membershipMap(industries.data, industry), [industries.data, industry?.config, industry?.field])
  const conceptMap = conceptMembership.map
  const industryMap = industryMembership.map
  const bySymbol = useMemo(() => {
    const result = new Map<string, Membership>()
    for (const symbol of new Set([...conceptMap.keys(), ...industryMap.keys()])) {
      const plain = symbol.split('.')[0]
      result.set(symbol, {
        concepts: [...(conceptMap.get(symbol) ?? conceptMap.get(plain) ?? [])].sort((a, b) => a.localeCompare(b, 'zh-CN')),
        industries: [...(industryMap.get(symbol) ?? industryMap.get(plain) ?? [])].sort((a, b) => a.localeCompare(b, 'zh-CN')),
      })
    }
    return result
  }, [conceptMap, industryMap])
  const error = configs.error ?? concepts.error ?? industries.error ??
    (conceptMembership.fieldMismatch || industryMembership.fieldMismatch ? new Error('字段配置已变化，请重新读取资料。') : null)
  const sources = [
    concept && `题材：${concept.config.label}（${concepts.data?.date ?? '当前快照'}）`,
    industry && `行业：${industry.config.label}（${industries.data?.date ?? '当前快照'}）`,
  ].filter(Boolean).join('；')
  return {
    bySymbol,
    sources,
    loading: configs.isLoading || concepts.isLoading || industries.isLoading,
    error: error ? new Error(`题材 / 板块读取失败：${error.message}`) : null,
    truncated: [concepts.data, industries.data].some(data => data && data.total > data.rows.length),
    retry: () => {
      if (configs.isError) void configs.refetch()
      if (concepts.isError || conceptMembership.fieldMismatch) void concepts.refetch()
      if (industries.isError || industryMembership.fieldMismatch) void industries.refetch()
    },
  }
}

export function SectorMembership({ value, loading }: { value?: Membership; loading: boolean }) {
  const [expanded, setExpanded] = useState(false)
  if (!value || (!value.concepts.length && !value.industries.length)) return <span className="text-secondary">{loading ? '读取中…' : '—'}</span>
  const shown = expanded ? value.concepts : value.concepts.slice(0, 2)
  return <div className="w-56 space-y-1.5 whitespace-normal break-words leading-relaxed">
    {value.industries.length > 0 && <div>{value.industries.join('、')}</div>}
    {shown.length > 0 && <div className="text-secondary">{shown.join('、')}</div>}
    {value.concepts.length > 2 && <button type="button" aria-expanded={expanded} className="min-h-7 rounded text-accent hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => setExpanded(previous => !previous)}>{expanded ? '收起题材' : `展开 ${value.concepts.length - 2} 项题材`}</button>}
  </div>
}
