import { Globe2 } from 'lucide-react'
import type { MarketGameOverseasContext } from '@/lib/api'
import { cn } from '@/lib/cn'

const up = 'text-red-700 dark:text-bull'
const down = 'text-emerald-700 dark:text-bear'
const warning = 'text-amber-700 dark:text-warning'
const factorLabels = { support: '外部支持', pressure: '外部压力', mixed: '方向分化', neutral: '波动温和', unknown: '待核验' }
const riskLabels = { caution: '谨慎观察', support: '外部支持', mixed: '方向分化', neutral: '波动温和', unknown: '待核验' }
const quoteLabels = { ready: '证据可用', stale: '行情过时', unavailable: '行情缺失', unverified: '行情未核验' }
const contextLabels = { ready: '输入可用', limited: '部分数据可用', unavailable: '暂不可用', historical_unverified: '历史可得性未核验' }
const phaseLabels = { intraday: '盘中快照', closed: '已收盘', unknown: '交易阶段未知' }
const currencyLabels: Record<string, string> = { USD: '美元', KRW: '韩元' }
const signedClass = (value: number | null) => value == null || !Number.isFinite(value) || value === 0 ? 'text-secondary' : value > 0 ? up : down
const factorClass = (state: string) => state === 'support' ? up : state === 'pressure' ? down : state === 'mixed' || state === 'caution' ? warning : 'text-secondary'
const percent = (value: number | null) => value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
const price = (value: number | null) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function time(value: string | null) {
  if (!value) return '未提供'
  // 海外时间必须携带时区，不能按访问者或服务器时区猜测。
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) return '时区未核验'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未核验' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

export function OverseasPanel({ data, mode }: { data?: MarketGameOverseasContext | null; mode: 'frozen' | 'observation' }) {
  const title = mode === 'frozen' ? '海外市场 · 计划冻结证据' : '海外市场 · 本次观察'
  return <section aria-label={title} className="min-w-0 rounded-card border border-border bg-surface/80 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-2 text-sm font-semibold"><Globe2 aria-hidden className="h-4 w-4 shrink-0 text-accent" />{title}</h2>
      {data && <span className={cn('text-xs font-medium', data.state === 'ready' ? 'text-secondary' : warning)}>{contextLabels[data.state]}</span>}
    </div>
    {!data ? <p className="mt-3 max-w-prose text-xs leading-relaxed text-secondary">{mode === 'frozen' ? '这份报告没有冻结的海外行情。生成新计划时记录纳斯达克、韩国 KS11、三星与 SK 海力士；历史报告不补入后来行情。' : '这次观察没有记录海外行情。下次刷新观察时按当时可用的数据记录，原计划保持冻结。'}</p> : <>
      <div className="mt-3 flex flex-wrap items-baseline gap-x-3 gap-y-1"><span className={cn('text-sm font-semibold', factorClass(data.risk_state))}>{riskLabels[data.risk_state]}</span><p className="max-w-prose text-xs leading-relaxed text-secondary">{data.summary}</p></div>
      <p className="mt-2 text-xs leading-relaxed text-secondary">截止 {time(data.cutoff)} · 取得 {time(data.fetched_at)}（北京时间）</p>
      <p className="mt-1 break-words text-xs leading-relaxed text-secondary">来源 {data.source || '未提供'} · {mode === 'frozen' ? '保留生成计划时的海外证据。' : '保留本次取得的快照，不回填原计划。'}</p>
      {data.quotes.length > 0 ? <div className="mt-4 grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
        {data.quotes.map(quote => <article key={quote.symbol} aria-label={quote.name} className="min-w-0 border-t border-border pt-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className="text-sm font-medium">{quote.name}</h3><span className={cn('font-mono text-base font-semibold', signedClass(quote.change_pct))}>{percent(quote.change_pct)}</span></div>
          <div className="mt-1 flex flex-wrap items-baseline justify-between gap-2 text-xs"><span className="font-mono text-secondary">{quote.symbol}</span><span className="font-mono">{price(quote.price)} <span className="font-sans text-secondary">{quote.symbol.startsWith('^') ? '点' : currencyLabels[quote.currency] ?? quote.currency}</span></span></div>
          <p className={cn('mt-2 text-xs', quote.state === 'ready' ? 'text-secondary' : warning)}>{quoteLabels[quote.state]} · {phaseLabels[quote.phase]}</p>
          <dl className="mt-2 space-y-1 text-xs leading-relaxed text-secondary">
            <div><dt className="inline">当地交易日 </dt><dd className="inline font-mono">{quote.session_date ?? '未提供'}</dd></div>
            <div><dt className="inline">行情时间 </dt><dd className="inline">{time(quote.observed_at)}</dd></div>
          </dl>
          <p className="mt-2 text-xs leading-relaxed text-secondary">{quote.reason}</p>
          <details className="mt-1 text-xs"><summary className="min-h-8 cursor-pointer py-2 text-secondary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">报价口径</summary><dl className="space-y-1 leading-relaxed text-secondary"><div><dt className="inline">前一交易日收盘 </dt><dd className="inline font-mono">{price(quote.previous_close)}</dd></div><div><dt className="inline">可得时间 </dt><dd className="inline">{time(quote.available_at)}</dd></div></dl></details>
        </article>)}
      </div> : <p className="mt-4 text-xs text-secondary">暂没有可核验的海外报价，缺失涨跌幅不记为零。</p>}
      {data.factors.length > 0 && <div className="mt-4 divide-y divide-border border-t border-border">{data.factors.map(factor => <div key={factor.id} className="grid gap-x-5 gap-y-1 py-3 text-xs sm:grid-cols-[minmax(150px,0.4fr)_1fr]"><div className="flex flex-wrap items-baseline gap-x-3 gap-y-1"><h3 className="font-medium">{factor.label}</h3><span className={cn('font-medium', factorClass(factor.state))}>{factorLabels[factor.state]}</span>{factor.change_pct != null && <span className={cn('font-mono', signedClass(factor.change_pct))}>{percent(factor.change_pct)}</span>}</div><p className="max-w-prose leading-relaxed text-secondary">{factor.explanation}</p></div>)}</div>}
      <div className="mt-2 border-t border-border pt-3 text-xs leading-relaxed"><h3 className="font-medium">与散户情绪联动</h3><p className="mt-1 max-w-prose text-secondary">{data.psychology_link}</p>{data.affected_sectors.length > 0 && <p className="mt-2 text-secondary">重点复核方向：{data.affected_sectors.join('、')}</p>}</div>
      <p className="mt-2 max-w-prose text-xs leading-relaxed text-secondary">海外因素用于补充情景与风险提示，仍需 A 股自身量价、板块扩散和原计划条件确认，不自动增加仓位。</p>
      {data.state === 'unavailable' && <a href="/settings?tab=data-sources" className="mt-2 inline-flex min-h-9 items-center text-xs text-accent underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">检查海外行情数据源</a>}
      {data.limitations.length > 0 && <details className="mt-2 text-xs"><summary className="min-h-8 cursor-pointer py-2 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">海外证据边界 · {data.limitations.length} 项</summary><ul className="max-w-prose space-y-1 leading-relaxed text-secondary">{data.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
    </>}
  </section>
}
