// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardOpportunities, FirstBoardOpportunityRow } from '@/lib/api'
import { OpportunityList } from './OpportunityList'

const calls = vi.hoisted(() => ({ firstBoardOpportunities: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: calls }))

const at = (minute: number, second = 0) => `2026-10-16T10:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}+08:00`
function row(symbol: string, values: Partial<FirstBoardOpportunityRow> = {}): FirstBoardOpportunityRow {
  return { symbol, name: `名${symbol}`, patterns: [{ pattern: 'platform', pattern_label: '平台突破' }],
    first_alert: { at: at(1, 18), price: 10.2, pattern: 'platform', event_id: `e-${symbol}` }, first_touch: { at: at(9, 36), price: 11 },
    broken: at(20), resealed: at(30, 4), last_state: { event_type: 'sealed', at: at(40) },
    first_record: { at: at(1, 18), event_type: 'buy_candidate', blocked_reasons: [] },
    alerted: true, sealed_unalerted: false, event_count: 12, ...values }
}
const unalerted = (symbol: string) => row(symbol, { first_alert: null, alerted: false, sealed_unalerted: true, broken: null, resealed: null,
  first_record: { at: at(2, 5), event_type: 'approaching', blocked_reasons: ['行业强度不足'] }, event_count: 3 })
function report(rows: FirstBoardOpportunityRow[]): FirstBoardOpportunities {
  return { day: '2026-10-16', revision: 4, available_revisions: [4], rows, summary: {
    stock_count: rows.length, alerted_count: rows.filter(item => item.alerted).length, touched_count: rows.filter(item => item.first_touch).length,
    sealed_unalerted_count: rows.filter(item => item.sealed_unalerted).length, skipped_count: 0 } }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
const onOpen = vi.fn()
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.firstBoardOpportunities.mockReset()
  onOpen.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 6; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><OpportunityList day="2026-10-16" poll={false} onOpen={onOpen} /></QueryClientProvider>))
  await settle()
}
const rows = () => [...host.querySelectorAll('table[aria-label="首板机会记录"] tbody tr[data-opportunity-row]')]
const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim().startsWith(text) || item.getAttribute('aria-label')?.startsWith(text))!
async function click(text: string) { await act(async () => button(text).click()); await settle() }

it('requests the day and shows summary counts with separate denominators', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([row('A'), row('B'), unalerted('C')]))
  await render()
  expect(calls.firstBoardOpportunities).toHaveBeenCalledWith('2026-10-16', false)
  const text = host.textContent!
  expect(text).toContain('机会记录')
  for (const [label, count] of [['记录股票', '3'], ['独立买入提示', '2'], ['有触板记录', '3'], ['封板未提示', '1']]) {
    const cell = [...host.querySelectorAll('[data-opportunity-summary] > div')].find(item => item.textContent?.includes(label))!
    expect(cell.textContent, label).toContain(count)
  }
  expect(rows()).toHaveLength(3)
})

it('shows first alert and touch times and the last observed state, not a closing result', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([row('A')]))
  await render()
  const cells = rows()[0].textContent!
  expect(cells).toContain('名A')
  expect(cells).toContain('10:01:18')
  expect(cells).toContain('10:09:36')
  expect(cells).toContain('涨停价观察')
  expect(host.textContent).toContain('最后观察')
  expect(host.textContent).not.toContain('收盘封板')
})

it('filters to sealed-but-unalerted stocks and back', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([row('A'), unalerted('C')]))
  await render()
  expect(button('封板未提示').textContent).toContain('1')
  await click('封板未提示')
  expect(rows().map(item => item.textContent)).toEqual([expect.stringContaining('名C')])
  await click('全部')
  expect(rows()).toHaveLength(2)
})

it('expands a timeline with dashes for missing steps and the first record for unalerted stocks', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([row('A'), unalerted('C')]))
  await render()
  await click('展开 名A')
  const timeline = host.querySelector('[data-opportunity-timeline]')!.textContent!
  expect(timeline).toContain('板前提示10:01:18')
  expect(timeline).toContain('首次触板10:09:36')
  expect(timeline).toContain('开板10:20:00')
  expect(timeline).toContain('回封10:30:04')
  expect(timeline).toContain('原始事件 12 条')
  await click('收起 名A')
  await click('展开 名C')
  const unalertedTimeline = host.querySelector('[data-opportunity-timeline]')!.textContent!
  expect(unalertedTimeline).toContain('板前提示—')
  expect(unalertedTimeline).toContain('开板—')
  expect(unalertedTimeline).toContain('最早记录 10:02:05')
  expect(unalertedTimeline).toContain('临近涨停')
  expect(unalertedTimeline).toContain('行业强度不足')
})

