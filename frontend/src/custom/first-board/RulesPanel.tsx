import type { FormEvent } from 'react'
import type { FirstBoardConfig, FirstBoardRules, PaperAccountSummary } from '@/lib/api'
import { buttonClass, ErrorNotice, inputClass, marketStates, Notice, panelClass, patternLabels, stamp, universeLabels } from './shared'

type NumericRule = Exclude<keyof FirstBoardRules, 'universe' | 'enabled_patterns'>
type Field = { key: NumericRule; label: string; min: number; max: number; step?: number; scale?: number }
const groups: { title: string; fields: Field[] }[] = [
  { title: '候选范围与实时确认', fields: [
    { key: 'lookback_days', label: '最近无收盘涨停的交易日数', min: 1, max: 60 },
    { key: 'min_history_days', label: '最少历史交易日数', min: 20, max: 250 },
    { key: 'approaching_distance', label: '距涨停价最多（%）', min: 0, max: 10, step: 0.1, scale: 100 },
    { key: 'min_change_pct', label: '盘中最低涨幅（%）', min: 0, max: 20, step: 0.1, scale: 100 },
    { key: 'min_turnover_rate', label: '最低换手率（%）', min: 0, max: 100, step: 0.1 },
    { key: 'max_turnover_rate', label: '最高换手率（%）', min: 0.1, max: 100, step: 0.1 },
    { key: 'min_amount', label: '最低成交额（万元）', min: 0, max: 100000000, step: 100, scale: 0.0001 },
  ] },
  { title: '平台突破', fields: [
    { key: 'platform_window', label: '平台观察交易日数', min: 5, max: 60 },
    { key: 'platform_max_range', label: '平台最大振幅（%）', min: 0.1, max: 100, step: 0.1, scale: 100 },
    { key: 'platform_near_high', label: '距平台高点最多（%）', min: 0, max: 50, step: 0.1, scale: 100 },
  ] },
  { title: '趋势加速', fields: [
    { key: 'trend_window', label: '趋势观察交易日数', min: 10, max: 60 },
    { key: 'trend_min_return', label: '趋势累计涨幅下限（%）', min: 0, max: 100, step: 0.1, scale: 100 },
    { key: 'trend_max_return', label: '趋势累计涨幅上限（%）', min: 0.1, max: 200, step: 0.1, scale: 100 },
  ] },
  { title: '超跌反弹', fields: [
    { key: 'oversold_window', label: '超跌观察交易日数', min: 20, max: 120 },
    { key: 'oversold_min_drawdown', label: '最低回撤幅度（%）', min: 0.1, max: 99.9, step: 0.1, scale: 100 },
  ] },
]

export function validateDraft(config: FirstBoardConfig): string | null {
  if (config.rules.trend_min_return > config.rules.trend_max_return) return '趋势涨幅下限不能大于上限。'
  if (config.rules.min_turnover_rate > config.rules.max_turnover_rate) return '最低换手率不能高于最高换手率。'
  if (config.max_stock_weight > config.total_exposure) return '单股仓位上限不能高于总仓位上限。'
  if (config.max_stock_weight > config.max_sector_weight) return '单股仓位上限不能高于同行业仓位上限。'
  return null
}

