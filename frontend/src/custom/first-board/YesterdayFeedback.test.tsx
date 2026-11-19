// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FirstBoardFeedback, FirstBoardFeedbackSummary } from '@/lib/api'
import { YesterdayFeedback } from './YesterdayFeedback'

const summary: FirstBoardFeedbackSummary = {
  sample_count: 40, quote_count: 40, coverage: 1, mean_change_pct: .0156, median_change_pct: .0083,
  up_count: 21, up_ratio: .525, limit_up_count: 8, limit_known_count: 40, limit_up_ratio: .2,
  loss_count: 6, loss_ratio: .15, mean_open_premium: .0169, mean_open_change: -.0007,
  open_count: 40, small_sample: false,
}
function feedback(): FirstBoardFeedback {
  return {
    status: 'ready', message: '行情覆盖完整', as_of: '2026-10-27', sample_date: '2026-10-26',
    observed_at: '2026-10-27T10:00:00+08:00', quote_mode: 'live', membership_note: '当前成分等权涨幅，非官方板块指数。', history_excluded_count: 0,
    groups: { all: summary, main: { ...summary, sample_count: 33, quote_count: 33, median_change_pct: -.0117 }, growth: { ...summary, sample_count: 7, quote_count: 7, small_sample: true } },
    rows: [
      { symbol: '600000.SH', name: '主板样本', board: 'main', price: 9.4, change_pct: -.06, open_premium: .01, open_change: -.0693, at_limit: false, quote_time: '2026-10-27T10:00:00+08:00', status: 'valid', reason: null },
      { symbol: '300001.SZ', name: '创业板样本', board: 'growth', price: 12, change_pct: .2, open_premium: .1, open_change: .0909, at_limit: true, quote_time: '2026-10-27T10:00:00+08:00', status: 'valid', reason: null },
    ],
    sectors: [{ id: 'industry:food', name: '休闲食品', kind: 'industry', sample_count: 3, quote_count: 3, mean_change_pct: .0627, limit_up_count: 2, limit_known_count: 3, member_count: 21, member_quote_count: 21, coverage: 1, sector_mean_change_pct: -.0079, up_ratio: .1429, status: 'ready', reason: null }],
  }
}
let host: HTMLDivElement
let root: Root
const openStock = vi.fn()
beforeEach(() => {
  localStorage.removeItem('first-board-feedback-expanded')
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host); openStock.mockReset()
})
afterEach(async () => { await act(async () => root.unmount()); host.remove(); localStorage.removeItem('first-board-feedback-expanded') })
async function render(data?: FirstBoardFeedback, failed = false) {
  await act(async () => root.render(<YesterdayFeedback data={data} refreshFailed={failed} onOpenStock={openStock}><p>原盯盘状态</p></YesterdayFeedback>))
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === label)!
  expect(button).toBeDefined()
  await act(async () => button.click())
}
function metric(label: string) {
  return [...host.querySelectorAll('dt')].find(item => item.textContent === label)?.nextElementSibling
}

it('collapses to key indicators by default and remembers the expanded distribution', async () => {
  await render({ ...feedback(), opening_distribution: { sample_count: 12, open_count: 10, history_excluded_count: 0, up_count: 6, up_ratio: .6, mean_open_premium: .01, bins: [] } })
  const toggle = [...host.querySelectorAll('button')].find(item => item.textContent === '展开分布')!
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(host.textContent).not.toContain('原盯盘状态')
  expect(metric('中位数')).toBeUndefined()
  expect(metric('平均溢价')?.textContent).toBe('+1.56%')
  expect(metric('平均溢价')?.classList.contains('text-bull')).toBe(true)
  expect(metric('当前二板')?.textContent).toBe('8 / 40')
  expect(metric('非一字涨停开盘上涨占比')?.textContent).toBe('60.00%')
  expect(host.textContent).toContain('样本 2026-10-26')
  await click('主板')
  await click('展开分布')
  expect(localStorage.getItem('first-board-feedback-expanded')).toBe('true')
  expect(host.textContent).toContain('原盯盘状态')
  expect(metric('中位数')?.textContent).toBe('-1.17%')
  await act(async () => root.unmount())
  root = createRoot(host)
  await render(feedback())
  expect([...host.querySelectorAll('button')].find(item => item.textContent === '收起分布')?.getAttribute('aria-expanded')).toBe('true')
  expect(metric('中位数')).toBeDefined()
})

