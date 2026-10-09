// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { KaipanlaMarketDataset, KaipanlaMarketEmotion, KaipanlaQueryResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { NavItem } from '@/lib/listNav'
import { MarketEmotion } from './MarketEmotion'
import extension from './extension'

const calls = vi.hoisted(() => ({ market: vi.fn(), stocks: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { kaipanlaMarketEmotion: calls.market, kaipanlaQuery: calls.stocks } }))
vi.mock('echarts-for-react', () => ({ default: ({ option }: { option: unknown }) => <div data-chart>{JSON.stringify(option)}</div> }))
vi.mock('./LiveIndexChart', () => ({ LiveIndexChart: ({ date, annotations, onAnnotationClick }: {
  date: string; annotations: { id: string; time: string; label: string; caption?: string; direction?: string }[]; onAnnotationClick: (id: string) => void
}) => <div data-live-chart={date}>{annotations.map(item => <button key={item.id} data-direction={item.direction} data-caption={item.caption} onClick={() => onAnnotationClick(item.id)}>定位 {item.time}{item.caption ? ` ${item.caption}` : ''}</button>)}</div> }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: ({ value, onChange }: {
  value: string; onChange: (value: string) => void
}) => <input aria-label="行情日期" value={value} onChange={event => onChange(event.target.value)} /> }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol, name, navList = [], onClose, onNavigate }: {
  symbol: string | null; name?: string; navList?: NavItem[]; onClose: () => void; onNavigate: (symbol: string, name?: string) => void
}) => symbol ? <div role="dialog" aria-label="个股详情" data-symbol={symbol} data-nav={JSON.stringify(navList)}>
  <span>{name}</span><button onClick={onClose}>关闭个股详情</button>
  <button onClick={() => { const item = navList[(navList.findIndex(item => item.symbol === symbol) + 1) % navList.length]; onNavigate(item.symbol, item.name) }}>下一只股票</button>
