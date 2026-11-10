import { lazy } from 'react'
import { Newspaper } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const NewsAnalysis = lazy(() => import('./Page').then(module => ({ default: module.NewsAnalysis })))

const extension: FrontendExtension = {
  id: 'official.news',
  apiVersion: 1,
  routes: [{ id: 'news', path: '/news', component: NewsAnalysis }],
  navigation: [{ id: 'news', routeId: 'news', label: '资讯分析', icon: Newspaper, order: 113 }],
}

export default extension
