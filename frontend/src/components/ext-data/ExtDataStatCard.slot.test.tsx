// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ExtDataStatCard } from './ExtDataStatCard'
import { QK } from '@/lib/queryKeys'
import type { ExtDataConfig } from '@/lib/api'
import type { FrontendSlotContextMap } from '@/extensions/types'

const slot = vi.hoisted(() => ({ name: '', context: null as FrontendSlotContextMap['data.ext-card.actions'] | null }))
vi.mock('@/extensions/ExtensionSlot', () => ({
  ExtensionSlot: (props: { name: string; context: FrontendSlotContextMap['data.ext-card.actions'] }) => {
    slot.name = props.name
    slot.context = props.context
    return <output data-slot={props.name} />
  },
}))
vi.mock('./ExtConfigTransfer', () => ({ ExtConfigExportButton: () => null }))

const config: ExtDataConfig = {
  id: 'etf_broad_flow_daily', label: '宽基 ETF 日度份额与净申购估算', mode: 'timeseries',
  fields: [{ name: 'listed_units', label: '', dtype: 'int' }],
  description: '登记日口径', pull: null, created_at: '', updated_at: '',
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, value: () => [] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient()
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('gives card actions the table id and a refresh for the data page', async () => {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <ExtDataStatCard config={config} onDelete={vi.fn()} deleting={false} />
    </QueryClientProvider>,
  ))
  expect(host.querySelector('[data-slot="data.ext-card.actions"]')).not.toBeNull()
  expect(slot.context?.tableId).toBe('etf_broad_flow_daily')

  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await act(async () => slot.context!.refresh())
  expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.dataStatus })
  expect(invalidate.mock.calls.some(([filters]) => typeof filters?.predicate === 'function')).toBe(true)
})