</div> : null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-30T07:30:00Z'))
  calls.market.mockReset()
  calls.stocks.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
})
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><MarketEmotion /></QueryClientProvider>))
  await settle()
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === text)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
async function selectDate(day: string) {
  const input = host.querySelector<HTMLInputElement>('input[aria-label="行情日期"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, day)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}
function dataset(id: string, date: string, rows: Record<string, unknown>[], columns: KaipanlaMarketDataset['columns']): KaipanlaMarketDataset {
  return { id: `ext_kpl_${id}`, label: id, state: rows.length ? 'ok' : 'empty', date,
    date_origin: 'response', fetched_at: `${date}T15:30:00+08:00`, rows, columns }
}
const CAPACITY_COLUMNS: KaipanlaMarketDataset['columns'] = [
  { name: 'last_wan', label: '最新量能(万)', unit: 'wan' },
  { name: 's_zrcs_wan', label: '昨日量能(万)', unit: 'wan' },
  { name: 's_zrtj_wan', label: '昨日统计量能(万)', unit: 'wan' },
  { name: 's3_zrtj_wan', label: '三日量能(万)', unit: 'wan' },
  { name: 'yclnstr', label: '预测成交额', unit: 'text' },
  { name: 'time', label: '来源时间', unit: 'text' },
]
function market(date = '2026-09-30'): KaipanlaMarketEmotion {
  return { source: '开盘啦', requested_date: null, date, previous_date: '2026-09-29',
    fetched_at: `${date}T15:30:00+08:00`, state: 'partial',
    datasets: {
      emotion: dataset('emotion', date, [{ strong: 61.5, lbgd: 5, df_num: null }], []),
      limit_counts: dataset('limit_counts', date, [{ SJZT: 40, SJDT: 8 }], []),
      limit_performance: dataset('limit_performance', date, [{ broken_rate_pct: 12.5, yesterday_limit_up_pct: 3.66 }], [
        { name: 'yesterday_limit_up_pct', label: '昨日涨停表现', unit: 'percent' },
      ]),
      capacity: dataset('capacity', date, [], [{ name: 'last_wan', label: '最新量能', unit: 'wan' }]),
    },
    history: dataset('emotion', date, [
      { trade_date: '2026-09-29', strong: 50, df_num: 4 },
      { trade_date: date, strong: 61.5, df_num: null },
    ], []),
    missing: [{ id: 'auction', label: '竞价列表', reason: '认证要求待确认，暂未启用。' },
      { id: 'limit_down', label: '跌停列表', reason: '文档未提供对应接口。' }],
  }
}

it('registers one lazy route and matching 市场情绪 navigation without changing the core router', () => {
  expect(extension.routes).toHaveLength(1)
  expect(extension.routes![0].path).toBe('/market-emotion')
  expect(extension.navigation![0].label).toBe('市场情绪')
  expect(extension.navigation![0].routeId).toBe(extension.routes![0].id)
})

it.each(['2026-10-08T16:05:00Z', '2026-10-09T07:25:52Z'])('opens on Beijing today despite a previous-session latest cache: %s', async now => {
  vi.setSystemTime(new Date(now))
  client.setQueryData(QK.kaipanlaMarketEmotion(), market('2026-10-08'))
  calls.market.mockResolvedValue(market('2026-10-09'))
  await render()
  expect(calls.market).toHaveBeenCalledWith('2026-10-09', false)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="行情日期"]')?.value).toBe('2026-10-09')
  expect(host.textContent).toContain('行情归属 2026-10-09')
  expect(host.textContent).not.toContain('2026-10-08')
  await click('刷新数据')
  expect(calls.market).toHaveBeenLastCalledWith('2026-10-09', true)
})

it('does not display previous-session metrics as today when the source returns an older date', async () => {
  vi.setSystemTime(new Date('2026-10-09T07:25:52Z'))
  calls.market.mockResolvedValue(market('2026-10-08'))
  await render()
  expect(host.querySelector<HTMLInputElement>('input[aria-label="行情日期"]')?.value).toBe('2026-10-09')
  expect(host.textContent).toContain('数据日期与所选日期不一致')
  expect(host.textContent).not.toContain('+3.66%')
})

it('keeps today selected on an empty holiday and lets the user request the latest supplier session', async () => {
  vi.setSystemTime(new Date('2026-10-02T07:30:00Z'))
  const empty = { ...market('2026-10-02'), state: 'empty' as const, datasets: {} }
  calls.market.mockImplementation((date?: string) => Promise.resolve(date ? empty : market()))
  await render()
  expect(calls.market).toHaveBeenCalledWith('2026-10-02', false)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="行情日期"]')?.value).toBe('2026-10-02')
  expect(host.textContent).toContain('暂无数据')
  expect(host.textContent).not.toContain('+3.66%')
  await click('最新交易日')
  expect(calls.market).toHaveBeenLastCalledWith(undefined, false)
  expect(host.querySelector<HTMLInputElement>('input[aria-label="行情日期"]')?.value).toBe('2026-09-30')
  expect(host.textContent).toContain('最近来源交易日快照')
})

it('shows sourced percentages without scaling, real historical values and explicit missing states', async () => {
  calls.market.mockResolvedValue(market())
  await render()
  expect(host.textContent).toContain('行情归属 2026-09-30')
  expect(host.textContent).toContain('2026-09-30 15:30:00')
  expect(host.textContent).toContain('+3.66%')
  expect(host.textContent).not.toContain('366.00%')
  expect(host.textContent).toContain('综合强度历史')
  expect(host.querySelector('[data-chart]')?.textContent).toContain('61.5')
  expect(host.textContent).toContain('暂无该日量能数据')
  expect(host.textContent).toContain('认证要求待确认')
  expect(calls.stocks).not.toHaveBeenCalled()
})

it('places static index cards first with supplier names, percent units and two-decimal prices', async () => {
  const value = market()
  value.datasets.indices = dataset('indices', value.date!, [
    { StockID: 'SH000001', prod_name: '上证指数', last_px: 3842.19, increase_rate_pct: 0.31 },
    { StockID: 'SZ399001', prod_name: '深证成指', last_px: 12887.6, increase_rate_pct: -0.11 },
    { StockID: 'SH000688', prod_name: '科创50', last_px: 1530.01, increase_rate_pct: 0 },
    { StockID: 'CUSTOM', prod_name: '未提供数值', last_px: null, increase_rate_pct: null },
  ], [])
  calls.market.mockResolvedValue(value)
  await render()
  const cards = host.querySelector('section[aria-label="核心指数快照"]')!
  expect(host.querySelector('[role="tabpanel"]')?.firstElementChild).toBe(cards)
  expect(cards.querySelectorAll('article')).toHaveLength(4)
  expect(cards.querySelector('a')).toBeNull()
  expect(cards.textContent).toContain('+0.31%')
  expect(cards.textContent).toContain('-0.11%')
  expect(cards.textContent).not.toContain('31.00%')
  expect(cards.textContent).toContain('12887.60')
  expect(cards.textContent).toContain('000001.SH')
  expect(cards.textContent).toContain('000688.SH')
  expect(cards.textContent).toContain('科创50')
  expect(cards.textContent).not.toContain('科创综指')
  const missing = cards.querySelector('article[aria-label="未提供数值"]')!
  expect(missing.textContent).toContain('CUSTOM')
  expect(missing.textContent?.match(/—/g)).toHaveLength(2)
  expect(missing.querySelector('svg')).toBeNull()
})

