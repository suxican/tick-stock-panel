import type { FrontendExtension } from '@/extensions/types'
import { KaipanlaSettings } from './KaipanlaSettings'

const extension: FrontendExtension = {
  id: 'official.kaipanla',
  apiVersion: 1,
  slots: [{ id: 'kaipanla-settings', name: 'settings.system.section', component: KaipanlaSettings }],
}

export default extension