it('renders signed decimal returns, fixed denominators and independent market context', async () => {
  await render(feedback())
  await click('展开分布')
  expect(host.textContent).toContain('原盯盘状态')
  expect(host.textContent).toContain('样本 2026-10-26')
  expect(host.textContent).toContain('行情 40 / 40')
  expect(metric('平均溢价')?.textContent).toBe('+1.56%')
  expect(metric('中位数')?.textContent).toBe('+0.83%')
  expect(metric('上涨占比')?.textContent).toContain('52.5%')
  expect(metric('当前二板')?.textContent).toContain('8 / 40')
  expect(metric('开盘后涨幅')?.textContent).toBe('-0.07%')
  expect(metric('跌幅 ≥ 5%')?.textContent).toContain('6 / 40')
})

it('switches overall and member scope but explicitly keeps sector comparison in the full universe', async () => {
  await render(feedback())
  await click('展开分布')
  await click('查看明细')
  await click('主板')
  expect(metric('中位数')?.textContent).toBe('-1.17%')
  const members = host.querySelector('table[aria-label="昨日首板个股表现"]')!
  expect(members.textContent).toContain('主板样本')
  expect(members.textContent).not.toContain('创业板样本')
  expect(host.textContent).toContain('行业／概念对照固定为沪深非 ST 全范围')
  const sectors = host.querySelector('table[aria-label="昨日首板行业与概念对照"]')!
  expect(sectors.textContent).toContain('+6.27%')
  expect(sectors.textContent).toContain('-0.79%')
  await click('双创')
  expect(host.textContent).toContain('样本少于 10 只')
  await click('创业板样本300001.SZ')
  expect(openStock).toHaveBeenCalledWith('300001.SZ', '创业板样本', [{ symbol: '300001.SZ', name: '创业板样本' }])
})

it('preserves selected scope and open details when quotes refresh', async () => {
  const data = feedback()
  await render(data)
  await click('展开分布')
  await click('查看明细')
  await click('主板')
  await render({ ...data, groups: { ...data.groups, main: { ...data.groups.main, median_change_pct: -.02 } } })
  expect(metric('中位数')?.textContent).toBe('-2.00%')
  expect(host.querySelector('table[aria-label="昨日首板个股表现"]')).not.toBeNull()
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '主板')?.getAttribute('aria-pressed')).toBe('true')
})

it('keeps missing values distinct from zero and does not invent a complete promotion rate', async () => {
  const data = feedback()
  await render({ ...data, status: 'partial', groups: { ...data.groups, all: { ...summary, quote_count: 30, coverage: .75, mean_change_pct: null, up_ratio: null, limit_known_count: 30, limit_up_ratio: null } } })
  expect(metric('平均溢价')?.textContent).toBe('—')
  expect(metric('上涨占比')?.textContent).toContain('—')
  expect(metric('当前二板')?.textContent).toContain('8 / 40')
  expect(metric('当前二板')?.textContent).not.toContain('20.0%')
  expect(host.textContent).toContain('涨停状态待确认 10 只')
})

it('retains values with a refresh failure message and neutralizes stale price emphasis', async () => {
  await render(feedback(), true)
  expect(metric('平均溢价')?.textContent).toBe('+1.56%')
  expect(metric('平均溢价')?.classList.contains('text-bull')).toBe(false)
  expect(host.textContent).toContain('刷新失败，保留上次快照')
  await render({ ...feedback(), status: 'stale' })
  expect(metric('平均溢价')?.classList.contains('text-bull')).toBe(false)
})

it('labels snapshots and hides expanded explanations while preserving member details', async () => {
  await render({ ...feedback(), quote_mode: 'snapshot', history_excluded_count: 2 })
  await click('展开分布')
  expect(host.textContent).toContain('最近行情')
  expect(host.textContent).not.toContain('收盘已确认')
  await click('查看明细')
  expect(host.textContent).not.toContain('固定样本：')
  expect(host.textContent).not.toContain('平均开盘溢价')
  expect(host.textContent).not.toContain('2 只股票缺少首板识别所需的历史或涨停依据，未纳入确认样本')
  expect(host.textContent).not.toContain('行情时点以个股报价时间为准')
  expect(host.querySelector('table[aria-label="昨日首板个股表现"]')?.textContent).toContain('主板样本')
})