it('opens the stock preview from the name', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([row('A')]))
  await render()
  await click('名A')
  expect(onOpen).toHaveBeenCalledWith('A', '名A')
})

it('shows an empty state and a retryable error without crashing on missing fields', async () => {
  calls.firstBoardOpportunities.mockResolvedValue(report([]))
  await render()
  expect(host.textContent).toContain('该日暂无机会记录')
  client.clear()
  calls.firstBoardOpportunities.mockReset()
  calls.firstBoardOpportunities.mockRejectedValueOnce(new Error('网络错误')).mockResolvedValueOnce(report([row('A', { first_touch: null, last_state: { event_type: 'mystery', at: 'bad' } })]))
  await render()
  expect(host.textContent).toContain('网络错误')
  await click('重试')
  expect(rows()).toHaveLength(1)
  expect(rows()[0].textContent).toContain('状态变化')
})

function verifiedReport() {
  const rows = [
    { ...row('A'), close: { status: 'sealed' as const, close_price: 11, limit_price: 11, reason: null }, paper: { status: 'closed' as const, reason: null, net_return: 0.0312 } },
    { ...row('B'), close: { status: 'not_sealed' as const, close_price: 10.6, limit_price: 11, reason: null }, paper: { status: 'ambiguous' as const, reason: '同一股票存在多张买单', net_return: null } },
    { ...unalerted('C'), close: { status: 'unverified' as const, close_price: null, limit_price: null, reason: '缺少当日日线' }, paper: { status: 'no_signal' as const, reason: null, net_return: null } },
  ]
  const base = report(rows)
  return { ...base, verified: true, rows, summary: { ...base.summary, close_sealed_count: 1, close_checked_count: 2, paper_filled_count: 1, paper_checked_count: 1 } }
}

it('does not show the verification columns until the button is pressed, then requests verify=true', async () => {
  calls.firstBoardOpportunities.mockImplementation(async (_day: string, verify: boolean) => verify ? verifiedReport() : report([row('A'), row('B'), unalerted('C')]))
  await render()
  expect(host.querySelector('table')!.textContent).not.toContain('收盘结果')
  expect(calls.firstBoardOpportunities).not.toHaveBeenCalledWith('2026-10-16', true)
  await click('核验收盘与成交')
  expect(calls.firstBoardOpportunities).toHaveBeenCalledWith('2026-10-16', true)
  const head = host.querySelector('table')!.textContent!
  expect(head).toContain('收盘结果')
  expect(head).toContain('模拟成交')
  const body = rows().map(item => item.textContent!)
  expect(body[0]).toContain('收盘封板')
  expect(body[0]).toContain('已平仓')
  expect(body[0]).toContain('3.12%')
  expect(body[1]).toContain('收盘未封')
  expect(body[1]).toContain('来源不唯一')
  expect(body[2]).toContain('待核验')
})

it('shows verified summary counts with their denominators', async () => {
  calls.firstBoardOpportunities.mockImplementation(async (_day: string, verify: boolean) => verify ? verifiedReport() : report([row('A'), row('B'), unalerted('C')]))
  await render()
  await click('核验收盘与成交')
  const text = host.querySelector('[data-opportunity-summary]')!.textContent!
  expect(text).toContain('收盘封板1只')
  expect(text).toContain('已核日线 2 / 3')
  expect(text).toContain('已核验模拟成交1只')
  expect(text).toContain('已核提示股 1 / 2')
})

it('keeps the unverified table and offers a retry when the verification request fails', async () => {
  calls.firstBoardOpportunities.mockImplementation(async (_day: string, verify: boolean) => {
    if (verify) throw new Error('核验失败')
    return report([row('A')])
  })
  await render()
  await click('核验收盘与成交')
  expect(host.textContent).toContain('核验失败')
  expect(rows()).toHaveLength(1)
  expect(host.querySelector('table')!.textContent).not.toContain('收盘结果')
  calls.firstBoardOpportunities.mockImplementation(async (_day: string, verify: boolean) => verify ? { ...verifiedReport(), rows: [verifiedReport().rows[0]] } : report([row('A')]))
  await click('重试')
  expect(host.querySelector('table')!.textContent).toContain('收盘结果')
})
