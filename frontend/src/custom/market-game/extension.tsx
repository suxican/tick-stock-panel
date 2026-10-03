import { lazy } from 'react'
import { Scale } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const MarketGame = lazy(() => import('./MarketGame').then(module => ({ default: module.MarketGame })))

const extension: FrontendExtension = {
  id: 'market-game.workbench',
  apiVersion: 1,
  routes: [{ id: 'market-game', path: '/market-game', component: MarketGame }],
  navigation: [{ id: 'market-game', routeId: 'market-game', label: '超短线博弈', icon: Scale, order: 110 }],
}

export default extension
