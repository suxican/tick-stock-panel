import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, X } from 'lucide-react'
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
  const id = useId()
  const anchor = useRef<HTMLButtonElement>(null)
  const popup = useRef<HTMLDivElement>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const [position, setPosition] = useState<{ top: number; left: number; width: number } | null>(null)
  useEffect(() => { setPosition(null) }, [value])
  useEffect(() => {
    if (!position) return
    closeButton.current?.focus()
    const dismiss = (event: Event) => {
      if (event.target instanceof Node && (popup.current?.contains(event.target) || anchor.current?.contains(event.target))) return
      setPosition(null)
    }
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' || (event.key === 'Tab' && popup.current?.contains(document.activeElement))) {
        event.preventDefault()
        setPosition(null)
        anchor.current?.focus()
      }
    }
    const resize = () => setPosition(null)
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', keydown)
    window.addEventListener('scroll', dismiss, true)
    window.addEventListener('resize', resize)
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', keydown)
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('resize', resize)
    }
  }, [position])
  if (!value || (!value.concepts.length && !value.industries.length)) return <span className="text-secondary">{loading ? '读取中…' : '—'}</span>
  const summary = [value.industries[0], value.concepts[0]].filter(Boolean).join(' · ')
  return <>
    <button ref={anchor} type="button" aria-label="查看全部题材与行业" aria-expanded={!!position} aria-controls={position ? id : undefined} aria-haspopup="dialog"
      title={summary} className="flex min-h-8 w-36 items-center gap-1 rounded text-left text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-accent"
      onClick={() => {
        if (position) { setPosition(null); return }
        const rect = anchor.current!.getBoundingClientRect()
        const width = Math.min(288, window.innerWidth - 24)
        setPosition({ width, left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
          top: Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - 292)) })
      }}><span className="min-w-0 flex-1 truncate">{summary}</span><ChevronDown className="h-3 w-3 shrink-0" aria-hidden="true" /></button>
    {position && createPortal(<div id={id} ref={popup} role="dialog" aria-label="题材与行业" style={position}
      className="fixed z-[70] max-h-[min(280px,calc(100dvh-24px))] overflow-y-auto rounded-card border border-border bg-surface p-3 text-xs text-foreground">
      <div className="mb-2 flex items-center justify-between gap-3"><span className="font-medium">当前题材与行业</span>
        <button ref={closeButton} type="button" aria-label="收起题材与行业" className="inline-flex h-8 w-8 items-center justify-center rounded-btn text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-accent"
          onClick={() => { setPosition(null); anchor.current?.focus() }}><X className="h-3.5 w-3.5" aria-hidden="true" /></button>
      </div>
      <dl className="space-y-3 whitespace-normal break-words leading-relaxed">
        {value.industries.length > 0 && <div><dt className="mb-1 text-secondary">行业</dt><dd>{value.industries.join('、')}</dd></div>}
        {value.concepts.length > 0 && <div><dt className="mb-1 text-secondary">题材 · {value.concepts.length} 项</dt><dd>{value.concepts.join('、')}</dd></div>}
      </dl>
    </div>, document.body)}
  </>
}
