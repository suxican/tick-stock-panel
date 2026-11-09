// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ResearchPanel, type ResearchTab } from './ResearchPanel'

const research = vi.hoisted(() => vi.fn())
vi.mock('@/lib/api', () => ({ api: { zzshareResearch: research } }))

const day = '2026-11-06'
let root: Root
let host: HTMLDivElement
let client: QueryClient
const open = vi.fn()
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  research.mockReset(); open.mockReset()
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) }) }
async function render(tab: ResearchTab, date = day) {
  await act(async () => root.render(<QueryClientProvider client={client}><ResearchPanel key={tab + date} tab={tab} day={date} open={open} live={false} /></QueryClientProvider>))
  await settle()
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  expect(button, label).toBeDefined()
  await act(async () => button!.click()); await settle()
}
function result(kind: string, rows: Record<string, string | number | null>[]) {
  return { kind, date: day, source: 'zzshare', fetched_at: day + 'T15:20:00+08:00', stale: false, rows }
}

it('热榜合并名称代码，次要字段按需展开，股票保持所选交易日', async () => {
  research.mockResolvedValue(result('ths_hot_top', [{ rank: 1, stock_name: '样例股票', stock_code: '600000', symbol: '600000.SH',
    rank_change: 2, change_pct: 3.45, price_yuan: 12.34, updated_at: day + 'T15:00:00+08:00' }]))
  await render('heat')
  expect([...host.querySelectorAll('thead th')].map(t => t.textContent)).not.toContain('代码')
  expect(host.querySelector('tbody td:nth-child(2)')?.textContent).toContain('600000')
  expect(host.textContent).not.toContain('12.34')
  await click('详情')
  expect(host.textContent).toContain('12.34')
  expect(host.textContent).toContain('源更新时间')
  await click('样例股票')
  expect(open).toHaveBeenCalledWith('600000.SH', '样例股票', day)
})

it('情绪图按时间选择最新有效点位，差值使用模型点位，明细默认折叠', async () => {
  research.mockResolvedValue(result('market_sentiment', [
    { trade_date: day, close: 120, previous_close: 110 },
    { trade_date: '2026-11-05', close: 110, previous_close: 100 },
  ]))
  await render('sentiment')
  expect(host.textContent).toContain('较前收 +10 点')
  const raw = [...host.querySelectorAll('details')].find(d => d.querySelector('summary')?.textContent?.includes('原始明细'))!
  expect(raw.open).toBe(false)
  const chart = host.querySelector<SVGElement>('[role=slider]')!
  expect(chart.getAttribute('aria-valuetext')).toContain(day + '，120 点')
  await act(async () => chart.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })))
  expect(chart.getAttribute('aria-valuetext')).toContain('2026-11-05，110 点')
  await act(async () => chart.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })))
  vi.spyOn(chart, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 800 } as DOMRect)
  const touch = new MouseEvent('pointerdown', { clientX: 0, bubbles: true })
  Object.defineProperty(touch, 'pointerType', { value: 'touch' })
  await act(async () => chart.dispatchEvent(touch))
  const leave = new MouseEvent('pointerout', { bubbles: true })
  Object.defineProperty(leave, 'pointerType', { value: 'touch' })
  await act(async () => chart.dispatchEvent(leave))
  expect(chart.getAttribute('aria-valuetext')).toContain('2026-11-05，110 点')
  expect(host.textContent).not.toContain('冰点')
})

it('日内单点及缺失前值不显示虚构变化', async () => {
  research.mockImplementation(async kind => result(kind, kind === 'sentiment_trend' ? [{ time: '09:31:00', value: 0, aux_value: null }] : []))
  await render('sentiment'); await click('日内情绪走势')
  expect(host.textContent).toContain('较上一观测 —')
  expect(host.querySelector('[role=slider]')?.getAttribute('aria-valuetext')).toContain('0 点')
})

it('当前目录入口按需查询并保留非历史目录说明', async () => {
  research.mockImplementation(async kind => result(kind, []))
  await render('themes')
  expect(research).toHaveBeenCalledTimes(1)
  expect([...host.querySelectorAll('button')].map(b => b.textContent)).not.toContain('供应商板块榜')
  await click('当前板块目录'); await click('行业')
  expect(research).toHaveBeenLastCalledWith('plates_list', day, undefined, 14)
  expect(host.textContent).toContain('切换交易日不会还原历史目录')
  await click('返回热榜')
  expect(research).toHaveBeenLastCalledWith('plates_rank', day, undefined, 14)
})

it('龙虎榜展开完整原因不额外查询，席位仍按需查询', async () => {
  research.mockImplementation(async kind => result(kind, kind === 'lhb_list' ? [{ stock_code: '600000', stock_name: '上榜样本',
    reason: '连续三个交易日内收盘价格涨幅偏离值累计达到规定幅度', amount_yuan: 150000000 }] : [{ seat_name: '样例席位', buy_yuan: 30000 }]))
  await render('lhb')
  await click('详情')
  expect(host.textContent).toContain('1.50亿元')
  expect(research).toHaveBeenCalledTimes(1)
  await click('席位')
  expect(research).toHaveBeenLastCalledWith('lhb_detail', day, '600000')
  expect(host.textContent).toContain('样例席位')
})

it('错误可重试，错日不画旧榜，缓存延迟不藏在详情', async () => {
  research.mockRejectedValueOnce(new Error('来源不可用')).mockResolvedValueOnce({ ...result('ths_hot_top', [{ stock_name: '旧榜' }]), date: '2026-11-05' })
    .mockResolvedValue({ ...result('ths_hot_top', [{ stock_name: '正确榜单' }]), stale: true })
  await render('heat')
  await click('重试')
  expect(host.textContent).toContain('数据与所选日期不一致')
  expect(host.textContent).not.toContain('旧榜')
  await click('重试')
  const warning = [...host.querySelectorAll('strong')].find(s => s.textContent?.includes('使用缓存'))!
  expect(warning.closest('details')).toBeNull()
  expect(host.textContent).toContain('正确榜单')
})
