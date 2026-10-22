// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { MarketGameOverseasContext } from '@/lib/api'
import { OverseasPanel } from './OverseasPanel'

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })
async function render(data?: MarketGameOverseasContext | null, mode: 'frozen' | 'observation' = 'frozen') {
  await act(async () => root.render(<OverseasPanel data={data} mode={mode} />))
}
function context(): MarketGameOverseasContext {
  return {
    version: '1.0.0', source: 'QA 海外数据', fetched_at: '2026-10-05T10:10:00+08:00', cutoff: '2026-10-05T10:10:00+08:00', state: 'limited',
    quotes: [
      { symbol: '^IXIC', name: '纳斯达克综合指数', market: 'US', currency: 'USD', state: 'ready', price: 21000, previous_close: 20750, change_pct: .012, session_date: '2026-10-02', observed_at: '2026-10-02T16:00:00-04:00', available_at: '2026-10-05T02:10:00Z', phase: 'closed', source_url: null, reason: '上一已收盘交易时段' },
      { symbol: '^KS11', name: '韩国 KS11', market: 'KR', currency: 'KRW', state: 'ready', price: 2900, previous_close: 2930, change_pct: -.01, session_date: '2026-10-05', observed_at: '2026-10-05T11:05:00+09:00', available_at: null, phase: 'intraday', source_url: null, reason: '盘中行情可能延迟' },
      { symbol: '005930.KS', name: '三星电子', market: 'KR', currency: 'KRW', state: 'ready', price: 70000, previous_close: 70000, change_pct: 0, session_date: '2026-10-05', observed_at: '2026-10-05T11:05:00+09:00', available_at: null, phase: 'intraday', source_url: null, reason: '未发生价格变化' },
      { symbol: '000660.KS', name: 'SK 海力士', market: 'KR', currency: 'KRW', state: 'unavailable', price: null, previous_close: null, change_pct: null, session_date: null, observed_at: null, available_at: null, phase: 'unknown', source_url: null, reason: '未取得行情' },
    ],
    factors: [
      { id: 'us_tech', label: '美国科技风险偏好', state: 'support', change_pct: .012, symbols: ['^IXIC'], explanation: '科技方向有外部支持' },
      { id: 'korea_market', label: '韩国大盘', state: 'pressure', change_pct: -.01, symbols: ['^KS11'], explanation: '区域风险偏好偏弱' },
      { id: 'memory_chain', label: '存储产业链', state: 'unknown', change_pct: null, symbols: ['005930.KS', '000660.KS'], explanation: '样本缺失，不强行推断共同方向' },
    ],
    summary: '海外方向分化，A 股承接仍需确认。', psychology_link: '散户追涨偏热时，不能把隔夜上涨直接作为追加仓位依据。',
    risk_state: 'mixed', affected_sectors: ['半导体', '存储芯片'], limitations: ['报价可能延迟；海外涨跌不能识别机构身份。'],
  }
}

it('keeps old frozen reports and unrecorded observations honest', async () => {
  await render()
  expect(host.textContent).toContain('历史报告不补入后来行情')
  expect(host.querySelectorAll('article')).toHaveLength(0)
  expect(host.textContent).not.toContain('0.00%')
  await render(null, 'observation')
  expect(host.textContent).toContain('这次观察没有记录海外行情')
  expect(host.textContent).toContain('原计划保持冻结')
})

it('displays decimal changes once with A-share direction colors and separates zero from missing', async () => {
  await render(context())
  const nasdaq = host.querySelector('article[aria-label="纳斯达克综合指数"]')!
  const korea = host.querySelector('article[aria-label="韩国 KS11"]')!
  const samsung = host.querySelector('article[aria-label="三星电子"]')!
  const hynix = host.querySelector('article[aria-label="SK 海力士"]')!
  expect(nasdaq.textContent).toContain('+1.20%')
  expect(nasdaq.textContent).not.toContain('120.00%')
  expect(nasdaq.querySelector('.dark\\:text-bull')?.textContent).toBe('+1.20%')
  expect(korea.querySelector('.dark\\:text-bear')?.textContent).toBe('-1.00%')
  expect(samsung.textContent).toContain('0.00%')
  expect(samsung.textContent).toContain('70,000.00 韩元')
  expect(hynix.textContent).toContain('行情缺失')
  expect(hynix.textContent).toContain('—')
  expect(hynix.textContent).not.toContain('0.00%')
})

it('converts overseas timestamps to Beijing while preserving local session dates', async () => {
  await render(context())
  const nasdaq = host.querySelector('article[aria-label="纳斯达克综合指数"]')!
  const korea = host.querySelector('article[aria-label="韩国 KS11"]')!
  expect(nasdaq.textContent).toContain('当地交易日 2026-10-02')
  expect(nasdaq.textContent).toContain('2026/10/03 04:00')
  expect(nasdaq.textContent).toContain('2026/10/05 10:10')
  expect(korea.textContent).toContain('2026/10/05 10:05')
  expect(host.textContent).toContain('北京时间')
  expect(host.textContent).toContain('来源 QA 海外数据')
})

it('preserves factor uncertainty, psychology linkage and original-plan boundaries', async () => {
  const data = context()
  data.state = 'historical_unverified'
  await render(data)
  expect(host.textContent).toContain('历史可得性未核验')
  expect(host.textContent).toContain('外部支持')
  expect(host.textContent).toContain('外部压力')
  expect(host.textContent).toContain('待核验')
  expect(host.textContent).toContain(data.psychology_link)
  expect(host.textContent).toContain('重点复核方向：半导体、存储芯片')
  expect(host.textContent).toContain('不自动增加仓位')
  expect(host.textContent).toContain(data.limitations[0])
})

it('shows provider failures and fails closed for unzoned timestamps', async () => {
  const data = context()
  data.state = 'unavailable'
  data.fetched_at = null
  data.cutoff = '2026-10-05T10:10:00'
  data.quotes = []
  data.factors = []
  data.risk_state = 'unknown'
  await render(data)
  expect(host.textContent).toContain('暂不可用')
  expect(host.textContent).toContain('时区未核验')
  expect(host.textContent).toContain('缺失涨跌幅不记为零')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/settings?tab=data-sources')
})

it('replaces frozen evidence with the selected observation without retaining old quotes', async () => {
  await render(context())
  const next = context()
  next.quotes = [{ ...next.quotes[1], change_pct: .0234, state: 'stale', reason: '这份快照已过时' }]
  next.state = 'limited'
  await render(next, 'observation')
  expect(host.textContent).toContain('海外市场 · 本次观察')
  expect(host.textContent).toContain('+2.34%')
  expect(host.textContent).toContain('行情过时')
  expect(host.textContent).not.toContain('纳斯达克综合指数')
  expect(host.textContent).toContain('不回填原计划')
})