it('isolates delayed dates and retains the last valid same-date snapshot on refresh failure', async () => {
  let resolveNext!: (value: KaipanlaMarketEmotion) => void
  calls.market.mockImplementation((date?: string, force?: boolean) => {
    if (force) return Promise.reject(new Error('supplier internals'))
    return date === '2026-09-28' ? new Promise<KaipanlaMarketEmotion>(resolve => { resolveNext = resolve }) : Promise.resolve(market())
  })
  await render()
  await selectDate('2026-09-28')
  expect(host.textContent).not.toContain('61.5')
  expect(host.textContent).toContain('读取开盘啦市场数据')
  const value = market('2026-09-28')
  value.requested_date = value.date
  value.datasets.emotion.rows[0].strong = 28
  await act(async () => resolveNext(value))
  await settle()
  expect(host.textContent).toContain('行情归属 2026-09-28')
  await click('刷新数据')
  expect(host.textContent).toContain('刷新失败，保留上次有效快照')
  expect(host.textContent).toContain('28')
  expect(host.textContent).not.toContain('supplier internals')
})

it('loads stock tabs on demand, paginates locally and identifies first-board broken stocks', async () => {
  calls.market.mockResolvedValue(market())
  calls.stocks.mockImplementation((id: string, date: string) => Promise.resolve({
    id, source: '开盘啦', requested_date: date, data_date: date, date_origin: 'response',
    fetched_at: `${date}T15:30:00+08:00`, state: 'ok',
    rows: Array.from({ length: 26 }, (_value, index) => ({ StockID: String(600000 + index), Name: `样本股${index + 1}`, change_pct: '3.66' })),
  } satisfies KaipanlaQueryResult))
  await render()
  expect(calls.stocks).not.toHaveBeenCalled()
  await click('股票列表')
  expect(calls.stocks).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('样本股1')
  expect(host.textContent).not.toContain('样本股26')
  await click('下一页')
  expect(host.textContent).toContain('样本股26')
  expect(calls.stocks).toHaveBeenCalledTimes(1)
  await click('炸板')
  expect(calls.stocks).toHaveBeenLastCalledWith('ext_kpl_broken_limits', '2026-09-30', { PidType: 1 }, false)
  expect(host.textContent).toContain('今日首板破板')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '跌停')).toHaveProperty('disabled', true)
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '竞价')).toHaveProperty('disabled', true)
})

it('never polls a historical snapshot every minute', async () => {
  calls.market.mockResolvedValueOnce(market()).mockResolvedValue(market('2026-09-29'))
  await render()
  await selectDate('2026-09-29')
  vi.useRealTimers()
  vi.useFakeTimers()
  const count = calls.market.mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(180_000) })
  expect(calls.market).toHaveBeenCalledTimes(count)
  expect(client.getQueryData(QK.kaipanlaMarketEmotion('2026-09-29'))).toEqual(market('2026-09-29'))
})

it('polls only confirmed current-day data every minute and rejects an explicit date mismatch', async () => {
  vi.useRealTimers()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-02T02:00:00Z'))
  calls.market.mockResolvedValue(market('2026-10-02'))
  await act(async () => root.render(<QueryClientProvider client={client}><MarketEmotion /></QueryClientProvider>))
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(calls.market).toHaveBeenCalledTimes(2)
  vi.useRealTimers()
  calls.market.mockResolvedValue(market('2026-09-30'))
  await selectDate('2026-09-28')
  expect(host.textContent).toContain('数据日期与所选日期不一致')
  expect(host.textContent).not.toContain('+3.66%')
})

