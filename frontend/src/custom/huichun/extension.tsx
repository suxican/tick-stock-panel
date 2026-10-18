import { lazy } from 'react'
import { Sprout } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const Huichun = lazy(() => import('./Huichun').then(module => ({ default: module.Huichun })))

const extension: FrontendExtension = {
  id: 'huichun.workbench',
  apiVersion: 1,
  routes: [{ id: 'huichun', path: '/huichun', component: Huichun }],
  navigation: [{ id: 'huichun', routeId: 'huichun', label: '回春模式', icon: Sprout, order: 120 }],
}

export default extension
