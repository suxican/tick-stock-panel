import { lazy } from 'react'
import { Radar } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const FirstBoard = lazy(() => import('./FirstBoard').then(module => ({ default: module.FirstBoard })))

const extension: FrontendExtension = {
  id: 'first-board.workbench',
  apiVersion: 1,
  routes: [{ id: 'first-board', path: '/first-board', component: FirstBoard }],
  navigation: [{ id: 'first-board', routeId: 'first-board', label: '首板模式', icon: Radar, order: 110 }],
}

export default extension