export function RulesPanel({ config, draft, onDraft, onSave, versions, accounts, saving, error, reload }: {
  config: FirstBoardConfig; draft: FirstBoardConfig; onDraft: (draft: FirstBoardConfig) => void
  onSave: () => void; versions: FirstBoardConfig[]; accounts: PaperAccountSummary[]
  saving: boolean; error: Error | null; reload: () => void
}) {
  const dirty = JSON.stringify(draft) !== JSON.stringify(config)
  const invalid = validateDraft(draft)
  function rule(key: NumericRule, value: number) {
    if (Number.isFinite(value)) onDraft({ ...draft, rules: { ...draft.rules, [key]: value } })
  }
  function submit(event: FormEvent) { event.preventDefault(); if (!invalid) onSave() }
  const configNumber = (key: 'max_positions' | 'max_quote_age_seconds' | 'max_stock_weight' | 'max_sector_weight' | 'total_exposure' | 'exit_loss_pct', label: string, min: number, max: number, scale = 1) =>
    <label className="space-y-1.5 text-xs text-secondary" key={key}><span>{label}</span><input className={inputClass} type="number" required min={min} max={max} step={scale === 100 ? 0.1 : 1} value={Number((draft[key] * scale).toFixed(4))} onChange={e => { if (Number.isFinite(e.target.valueAsNumber)) onDraft({ ...draft, [key]: e.target.valueAsNumber / scale }) }} /></label>

  return <div className="space-y-4">
    <Notice>规则数值是用于验证的实验参数。草稿范围为{universeLabels[draft.rules.universe]}，最近 {draft.rules.lookback_days} 个交易日没有收盘涨停。修改只在保存后生效；历史版本保留用于追溯。</Notice>
    {config.revision !== draft.revision && <Notice error>当前配置已更新至版本 {config.revision}。草稿基于版本 {draft.revision}，请先重新载入，避免覆盖其他修改。<button className={`${buttonClass} ml-3`} onClick={reload}>重新载入当前版</button></Notice>}
    <ErrorNotice error={error} />
    <form className="space-y-4" onSubmit={submit}>
      <fieldset disabled={saving} className={`${panelClass} space-y-4`}>
        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">运行设置</h2><span className="text-xs text-secondary">当前版本 {config.revision} · {stamp(config.updated_at)}</span></div>
        <label className="block max-w-md space-y-1.5 text-xs text-secondary"><span>候选市场范围</span><select className={inputClass} value={draft.rules.universe} onChange={event => onDraft({ ...draft, rules: { ...draft.rules, universe: event.target.value as FirstBoardRules['universe'] } })}>{Object.entries(universeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <div className="flex flex-wrap gap-x-6 gap-y-3 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={draft.enabled} onChange={e => onDraft({ ...draft, enabled: e.target.checked })} />开启自动盯盘</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={draft.notify} onChange={e => onDraft({ ...draft, notify: e.target.checked })} />发送信号通知</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={draft.require_sector_confirmation} onChange={e => onDraft({ ...draft, require_sector_confirmation: e.target.checked })} />要求行业联动确认</label>
        </div>
        <p className="max-w-prose text-xs leading-relaxed text-secondary">行业联动采用实验判据：至少 3 只有效成员、其中至少 2 只涨幅达 5%，行业平均涨幅至少 1%。它用于辅助确认，不能等同完整题材主线。</p>
        <fieldset className="space-y-2"><legend className="mb-2 text-xs text-secondary">允许提示买入的市场环境</legend><div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">{marketStates.map(item => <label key={item.value} className="flex items-center gap-2"><input type="checkbox" checked={draft.allowed_market_states.includes(item.value)} onChange={e => onDraft({ ...draft, allowed_market_states: e.target.checked ? [...draft.allowed_market_states, item.value] : draft.allowed_market_states.filter(value => value !== item.value) })} />{item.label}</label>)}</div></fieldset>
        <fieldset className="space-y-2"><legend className="mb-2 text-xs text-secondary">启用形态</legend><div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">{(Object.keys(patternLabels) as (keyof typeof patternLabels)[]).map(pattern => <label key={pattern} className="flex items-center gap-2"><input type="checkbox" checked={draft.rules.enabled_patterns.includes(pattern)} onChange={e => onDraft({ ...draft, rules: { ...draft.rules, enabled_patterns: e.target.checked ? [...draft.rules.enabled_patterns, pattern] : draft.rules.enabled_patterns.filter(value => value !== pattern) } })} />{patternLabels[pattern]}</label>)}</div></fieldset>
      </fieldset>
      <fieldset disabled={saving} className={`${panelClass} space-y-4`}><h2 className="text-sm font-semibold">筛选与确认参数</h2>{groups.map((group, index) => <details key={group.title} open={index === 0} className="border-t border-border pt-3"><summary className="cursor-pointer text-sm font-medium focus-visible:outline focus-visible:outline-accent">{group.title}</summary><div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">{group.fields.map(field => <label key={field.key} className="space-y-1.5 text-xs text-secondary"><span>{field.label}</span><input className={inputClass} type="number" required min={field.min} max={field.max} step={field.step ?? 1} value={Number((draft.rules[field.key] * (field.scale ?? 1)).toFixed(6))} onChange={e => rule(field.key, e.target.valueAsNumber / (field.scale ?? 1))} /></label>)}</div></details>)}</fieldset>
      <fieldset disabled={saving} className={`${panelClass} space-y-4`}>
        <h2 className="text-sm font-semibold">模拟盘与风险约束</h2>
        <p className="text-xs leading-relaxed text-secondary">关联已有模拟账户后可手动提交模拟委托。仓位和退出阈值是实验约束，系统不会向券商下单；账户统计覆盖该账户全部交易。</p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <label className="space-y-1.5 text-xs text-secondary"><span>关联模拟账户</span><select className={inputClass} value={draft.paper_account_id ?? ''} onChange={e => onDraft({ ...draft, paper_account_id: e.target.value || null })}><option value="">仅观察，不关联账户</option>{accounts.map(account => <option key={account.id} value={account.id} disabled={account.status !== 'active'}>{account.name || account.id}{account.status === 'frozen' ? '（已冻结）' : ''}</option>)}{draft.paper_account_id && !accounts.some(account => account.id === draft.paper_account_id) && <option value={draft.paper_account_id}>原关联账户不可用</option>}</select></label>
          {configNumber('max_quote_age_seconds', '行情最长有效时间（秒）', 5, 120)}
          {configNumber('max_positions', '最多持仓股票数', 1, 10)}
          {configNumber('max_stock_weight', '单股仓位上限（%）', 0.1, 100, 100)}
          {configNumber('max_sector_weight', '同行业仓位上限（%）', 0.1, 100, 100)}
          {configNumber('total_exposure', '总仓位上限（%）', 0.1, 100, 100)}
          <label className="space-y-1.5 text-xs text-secondary"><span>次日退出检查时间（北京时间）</span><input type="time" required className={inputClass} value={draft.exit_time} onChange={e => onDraft({ ...draft, exit_time: e.target.value })} /></label>
          {configNumber('exit_loss_pct', '亏损退出提示阈值（%）', 0.1, 20, 100)}
        </div>
        <a href="/paper" className="inline-block text-xs text-accent underline underline-offset-2">管理模拟账户</a>
      </fieldset>
      {invalid && <Notice error>{invalid}</Notice>}
      <div className="flex flex-wrap items-center gap-3"><button type="submit" className={`${buttonClass} border-accent/50 text-foreground`} disabled={saving || !!invalid || !dirty || config.revision !== draft.revision}>{saving ? '正在保存…' : '保存为新版本'}</button><button type="button" className={buttonClass} disabled={saving || !dirty} onClick={() => onDraft(config)}>放弃草稿</button><span aria-live="polite" className="text-xs text-secondary">{dirty ? '有未保存的修改' : '已与当前版本同步'}</span></div>
    </form>
    <section className={panelClass}><h2 className="mb-3 text-sm font-semibold">历史版本</h2>{versions.length ? <ul className="divide-y divide-border">{versions.map(version => <li key={version.revision} className="flex flex-wrap items-center justify-between gap-3 py-3"><div className="text-xs"><span className="font-medium">版本 {version.revision}</span><span className="ml-3 text-secondary">{stamp(version.updated_at)} · {universeLabels[version.rules.universe]} · {version.rules.enabled_patterns.map(pattern => patternLabels[pattern]).join(' / ') || '未启用形态'} · {version.rules.lookback_days} 日无收盘涨停</span></div><button className={buttonClass} disabled={saving} onClick={() => onDraft({ ...draft, rules: version.rules })}>载入规则到草稿</button></li>)}</ul> : <p className="text-sm text-secondary">首次保存后，规则版本会显示在这里。</p>}</section>
  </div>
}