it('keeps discovering the current session in latest mode when the first response is the previous session', async () => {
  vi.useRealTimers()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-02T02:00:00Z'))
  calls.market.mockResolvedValue(market('2026-09-30'))
  await act(async () => root.render(<QueryClientProvider client={client}><MarketEmotion /></QueryClientProvider>))
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '最新交易日')!.click())
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(calls.market).toHaveBeenLastCalledWith(undefined, false)
  const count = calls.market.mock.calls.length
  calls.market.mockResolvedValue(market('2026-10-02'))
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(calls.market).toHaveBeenCalledTimes(count + 1)
  expect(host.textContent).toContain('行情归属 2026-10-02')
})

it('preserves successful same-date datasets when a refresh returns partial errors with HTTP success', async () => {
  const partial = market()
  partial.datasets.emotion = { ...partial.datasets.emotion, state: 'error', rows: [], fetched_at: '2026-09-30T16:00:00+08:00' }
  calls.market.mockResolvedValueOnce(market()).mockResolvedValue(partial)
  await render()
  await click('刷新数据')
  const stats = host.querySelector('[aria-label="核心统计"]')!
  expect(stats.textContent).toContain('61.5')
  const saved = client.getQueryData<KaipanlaMarketEmotion>(QK.kaipanlaMarketEmotion('2026-09-30'))!
  expect(saved.datasets.emotion.state).toBe('error')
  expect(saved.datasets.emotion.fetched_at).toBe('2026-09-30T15:30:00+08:00')
  expect(stats.textContent).toContain('更新失败 · 显示上次')
})

it('converts confirmed wan units to yi, preserves zero and bounds live rendering to dated messages', async () => {
  const value = market()
  value.datasets.capacity.columns[0].label = '最新量能(万)'
  value.datasets.capacity.rows = [{ last_wan: 143798942 }]
  value.datasets.capacity.state = 'ok'
  value.datasets.emotion.rows[0].df_num = 0
  value.datasets.live = dataset('live', value.date!, Array.from({ length: 8 }, (_value, index) => ({
    Time: Date.parse(`2026-09-30T07:${String(index).padStart(2, '0')}:00Z`) / 1000,
    Comment: `当日解读${index}`,
  })).concat([{ Time: Date.parse('2026-09-29T07:00:00Z') / 1000, Comment: '前日解读不得混入' }]), [
    { name: 'Time', label: '发布时间', unit: 'text' }, { name: 'Comment', label: '盘面解读', unit: 'text' },
  ])
  calls.market.mockResolvedValue(value)
  await render()
  expect(host.textContent).toContain('14379.89亿')
  expect(host.textContent).toContain('最新量能(亿)')
  expect(host.textContent).not.toContain('最新量能(万)')
  expect(host.textContent).toContain('Type=0')
  expect(host.textContent).toContain('市场覆盖未定义')
  expect(host.textContent).not.toContain('前日解读不得混入')
  const live = host.querySelector('section[aria-label="大盘直播"]')!
  expect(live.querySelectorAll('ol > li')).toHaveLength(5)
  expect(live.textContent).toContain('当日解读7')
  expect(live.textContent).not.toContain('当日解读0')
  await click('下一页消息')
  expect(live.textContent).toContain('当日解读0')
  const zero = [...host.querySelectorAll('dt')].find(item => item.textContent === '大幅回撤家数')!
  expect(zero.nextElementSibling?.textContent).toBe('0')
})

it.each([
  { latest: 143798942, yesterday: 140919753, label: '较昨日增量', difference: '+287.92亿', percentage: '+2.04%', color: 'text-red-700', barColor: 'bg-bull' },
  { latest: 234167807, yesterday: 241523238, label: '较昨日缩量', difference: '-735.54亿', percentage: '-3.05%', color: 'text-emerald-700', barColor: 'bg-bear' },
  { latest: 140919753, yesterday: 140919753, label: '较昨日持平', difference: '0.00亿', percentage: '0.00%', color: 'text-secondary', barColor: 'bg-secondary/60' },
])('compares latest and yesterday capacity on one scale: $label', async sample => {
  const value = market()
  value.datasets.capacity = dataset('capacity', value.date!, [{
    last_wan: sample.latest, s_zrcs_wan: sample.yesterday, s_zrtj_wan: 300000000, s3_zrtj_wan: 400000000,
    yclnstr: '预测原文：缩量（来源）', time: '2026-09-30T15:00:00+08:00', color: 'green',
  }], CAPACITY_COLUMNS)
  calls.market.mockResolvedValue(value)
  await render()
  const section = host.querySelector('section[aria-label="市场量能"]')!
  const change = section.querySelector('[aria-label="量能变化"]')!
  expect(change.textContent).toContain(sample.label)
  expect(change.textContent).toContain(sample.difference)
  expect(change.textContent).toContain(sample.percentage)
  expect(change.firstElementChild?.classList.contains(sample.color)).toBe(true)
  const bars = section.querySelectorAll<HTMLDivElement>('[aria-label="最新与昨日量能对比"] [aria-hidden="true"] > div')
  expect(bars).toHaveLength(2)
  expect(bars[0].classList.contains(sample.barColor)).toBe(true)
  expect(parseFloat(bars[0].style.width) / parseFloat(bars[1].style.width)).toBeCloseTo(sample.latest / sample.yesterday)
  expect(Math.max(parseFloat(bars[0].style.width), parseFloat(bars[1].style.width))).toBe(100)
  expect(section.textContent).toContain('30000.00亿')
  expect(section.textContent).toContain('40000.00亿')
  expect(section.textContent).toContain('预测原文：缩量（来源）')
  expect(section.textContent).toContain('2026-09-30 15:00:00')
})