it('shows the quote explanation only when its timestamp icon is hovered or focused', async () => {
  const message = '最近行情快照；未另行确认最终收盘结果。'
  await render({ ...feedback(), status: 'partial', quote_mode: 'snapshot', message })
  await click('展开分布')
  expect(host.textContent).not.toContain(message)
  const icon = host.querySelector<HTMLButtonElement>('button[aria-label="行情说明"]')!
  expect(icon).not.toBeNull()
  expect(icon.parentElement?.parentElement?.textContent).toContain('统计更新 10/27 10:00:00')
  await act(async () => icon.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })))
  expect(host.querySelector('[role="tooltip"]')?.textContent).toBe(`部分数据待确认 · ${message}`)
  expect(icon.getAttribute('aria-describedby')).toBe(host.querySelector('[role="tooltip"]')?.id)
  await act(async () => icon.dispatchEvent(new MouseEvent('mouseout', { bubbles: true })))
  expect(host.querySelector('[role="tooltip"]')).toBeNull()
  await act(async () => icon.focus())
  expect(host.querySelector('[role="tooltip"]')?.textContent).toContain(message)
  await act(async () => icon.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(host.querySelector('[role="tooltip"]')).toBeNull()
  await act(async () => icon.blur())
  await act(async () => icon.focus())
  await render({ ...feedback(), message: '行情覆盖完整' }, true)
  expect(host.querySelector('[role="tooltip"]')?.textContent).toBe('保留快照 · 行情覆盖完整')
  await act(async () => icon.blur())
  expect(host.querySelector('[role="tooltip"]')).toBeNull()
})

it('explains old API, waiting, unavailable and empty samples without fake performance', async () => {
  await render()
  expect(host.textContent).toContain('昨日首板反馈暂未生成')
  await render({ ...feedback(), status: 'waiting', message: '正在建立昨日首板名单' })
  expect(host.querySelector('[aria-label="正在加载昨日首板反馈"]')).not.toBeNull()
  await render({ ...feedback(), status: 'unavailable', message: '历史数据不足，待日线更新后重试' })
  expect(host.textContent).toContain('历史数据不足')
  expect(metric('平均溢价')).toBeUndefined()
  const empty = { ...summary, sample_count: 0, quote_count: 0 }
  await render({ ...feedback(), groups: { all: empty, main: empty, growth: empty }, rows: [], sectors: [] })
  expect(host.textContent).toContain('该范围没有可确认的昨日首板')
  expect(metric('平均溢价')).toBeUndefined()
})

it('distinguishes unknown sector limit states from confirmed zero promotions', async () => {
  const data = feedback()
  await render({ ...data, sectors: [{ ...data.sectors[0], limit_up_count: 0, limit_known_count: 0 }] })
  await click('展开分布')
  await click('查看明细')
  const cell = host.querySelector('table[aria-label="昨日首板行业与概念对照"] tbody tr')!.children[3]
  expect(cell.textContent).toBe('—待确认 3')
})

function names() {
  return [...host.querySelectorAll('table[aria-label="昨日首板个股表现"] tbody tr')].map(row => row.querySelector('button')?.firstChild?.textContent)
}

it('shows auction columns and sorts by auction metrics only when auction data exists', async () => {
  const data = feedback()
  data.auction = { source: 'meoz', fetched_at: '2026-10-27T09:25:31+08:00' }
  data.rows[0].auction = { pct_chg: .05, amount: 3e7, vol_ratio: 8.5, limit_bid_amount: null }
  data.rows[1].auction = { pct_chg: .02, amount: 1e8, vol_ratio: 2.1, limit_bid_amount: 5e7 }
  await render(data)
  await click('展开分布')
  await click('查看明细')
  const table = host.querySelector('table[aria-label="昨日首板个股表现"]')!
  expect(table.textContent).toContain('竞价涨幅')
  expect(table.textContent).toContain('+5.00%')
  expect(names()).toEqual(['创业板样本', '主板样本'])  // 默认按当前涨幅
  await click('竞价涨幅')
  expect(names()).toEqual(['主板样本', '创业板样本'])
  await click('竞价金额')
  expect(names()).toEqual(['创业板样本', '主板样本'])
  await click('竞价量比')
  expect(names()).toEqual(['主板样本', '创业板样本'])
})

