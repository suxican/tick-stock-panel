import { lazy } from 'react'
import { ChartNoAxesCombined } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const EtfObservation = lazy(() => import('./EtfObservation'))

const extension: FrontendExtension = {
  id: 'etf-observation',
  apiVersion: 1,
  routes: [{ id: 'etf-observation', path: '/etf-observation', component: EtfObservation }],
  navigation: [{
    id: 'etf-observation', routeId: 'etf-observation', label: '宽基 ETF',
    icon: ChartNoAxesCombined, order: 115,
  }],
}

export default extension