it.each([0, 10000])('preserves zero yesterday capacity without dividing by zero: latest=$0', async latest => {
  const value = market()
  value.datasets.capacity = dataset('capacity', value.date!, [{ last_wan: latest, s_zrcs_wan: 0 }], CAPACITY_COLUMNS)
  calls.market.mockResolvedValue(value)
  await render()
  const section = host.querySelector('section[aria-label="市场量能"]')!
  const change = section.querySelector('[aria-label="量能变化"]')!
  expect(change.textContent).toContain(latest ? '较昨日增量+1.00亿' : '较昨日持平0.00亿')
  expect(change.textContent).not.toContain('%')
  expect(section.textContent).toContain('昨日量能为 0，变化比例不可计算。')
  expect(section.textContent).not.toMatch(/Infinity|NaN/)
})

it.each([null, -10000, 'invalid'])('does not replace unavailable yesterday capacity with statistical or forecast amounts: $0', async yesterday => {
  const value = market()
  value.datasets.capacity = dataset('capacity', value.date!, [{
    last_wan: 10000, s_zrcs_wan: yesterday, s_zrtj_wan: 10000, yclnstr: '100亿(+99%)',
  }], CAPACITY_COLUMNS)
  calls.market.mockResolvedValue(value)
  await render()
  const section = host.querySelector('section[aria-label="市场量能"]')!
  expect(section.querySelector('[aria-label="量能变化"]')?.textContent).toBe('暂无法比较')
  expect(section.querySelector('[aria-label="最新与昨日量能对比"]')?.textContent).toContain('昨日量能(亿)—')
  expect(section.textContent).toContain('100亿(+99%)')
  expect(section.textContent).not.toContain('较昨日持平')
})

it('requires confirmed matching capacity units and retains failed-refresh status with saved amounts', async () => {
  const value = market()
  value.datasets.capacity = { ...dataset('capacity', value.date!, [{ last_wan: 10000, s_zrcs_wan: 10000 }],
    CAPACITY_COLUMNS.map(column => column.name === 's_zrcs_wan' ? { ...column, unit: 'yuan' as const } : column)),
    state: 'error', message: '当前获取失败，保留上次有效快照。' }
  calls.market.mockResolvedValue(value)
  await render()
  const section = host.querySelector('section[aria-label="市场量能"]')!
  expect(section.querySelector('[aria-label="量能变化"]')?.textContent).toBe('暂无法比较')
  expect(section.textContent).toContain('1.00亿')
  expect(section.textContent).toContain('获取失败')
  expect(section.textContent).toContain('当前获取失败，保留上次有效快照。')
})

