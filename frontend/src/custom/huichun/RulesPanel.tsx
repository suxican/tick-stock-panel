import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, type HuichunConfig, type HuichunRules } from '@/lib/api'
import { buttonClass, ErrorNotice, inputClass, Notice, panelClass, primaryClass } from './shared'

const fields: { key: keyof HuichunRules; label: string; hint: string; min: number; max: number; scale: number; step: number }[] = [
  { key: 'rally_threshold', label: '前段最小涨幅（%）', hint: '前一轮金叉至死叉的收盘涨幅下限。', min: 0, max: 1000, scale: 100, step: .01 },
  { key: 'zero_threshold', label: '零轴距离上限（%）', hint: '回春金叉时 DIF、DEA 相对价格的距离上限。', min: 0, max: 100, scale: 100, step: .01 },
  { key: 'ma_window', label: '趋势均线周期（交易日）', hint: '信号日收盘价需位于该均线上方。', min: 2, max: 500, scale: 1, step: 1 },
  { key: 'slope_lag', label: '均线比较间隔（交易日）', hint: '当前均线需高于该间隔之前的均线。', min: 1, max: 250, scale: 1, step: 1 },
  { key: 'warmup_bars', label: '最少预热日线数', hint: '前段金叉前需要连续、有效的历史日线。', min: 1, max: 2000, scale: 1, step: 1 },
]
type Draft = { revision: number; values: Record<keyof HuichunRules, string> }
function draftFrom(config: HuichunConfig): Draft {
  return { revision: config.revision, values: Object.fromEntries(fields.map(field => [field.key, String(Number((config.rules[field.key] * field.scale).toFixed(8)))])) as Draft['values'] }
}
function parseDraft(draft: Draft): { rules: HuichunRules; error: string | null } {
  const rules = {} as HuichunRules
  for (const field of fields) {
    const raw = draft.values[field.key]
    const value = Number(raw)
    if (!raw.trim() || !Number.isFinite(value) || value < field.min || value > field.max || (field.scale === 1 && !Number.isInteger(value))) {
      return { rules, error: `${field.label}需填写 ${field.min}～${field.max} ${field.scale === 1 ? '之间的整数' : '之间的数值'}。` }
    }
    rules[field.key] = value / field.scale
  }
  return { rules, error: null }
}

export function RulesPanel({ config, onSaved, reload }: { config: HuichunConfig; onSaved: (config: HuichunConfig) => void; reload: () => Promise<HuichunConfig | undefined> }) {
  const [draft, setDraft] = useState(() => draftFrom(config))
  const [savedRevision, setSavedRevision] = useState<number | null>(null)
  const save = useMutation({
    mutationFn: api.huichunSaveConfig,
    onSuccess: next => { setDraft(draftFrom(next)); setSavedRevision(next.revision); onSaved(next) },
  })
  const parsed = parseDraft(draft)
  const changed = JSON.stringify(draft.values) !== JSON.stringify(draftFrom(config).values)
  useEffect(() => { if (changed) setSavedRevision(null) }, [changed])
  return <section className={`${panelClass} max-w-4xl space-y-5`}>
    <div><h2 className="text-sm font-semibold">回春 A0 日线规则 <span className="ml-2 font-normal text-secondary">版本 {config.revision}</span></h2><p className="mt-2 max-w-prose text-xs leading-relaxed text-secondary">先经历符合涨幅要求的金叉与死叉，再寻找零轴附近的首次金叉，并确认价格与均线趋势。MACD 固定为 10 / 20 / 9。</p></div>
    {config.revision !== draft.revision && <Notice>规则已在其他页面更新到版本 {config.revision}。请重新载入后再编辑，以免覆盖新的规则。</Notice>}
    {savedRevision !== null && <Notice>已保存为版本 {savedRevision}。重新筛选后使用新规则，已加入跟踪的股票保留原规则。</Notice>}
    <form className="space-y-5" onSubmit={event => { event.preventDefault(); if (!parsed.error) save.mutate({ rules: parsed.rules, expected_revision: draft.revision }) }}>
      <div className="grid gap-x-6 gap-y-5 sm:grid-cols-2">{fields.map(field => <label key={field.key} className="block space-y-2 text-sm"><span>{field.label}</span><input className={inputClass} type="number" inputMode="decimal" required min={field.min} max={field.max} step={field.step} value={draft.values[field.key]} disabled={save.isPending} onChange={event => { const value = event.target.value; setDraft(previous => ({ ...previous, values: { ...previous.values, [field.key]: value } })); save.reset() }} /><span className="block text-xs leading-relaxed text-secondary">{field.hint}</span></label>)}</div>
      {parsed.error && <Notice error>{parsed.error}</Notice>}
      <ErrorNotice error={save.error} />
      <div className="flex flex-wrap items-center gap-3"><button type="submit" className={primaryClass} disabled={save.isPending || !!parsed.error || !changed || config.revision !== draft.revision}>{save.isPending ? '保存中' : '保存规则'}</button><button type="button" className={buttonClass} disabled={save.isPending} onClick={async () => { const latest = await reload(); if (latest) { setDraft(draftFrom(latest)); setSavedRevision(null); save.reset() } }}>重新载入已保存规则</button><span className="text-xs text-secondary">{changed ? '有未保存的调整' : '当前规则已保存'}</span></div>
    </form>
  </section>
}
