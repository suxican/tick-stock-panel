import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { ZzshareLimitReasonsPanel } from '@/components/ext-data/ZzshareLimitReasonsPanel'
import { getFrontendExtensionNavigation } from '@/extensions/registry'

const buttonStyle = 'rounded-btn bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/20 disabled:opacity-50'

export function ZzshareActions({ available }: { available: boolean }) {
  const daily = useMutation({
    mutationFn: async () => {
      const result = await api.testDataSource('zzshare', 'daily', ['600000.SH'])
      if (result.error) throw new Error(result.error)
      return result
    },
  })
  const sample = daily.data?.preview[0]
  const radar = getFrontendExtensionNavigation().find(item => item.route.path === '/zzshare-radar')

  return (
    <div className="space-y-4 text-xs">
      {radar && <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/50 pb-3">
        <span className="text-secondary">强势题材、市场情绪、个股热榜和盘后龙虎榜</span>
        <Link className={buttonStyle} to={radar.route.path}>打开题材雷达</Link>
      </div>}
      <section className="space-y-2">
        <h4 className="font-medium text-foreground">股票日 K</h4>
        <p className="text-secondary">试拉浦发银行（600000.SH）的行情。需要用于同步时，可在上方「能力路由」的日 K 项选择 zzshare。该源仅覆盖股票，ETF 和指数日 K 不受支持。</p>
        <button type="button" className={buttonStyle} disabled={!available || daily.isPending} onClick={() => daily.mutate()}>
          {daily.isPending ? '试拉中…' : '试拉股票日 K'}
        </button>
        {daily.data && <p role="status" className="text-secondary">
          {daily.data.rows > 0 ? `返回 ${daily.data.rows} 行，样本日期 ${sample?.date ?? '—'}，收盘价 ${sample?.close ?? '—'} 元。` : '未返回日 K，请稍后重试。'}
        </p>}
        {daily.isError && <p role="alert" className="text-danger">{daily.error.message}</p>}
      </section>
      <div className="border-t border-border/50 pt-4">
        <ZzshareLimitReasonsPanel available={available} />
      </div>
    </div>
  )
}