it('maps five ladder buckets and three independent promotion rates, preserving zero and missing values', async () => {
  const value = market()
  value.datasets.limit_ladder_counts = dataset('limit_ladder_counts', value.date!, [{
    first_board_count: 40, second_board_count: 6, third_board_count: 4, fourth_board_count: 0, fifth_plus_count: null,
  }], [])
  value.datasets.limit_performance.rows = [{ two_board_promotion_pct: 12.766, three_board_promotion_pct: 0.36,
    max_board_promotion_pct: 66.6667, broken_rate_pct: 0 }]
  calls.market.mockResolvedValue(value)
  await render()
  const ladder = host.querySelector('section[aria-label="连板梯队"]')!
  expect([...ladder.querySelectorAll('dt')].map(item => item.textContent)).toEqual(['首板', '二板', '三板', '四板', '五板及以上'])
  expect([...ladder.querySelectorAll('dd')].map(item => item.textContent)).toEqual(['40', '6', '4', '0', '—'])
  const promotion = host.querySelector('section[aria-label="晋级与破板"]')!
  expect([...promotion.querySelectorAll('dt')].map(item => item.textContent)).toEqual(['二板晋级率', '三板晋级率', '最高板晋级率', '破板率'])
  expect([...promotion.querySelectorAll('dd')].map(item => item.textContent)).toEqual(['12.77%', '0.36%', '66.67%', '0.00%'])
  expect(promotion.textContent).toContain('未提供四板独立晋级率')
  expect(promotion.textContent).not.toContain('封板率')
  expect(promotion.textContent).not.toContain('36.00%')
})

it('keeps ladder failure snapshots independent and hides promotion data from a different date', async () => {
  const value = market()
  value.datasets.limit_ladder_counts = { ...dataset('limit_ladder_counts', value.date!, [{ first_board_count: 40 }], []),
    state: 'error', message: '当前获取失败，保留上次有效快照。' }
  value.datasets.limit_performance = dataset('limit_performance', '2026-09-29', [{ two_board_promotion_pct: 99.99 }], [])
  calls.market.mockResolvedValue(value)
  await render()
  const ladder = host.querySelector('section[aria-label="连板梯队"]')!
  expect(ladder.textContent).toContain('40')
  expect(ladder.textContent).toContain('获取失败')
  expect(ladder.textContent).toContain('2026-09-30 15:30:00')
  expect(ladder.textContent).toContain('保留上次有效快照')
  expect(host.textContent).toContain('暂无该日晋级与破板数据。')
  expect(host.textContent).not.toContain('99.99%')
})

it('keeps stock price precision, matches monetary labels and explains weight list groups', async () => {
  const value = market()
  value.datasets.withdrawal = dataset('withdrawal', value.date!, [{ Name: '样本股', price: 34.56, turnover: 123_000_000 }], [
    { name: 'Name', label: '名称', unit: 'text' }, { name: 'price', label: '价格', unit: 'yuan' },
    { name: 'turnover', label: '成交额(元)', unit: 'yuan' },
  ])
  value.datasets.weight = dataset('weight', value.date!, [{ direction: 'SZ' }, { direction: 'XD' }], [
    { name: 'direction', label: '交易方向', unit: 'text' }, { name: 'color', label: '展示颜色编号', unit: 'number' },
  ])
  value.datasets.previous_limit_performance = dataset('previous_limit_performance', value.previous_date!, [{ broken_rate_pct: 12.307692 }], [])
  value.datasets.limit_performance.rows[0].broken_rate_pct = 18.75
  calls.market.mockResolvedValue(value)
  await render()
  const withdrawal = host.querySelector('table[aria-label="大幅回撤名单"]')!
  expect(withdrawal.textContent).toContain('34.56元')
  expect(withdrawal.textContent).not.toContain('成交额(元)')
  expect(withdrawal.textContent).toContain('1.23亿元')
  const weight = host.querySelector('table[aria-label="权重表现"]')!
  expect(weight.textContent).toContain('榜单')
  expect(weight.textContent).toContain('上涨前三')
  expect(weight.textContent).toContain('下跌前三')
  expect(weight.textContent).not.toContain('交易方向')
  expect(weight.textContent).not.toContain('展示颜色编号')
  const broken = [...host.querySelectorAll('dt')].find(item => item.textContent === '破板率')!
  expect(broken.parentElement?.textContent).toContain('18.75%')
  expect(broken.parentElement?.textContent).toContain('前日 12.31%')
})

it('does not report zero live messages when the live source failed', async () => {
  const value = market()
  value.datasets.live = { ...dataset('live', value.date!, [], []), state: 'error' }
  calls.market.mockResolvedValue(value)
  await render()
  expect(host.textContent).toContain('直播消息暂不可用')
  expect(host.textContent).not.toContain('共 0 条同日有效消息')
})

