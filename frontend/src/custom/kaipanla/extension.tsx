import { lazy } from 'react'
import { Activity } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const MarketEmotion = lazy(() => import('./MarketEmotion').then(module => ({ default: module.MarketEmotion })))

const extension: FrontendExtension = {
  id: 'kaipanla.market-emotion',
  apiVersion: 1,
  routes: [{ id: 'kaipanla-market-emotion', path: '/market-emotion', component: MarketEmotion }],
  navigation: [{ id: 'kaipanla-market-emotion', routeId: 'kaipanla-market-emotion', label: '市场情绪', icon: Activity, order: 100 }],
}

export default extension