it('hides auction columns and sort control without auction data', async () => {
  await render(feedback())
  await click('展开分布')
  await click('查看明细')
  expect(host.querySelector('table[aria-label="昨日首板个股表现"]')!.textContent).not.toContain('竞价涨幅')
  expect(host.querySelector('[aria-label="个股排序"]')).toBeNull()
})


function withSeal(): FirstBoardFeedback {
  const base = feedback()
  const layer = (n: number, mean: number | null) => ({ ...summary, sample_count: n, quote_count: n, mean_change_pct: mean, up_ratio: n ? .5 : null, limit_up_count: n ? 1 : 0 })
  return {
    ...base,
    rows: [
      { ...base.rows[0], seal: { first_time: '13:45:00', opened: true, fd_amount: 12000000 } },
      { ...base.rows[1], seal: { first_time: '09:31:00', opened: false, fd_amount: 85000000 } },
    ],
    layers: {
      all: { early: layer(18, .031), late: layer(22, .002), unopened: layer(20, .028), reopened: layer(20, -.004) },
      main: { early: layer(15, .02), late: layer(18, 0), unopened: layer(16, .01), reopened: layer(17, -.01) },
      growth: { early: layer(3, .09), late: layer(4, -.02), unopened: layer(4, .05), reopened: layer(3, .01) },
    },
    seal_source: { date: '2026-10-26', matched: 40, sample: 40 },
  }
}

it('shows yesterday seal facts per stock and a seal-quality layer table in the details', async () => {
  await render(withSeal())
  await click('展开分布')
  await click('查看明细')
  const table = host.querySelector('[aria-label="按昨日封板质量分层"]')!
  expect(table).not.toBeNull()
  const rows = [...table.querySelectorAll('tbody tr')].map(r => r.textContent)
  expect(rows[0]).toContain('早封')
  expect(rows[0]).toContain('18')
  expect(rows[0]).toContain('3.10%')
  expect(rows[3]).toContain('开过板')
  const stocks = host.querySelector('[aria-label="昨日首板个股表现"]')!
  expect(stocks.textContent).toContain('09:31')
  expect(stocks.textContent).toContain('开过')
  expect(stocks.textContent).toContain('8500万')
})

it('sorts stocks by yesterday first-seal time, earliest first', async () => {
  await render(withSeal())
  await click('展开分布')
  await click('查看明细')
  await click('昨日首封')
  const names = [...host.querySelectorAll('[aria-label="昨日首板个股表现"] tbody tr')].map(r => r.querySelector('button')?.textContent)
  expect(names[0]).toContain('创业板样本')
})

it('layer table follows the market scope', async () => {
  await render(withSeal())
  await click('展开分布')
  await click('查看明细')
  await click('双创')
  const first = host.querySelector('[aria-label="按昨日封板质量分层"] tbody tr')!
  expect(first.textContent).toContain('3')
  expect(first.textContent).toContain('9.00%')
})

it('hides seal columns and layers when the archive is unavailable', async () => {
  await render({ ...feedback(), layers: null, seal_source: null })
  await click('展开分布')
  await click('查看明细')
  expect(host.querySelector('[aria-label="按昨日封板质量分层"]')).toBeNull()
  expect(host.querySelector('[aria-label="昨日首板个股表现"]')!.textContent).not.toContain('昨日首封')
})

it('shows a dash for unknown reopen status and blanked seal amount', async () => {
  const data = withSeal()
  data.rows[0] = { ...data.rows[0], seal: { first_time: '10:20:00', opened: null, fd_amount: null } }
  await render(data)
  await click('展开分布')
  await click('查看明细')
  const row = [...host.querySelectorAll('[aria-label="昨日首板个股表现"] tbody tr')].find(r => r.textContent?.includes('主板样本'))!
  expect(row.textContent).toContain('10:20')
  expect(row.textContent).not.toContain('0.00亿')
})

it('layer promotions read as count over sample and stay unknown until every limit state is confirmed', async () => {
  const data = withSeal()
  data.layers!.all.late = { ...data.layers!.all.late, limit_up_count: 0, limit_known_count: 5 }
  await render(data)
  await click('展开分布')
  await click('查看明细')
  const rows = [...host.querySelectorAll('[aria-label="按昨日封板质量分层"] tbody tr')]
  expect(rows[0].lastElementChild?.textContent).toBe('1 / 18')
  expect(rows[1].lastElementChild?.textContent).toBe('—')
  expect(rows[1].lastElementChild?.getAttribute('title')).toContain('待确认')
})
