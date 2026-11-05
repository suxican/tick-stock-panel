import { lazy } from 'react'
import { DEFAULT_MODE_EVENTS, isModeMonitorType, modeEventLabel } from '@/lib/modeMonitorEvents'
import type { FrontendExtension } from '@/extensions/types'
import { IconMonitor } from '@/components/BrandIcons'

export const Monitor = lazy(() => import('./Page').then(m => ({ default: m.Monitor })))
  first_board: '首板模式', huichun: '回春模式',
  first_board: 'bg-accent/10 text-accent border-accent/20',
  huichun: 'bg-emerald-400/10 text-emerald-500 border-emerald-400/20',
    } else if (isModeMonitorType(kind ?? undefined)) {
      setEditingRule(null)
      setEditorPreset({ type: kind as 'first_board' | 'huichun', asset_type: 'stock', scope: 'all' })
      setEditorOpen(true)
      setSearchParams({}, { replace: true })
  const [filter, setFilter] = useState<'all' | 'strategy' | 'signal' | 'price' | 'market' | 'sector' | 'abnormal' | 'volume_delta' | 'date' | 'first_board' | 'huichun'>('all')
                {(['all', 'strategy', 'first_board', 'huichun', 'signal', 'price', 'market', 'sector', 'abnormal', 'volume_delta', 'date'] as const).map(f => (
                      {isModeMonitorType(ev.source) ? (
                        <div className="mt-1 space-y-1 text-[11px]">
                          <span className="font-medium text-accent">{modeEventLabel(ev.source, ev.type)}</span>
                          {ev.message && <p className="leading-relaxed text-secondary">{ev.message}</p>}
                        </div>
                      ) : ev.signals && ev.signals.length > 0 ? (
                </div>
              ) : isModeMonitorType(r.type) ? (
                <div className="mt-1 flex flex-wrap items-center gap-1 pl-0.5">
                  {(r.mode_events ?? DEFAULT_MODE_EVENTS[r.type]).map(event => (
                    <span key={event} className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
                      {modeEventLabel(r.type as 'first_board' | 'huichun', event)}
                    </span>
                  ))}
                  <span className="text-[9px] text-muted">{r.type === 'huichun' ? '日线扫描后确认' : '盘中自动盯盘事件'}</span>

const extension: FrontendExtension = {
  id: 'official.monitor',
  apiVersion: 1,
  routes: [
    { id: 'monitor', path: '/monitor', component: Monitor },
  ],
  navigation: [
    { id: 'monitor', routeId: 'monitor', label: '监控中心', icon: IconMonitor },
  ],
}

export default extension