it('shows source-linked stocks under each live message with sourced percentages and missing values', async () => {
  const value = market()
  value.datasets.live = dataset('live', value.date!, [{
    ID: 'live-1', Time: Date.parse('2026-09-30T07:00:00Z') / 1000, Comment: '风电股拉升', UserName: '来源作者',
    stocks: [
      { symbol: '002487', name: '大金重工', change_pct: 7.83, kind: 'focus' },
      { symbol: '300443', name: '金雷股份', change_pct: -0.82, kind: 'discussion' },
      { symbol: '600000', name: '浦发银行', change_pct: 0, kind: 'discussion' },
      { symbol: '600001', name: '无涨幅股票', change_pct: null, kind: 'discussion' },
    ],
  }], [])
  calls.market.mockResolvedValue(value)
  await render()
  const live = host.querySelector('section[aria-label="大盘直播"]')!
  expect(live.querySelector('time')?.textContent).toBe('15:00')
  expect(live.textContent).toContain('来源作者')
  expect(live.textContent).toContain('关联个股涨跌幅由开盘啦返回，更新时间未提供')
  const stockRows = [...live.querySelectorAll('ul > li')]
  const label = (text: string) => stockRows.find(row => row.getAttribute('aria-label') === text)
  expect(label('大金重工 +7.83%')?.querySelector('button')?.lastElementChild?.classList.contains('dark:text-bull')).toBe(true)
  expect(label('金雷股份 -0.82%')?.querySelector('button')?.lastElementChild?.classList.contains('dark:text-bear')).toBe(true)
  expect(label('浦发银行 0.00%')).toBeDefined()
  expect(label('无涨幅股票 —')).toBeDefined()
  expect(live.textContent).not.toContain('发布时涨跌幅')
  expect(live.querySelector('[data-live-chart]')?.getAttribute('data-live-chart')).toBe(value.date)
  expect(live.querySelector('table')).toBeNull()
})

it('keeps old live rows readable and never invents stock matches from comments', async () => {
  const value = market()
  value.datasets.live = dataset('live', value.date!, [
    { Time: Date.parse('2026-09-30T06:25:00Z'), Comment: '大金重工、天顺风能上涨', stocks: null },
    { Time: Date.parse('2026-09-30T07:00:00Z') / 1000, Comment: '较晚的秒时间戳消息', stocks: [] },
    { Time: 'not-a-time', Comment: '无效时间不展示', stocks: [] },
  ], [])
  calls.market.mockResolvedValue(value)
  await render()
  const live = host.querySelector('section[aria-label="大盘直播"]')!
  expect([...live.querySelectorAll('time')].map(time => time.textContent)).toEqual(['15:00', '14:25'])
  expect(live.textContent).toContain('来源未提供关联个股')
  expect(live.querySelector('[aria-label="关联个股"]')).toBeNull()
  expect(live.textContent).not.toContain('无效时间不展示')
})

it('locates the message page and linked stocks when a chart annotation is selected', async () => {
  const value = market()
  value.datasets.live = dataset('live', value.date!, Array.from({ length: 8 }, (_, index) => ({
    ID: `event-${index}`, Time: Date.parse(`2026-09-30T06:${String(index).padStart(2, '0')}:00Z`) / 1000,
    Comment: `盘面消息 ${index}`, stocks: [{ symbol: '002487', name: `关联股票 ${index}`, change_pct: index, kind: 'focus' }],
  })), [])
  calls.market.mockResolvedValue(value)
  await render()
  await click('定位 14:00')
  const selected = host.querySelector('li[aria-current="true"]')
  expect(selected?.querySelector('time')?.textContent).toBe('14:00')
  expect(selected?.textContent).toContain('盘面消息 0')
  expect(selected?.textContent).toContain('关联股票 0')
  expect(host.textContent).toContain('2/2 页')
})

it('maps separate strength and weakness annotations back to the same original live message', async () => {
  const value = market()
  value.datasets.live = dataset('live', value.date!, [{
    ID: 'mixed', Time: Date.parse('2026-09-30T06:00:00Z') / 1000,
    Comment: '医药板块持续走强，芯片板块震荡走弱。', stocks: [],
  }, {
    ID: 'unknown', Time: Date.parse('2026-09-30T05:00:00Z') / 1000,
    Comment: '上涨个股超2500家', stocks: [],
  }], [])
  calls.market.mockResolvedValue(value)
  await render()
  const chart = host.querySelector('[data-live-chart]')!
  expect(chart.querySelector('[data-direction="up"]')?.getAttribute('data-caption')).toBe('医药板块持续走强')
  expect(chart.querySelector('[data-direction="down"]')?.getAttribute('data-caption')).toBe('芯片板块震荡走弱')
  expect(chart.querySelector('[data-direction="neutral"]')?.hasAttribute('data-caption')).toBe(false)
  await click('定位 14:00 芯片板块震荡走弱')
  expect(host.querySelector('li[aria-current="true"]')?.textContent).toContain('医药板块持续走强，芯片板块震荡走弱。')
})

