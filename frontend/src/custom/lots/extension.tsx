import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconLots } from '@/components/BrandIcons'

export const Lots = lazy(() => import('./Page').then(m => ({ default: m.Lots })))

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
