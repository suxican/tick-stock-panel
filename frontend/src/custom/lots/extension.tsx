import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconLots } from '@/components/BrandIcons'
import { PositionTMonitor } from './PositionTMonitor'

export const Lots = lazy(() => import('./Page').then(m => ({ default: m.Lots })))
  const [view, setView] = useState<'position-t' | 'batches'>('position-t')
  return <div className="flex h-full min-h-0 flex-col">
    <PageHeader title="持仓提醒" subtitle="按核对持仓监控日内价差机会，跟踪两笔交易与未完成敞口" />
    <div className="flex shrink-0 gap-2 border-b border-border px-5 py-3" aria-label="持仓提醒视图">
      {([['position-t', '持仓做 T'], ['batches', '批次提醒']] as const).map(([key, label]) => <button key={key} aria-current={view === key ? 'page' : undefined} onClick={() => setView(key)} className={cn('rounded-btn px-4 py-2 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent', view === key ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated')}>{label}</button>)}
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">{view === 'position-t' ? <PositionTMonitor /> : <BatchLots />}</div>
  </div>
}

function BatchLots() {

// ── 扩展注册清单 — lots 目录集中维护页面、路由和导航 ──
const extension: FrontendExtension = {
  id: 'official.lots',
  apiVersion: 1,
  routes: [
    { id: 'lots', path: '/lots', component: Lots },
  ],
  navigation: [
    { id: 'lots', routeId: 'lots', label: '持仓提醒', icon: IconLots },
  ],
}

export default extension