it('opens withdrawal stocks and weight leaders in the shared preview with local ordered navigation', async () => {
  const value = market()
  value.datasets.withdrawal = dataset('withdrawal', value.date!, [
    { StockID: '603396', Name: '金辰股份' }, { StockID: 'SZ300746', Name: '汉嘉数智' },
    { StockID: '603396.SH', Name: '重复股票' }, { StockID: 'invalid', Name: '无效代码' },
  ], [{ name: 'StockID', label: '代码', unit: 'text' }, { name: 'Name', label: '名称', unit: 'text' }])
  value.datasets.weight = dataset('weight', value.date!, [{ plate_id: '801045', plate_name: '医药', leader_id: '603127', leader_name: '昭衍新药' }],
    ['plate_id', 'plate_name', 'leader_id', 'leader_name'].map(name => ({ name, label: name, unit: 'text' })))
  calls.market.mockResolvedValue(value)
  await render()
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  await click('金辰股份')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('603396.SH')
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav')!)).toEqual([
    { symbol: '603396.SH', name: '金辰股份' }, { symbol: '300746.SZ', name: '汉嘉数智' },
  ])
  await click('下一只股票')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('300746.SZ')
  await click('关闭个股详情')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  await click('603127')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('603127.SH')
  expect([...host.querySelectorAll('table button')].some(button => ['医药', '801045', '无效代码', 'invalid'].includes(button.textContent!))).toBe(false)
  await selectDate('2026-09-29')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
})

it('opens live stock cards with normalized symbols and only that message stocks in navigation', async () => {
  const value = market()
  calls.stocks.mockResolvedValue({ data_date: value.date, state: 'empty', rows: [] })
  value.datasets.live = dataset('live', value.date!, [{
    Time: Date.parse('2026-09-30T07:00:00Z') / 1000, Comment: '直播原文', stocks: [
      { symbol: '000710', name: '贝瑞基因', change_pct: 10.02 },
      { symbol: 'SH688185', name: '康希诺', change_pct: 20 },
      { symbol: '920001.BJ', name: '北交样本', change_pct: null },
      { symbol: 'SH000001', name: '指数不可当个股', change_pct: 0.31 },
    ],
  }, { Time: Date.parse('2026-09-30T06:00:00Z') / 1000, Comment: '其他消息', stocks: [{ symbol: '600825', name: '新华传媒' }] }], [])
  calls.market.mockResolvedValue(value)
  await render()
  const card = host.querySelector<HTMLButtonElement>('button[aria-label="查看贝瑞基因个股详情"]')
  expect(card).not.toBeNull()
  await act(async () => card!.click())
  await settle()
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('000710.SZ')
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav')!).map((item: NavItem) => item.symbol)).toEqual(['000710.SZ', '688185.SH', '920001.BJ'])
  expect(host.querySelector('button[aria-label="查看指数不可当个股个股详情"]')).toBeNull()
  await click('股票列表')
  expect(host.querySelector('[role="dialog"]')).toBeNull()
})

it('uses the full stock list for preview navigation across table pages and replaces it for broken stocks', async () => {
  calls.market.mockResolvedValue(market())
  calls.stocks.mockImplementation((id: string, date: string) => Promise.resolve({
    id, data_date: date, state: 'ok', rows: id === 'ext_kpl_limit_up'
      ? Array.from({ length: 26 }, (_, index) => ({ StockID: String(600000 + index), Name: `导航样本${index}` }))
      : [{ StockID: '002799', Name: '环球印务' }],
  }))
  await render()
  await click('股票列表')
  await click('导航样本24')
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav')!)).toHaveLength(26)
  await click('下一只股票')
  expect(host.querySelector('[role="dialog"]')?.getAttribute('data-symbol')).toBe('600025.SH')
  await click('关闭个股详情')
  await click('炸板')
  await click('环球印务')
  expect(JSON.parse(host.querySelector('[role="dialog"]')!.getAttribute('data-nav')!)).toEqual([{ symbol: '002799.SZ', name: '环球印务' }])
})
