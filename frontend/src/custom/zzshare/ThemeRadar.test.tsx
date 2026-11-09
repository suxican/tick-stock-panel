// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ThemeRadar } from './ThemeRadar'
import extension from './extension'
import { QK } from '@/lib/queryKeys'

const calls = vi.hoisted(() => ({ home: vi.fn(), research: vi.fn(), themes: vi.fn(), sync: vi.fn(), status: vi.fn(), members: vi.fn(), intraday: vi.fn(), market: vi.fn() }))
vi.mock('@/lib/api', async actual => ({
  ...await actual<typeof import('@/lib/api')>(),
  api: { zzshareHome: calls.home, zzshareResearch: calls.research, zzshareThemes: calls.themes, syncZzshareThemes: calls.sync, quoteStatus: calls.status,
    zzshareThemeMembers: calls.members, zzshareThemeIntraday: calls.intraday, marketSnapshot: calls.market },
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, tradeDate, navList }: { symbol?: string; tradeDate?: string; navList?: {symbol: string}[] }) =>
    symbol ? <output data-stock data-nav={navList?.map(s => s.symbol).join(',')}>{symbol} · {tradeDate}</output> : null,
}))
// jsdom 没有滚动视口，只替换行测量；过滤、排序、渲染与开股操作使用真实弹窗。
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count, getItemKey }: { count: number; getItemKey: (index: number) => string }) => ({
    measureElement: () => {}, getTotalSize: () => count * 54,
    getVirtualItems: () => Array.from({ length: count }, (_, index) => ({ index, start: index * 54, key: getItemKey(index) })),
  }),
}))
const draw = vi.hoisted(() => ({ chart: vi.fn(), points: vi.fn() }))
vi.mock('lightweight-charts', () => ({
  LineStyle: { Dashed: 2 }, LineSeries: {}, createChart: draw.chart,
}))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: ({ onChange }: { onChange: (value: string) => void }) => <button onClick={() => onChange('2025-02-05')}>选取历史日</button>,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const observed = '2025-02-05'
const leader = { symbol: '600000.SH', name: '样例龙头', change_pct: 5.5, amount_yuan: 1e8 }
function model(date: string, ready = false) {
  return { date, state: ready ? 'ready' : 'idle', scope: '指定日期热度候选', message: '', coverage: { candidates: 10, completed: 10, eligible: 1 }, progress: { done: 10, total: 10 }, computed_at: null, quote_asof: date,
    rows: ready ? [{ plate_code: 'A1', plate_name: '候选内排名第一', score: 73.2, member_count: 10, valid_count: 8,
      coverage_pct: 80, avg_change_pct: 3.56, rising_pct: 80, strong_pct: 50, limit_up_count: 1, limit_down_count: 0, leader, anchor: null }] : [] }
}
function official(date: string, cutoff?: string) {
  const points = ['10:05:14', '14:55:37'].map(time => date + 'T' + time + '+08:00')
  const observedAt = cutoff ? points[0] : points[1]
  return { date, state: 'ready', mode: cutoff ? 'intraday' : 'close', as_of: cutoff ?? null,
    fetched_at: observedAt, source_at: observedAt, market_snapshot_at: cutoff ? '10:05' : '14:55',
    message: '', stale: false, previous_date: '2025-02-04', candidate_count: 15, strategy_candidate_count: 6,
    points, selection_rule: 'shared_strategy_top2_plus_all_top1', rows: [
      { tid: '95', rank: 1, name: cutoff ? '历史时点主题' : '官方第一席(251008)', score: 94.5, scope: 'strategy',
        rank_change: 1, new_entry: false, streak: null, streak_min: 2, member_count: 11, valid_count: 10,
        core_stocks: [{ symbol: '000001.SZ', name: '官网核心样本', role: '龙头', change_pct: 7.32, price_yuan: null, core_score: 91 }] },
      { tid: '18', rank: 2, name: '官方第二席', score: null, scope: 'strategy', new_entry: true, streak: 1, core_stocks: [] },
      { tid: '105', rank: 3, name: '全池高分第三席', score: 100, scope: 'all', new_entry: null, streak: null, streak_min: 1, core_stocks: [] },
    ] }
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  draw.chart.mockReturnValue({ addSeries: () => ({ createPriceLine: vi.fn(), setData: draw.points }),
    applyOptions: vi.fn(), remove: vi.fn(), timeScale: () => ({ fitContent: vi.fn() }) })
  calls.themes.mockImplementation(async date => model(date))
  calls.home.mockImplementation(async (date, cutoff) => official(date, cutoff))
  calls.sync.mockImplementation(async date => model(date, true))
  calls.status.mockResolvedValue({ is_trading_hours: false })
  calls.research.mockImplementation(async (kind, date) => ({ kind, date, source: 'zzshare', fetched_at: '2025-02-05T15:20:00+08:00',
    stale: false, rows: kind === 'plates_rank' ? [{ plate_code: 'BP', plate_name: '供应商热榜第一', source_score: 98, change_pct: 3.6 }] : [] }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function settle() {
  await act(async () => {
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(20)
    else await new Promise(resolve => setTimeout(resolve, 20))
  })
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><ThemeRadar /></QueryClientProvider>))
  await settle()
}
async function click(name: string) {
  const target = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === name)
  expect(target, name).toBeDefined()
  await act(async () => target!.click())
  await settle()
}
it('保留个股插槽并注册独立入口，模型与供应商热榜分开且按选定日钻取', async () => {
  expect(extension.navigation?.find(n => n.label === '题材雷达')?.routeId).toBe(extension.routes?.[0].id)
  expect(extension.slots?.some(s => s.name === 'stock-preview.footer')).toBe(true)
  await render()
  await click('选取历史日')
  expect(host.textContent).toContain('官方第一席')
  expect(host.textContent).not.toContain('候选内排名第一')
  expect(calls.themes).not.toHaveBeenCalled()
  await click('供应商热榜')
  expect(host.textContent).toContain('供应商热榜第一')
  await click('本地强度')
  expect(calls.sync).not.toHaveBeenCalled()
  await click('计算题材排名')
  expect(calls.sync).toHaveBeenCalledWith(observed)
  expect(host.textContent).toContain('候选内排名第一')
  await click('样例龙头')
  expect(host.querySelector('[data-stock]')?.textContent).toContain('600000.SH · ' + observed)
})

it('页签按需加载，龙虎榜按所选个股独立查询席位与历史', async () => {
  calls.research.mockImplementation(async (kind, date) => ({ kind, date, source: 'zzshare', fetched_at: null, stale: kind === 'ths_hot_top',
    warning: kind === 'ths_hot_top' ? '来源延迟' : null,
    rows: kind === 'lhb_list' ? [{ trade_date: date, stock_code: '600000', stock_name: '上榜股票', symbol: '600000.SH', reason: '样本原因' }]
      : kind === 'lhb_detail' ? [{ rank: 1, side: 'buy', seat_name: '机构专用', buy_yuan: 150000000 }]
      : kind === 'lhb_stock_history' ? [{ trade_date: '2025-01-20', change_pct: -7.32 }]
      : kind === 'ths_hot_top' ? [{ rank: 1, stock_name: '热门样本', stock_code: '000001', change_pct: 5.55 }] : [] }))
  await render()
  await click('选取历史日')
  await click('供应商热榜')
  await click('行业')
  expect(calls.research).toHaveBeenLastCalledWith('plates_rank', observed, undefined, 14)
  await click('当前板块目录')
  await click('概念')
  expect(calls.research).toHaveBeenLastCalledWith('plates_list', observed, undefined, 15)
  await click('市场情绪')
  await click('日内情绪走势')
  await click('个股热榜')
  expect(host.textContent).toContain('+5.55%')
  expect(host.textContent).toContain('使用缓存')
  await click('龙虎榜')
  expect(calls.research.mock.calls.map(([kind]) => kind)).not.toContain('lhb_detail')
  await click('席位')
  expect(calls.research).toHaveBeenLastCalledWith('lhb_detail', observed, '600000')
  expect(host.textContent).toContain('机构专用')
  expect(host.textContent).toContain('1.50亿元')
  await click('历史')
  expect(calls.research).toHaveBeenLastCalledWith('lhb_stock_history', observed, '600000')
  expect(host.textContent).toContain('2025-01-20')
  expect(host.textContent).not.toContain('机构专用')
  expect(new Set(calls.research.mock.calls.map(([kind]) => kind)).size).toBe(8)
})

it('今日盘中已有已完成排名时，停留五分钟才重算；历史页不触发', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-04T10:00:00+08:00'))
  calls.status.mockResolvedValue({ is_trading_hours: true })
  calls.themes.mockImplementation(async date => model(date, true))
  await render()
  await click('本地强度')
  expect(host.textContent).toContain('盘中每 5 分钟重算')
  await act(async () => vi.advanceTimersByTimeAsync(299000))
  expect(calls.sync).not.toHaveBeenCalled()
  await act(async () => vi.advanceTimersByTimeAsync(1010))
  expect(calls.sync).toHaveBeenCalledTimes(1)
  await click('选取历史日')
  await act(async () => vi.advanceTimersByTimeAsync(300000))
  expect(calls.sync).toHaveBeenCalledTimes(1)
})

it('错日和失败均不绘制旧榜单，仍可切换视图', async () => {
  calls.research.mockResolvedValue({ kind: 'plates_rank', date: '2001-01-01', rows: [{ plate_name: '过期榜单' }] })
  await render()
  await click('供应商热榜')
  expect(host.textContent).toContain('数据与所选日期不一致')
  expect(host.textContent).not.toContain('过期榜单')
  calls.research.mockRejectedValue(new Error('行情服务不可用'))
  await click('个股热榜')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('行情服务不可用')
})

it('官网席位保留官方顺序，含来源分、核心股、覆盖与历史环比', async () => {
  await render()
  await click('选取历史日')
  const headings = [...host.querySelectorAll('h3')].map(el => el.textContent)
  expect(headings).toEqual(['官方第一席', '官方第二席', '全池高分第三席'])
  expect(host.querySelector('h3')?.title).toBe('官方第一席(251008)')
  expect(host.textContent).toContain('策略池选前两席，再从全池补一席')
  expect(host.textContent).toContain('上升 1 位')
  expect(host.textContent).toContain('至少 2 个交易日')
  expect(host.textContent).toContain('新进前三')
  expect(host.textContent).toContain('10/11')
  await click('官网核心样本')
  expect(host.querySelector('[data-stock]')?.textContent).toBe('000001.SZ · ' + observed)
})

it('核心股先展示两只，展开后仍能按所选日打开其余标的且保留缺失值', async () => {
  calls.home.mockImplementation(async date => {
    const result = official(date)
    const coreStocks = [...result.rows[0].core_stocks,
      { symbol: '600000.SH', name: '第二只核心股', role: '来源角色二', change_pct: 2, price_yuan: null, core_score: 80 },
      { symbol: '600001.SH', name: '第三只核心股', role: '来源角色三', change_pct: null, price_yuan: null, core_score: null },
    ]
    return { ...result, rows: result.rows.map((row, index) => index === 0 ? { ...row, core_stocks: coreStocks } : row) }
  })
  await render(); await click('选取历史日')
  const card = host.querySelector('ol[aria-label="官网精选题材"] > li')!
  const outsideDetails = [...card.querySelectorAll('button')].filter(button => !button.closest('details'))
  expect(outsideDetails.map(button => button.textContent)).toEqual(['官网核心样本', '第二只核心股'])
  const details = card.querySelector('details')!
  expect(details.open).toBe(false)
  await act(async () => details.querySelector('summary')!.click())
  expect(details.open).toBe(true)
  const extra = [...details.querySelectorAll('li')].find(li => li.textContent?.includes('第三只核心股'))!
  expect(extra.textContent).toContain('来源角色三')
  expect(extra.textContent).toContain('价格 — · 核心分 —')
  await click('第三只核心股')
  expect(host.querySelector('[data-stock]')?.textContent).toBe('600001.SH · ' + observed)
})

it.each(['stale', 'unavailable'])('折叠数据详情不会隐藏 %s 状态', async state => {
  calls.home.mockImplementation(async date => ({ ...official(date),
    stale: state === 'stale', state: state === 'unavailable' ? 'unavailable' : 'ready',
    message: state === 'unavailable' ? '来源暂不可用，请稍后重试。' : '',
  }))
  await render()
  const message = [...host.querySelectorAll('[role=status]')].find(el => el.textContent?.includes(state === 'stale' ? '沿用此前记录' : '来源暂不可用'))
  expect(message).toBeDefined()
  expect(message!.closest('details')).toBeNull()
  expect([...host.querySelectorAll('details')].every(details => !details.open)).toBe(true)
})

async function selectPoint(value: string) {
  const select = host.querySelector('select')!
  await act(async () => {
    select.value = value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
}
async function cutoffTime(value: string) {
  const input = host.querySelector<HTMLInputElement>('input[type=time]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
}

it('选择实录立即清除日榜；过早截止只提示无实录，之后可返回官方日榜', async () => {
  await render()
  await click('选取历史日')
  let finish!: () => void
  const recorded = observed + 'T10:05:14+08:00'
  calls.home.mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve(official(observed, recorded)) }))
  await selectPoint(recorded)
  expect(host.textContent).not.toContain('官方第一席')
  expect(host.textContent).toContain('正在读取官网席位')
  await act(async () => finish()); await settle()
  expect(host.textContent).toContain('历史时点主题')
  expect(host.textContent).toContain('完整日 K')
  await selectPoint('manual')
  calls.home.mockImplementationOnce(async (date, cutoff) => ({ ...official(date, cutoff), state: 'no_data', rows: [], fetched_at: null, source_at: null }))
  await cutoffTime('09:28')
  expect(calls.home).toHaveBeenLastCalledWith(observed, observed + 'T09:28:00+08:00')
  expect(host.textContent).toContain('截至该时点没有可用的本地实录')
  expect(host.textContent).not.toContain('历史时点主题')
  await selectPoint('')
  expect(host.textContent).toContain('官方第一席')
})

it.each(['source_at', 'fetched_at'])('拒绝 %s 晚于截止时间的榜单', async field => {
  await render()
  await click('选取历史日')
  calls.home.mockImplementationOnce(async (date, cutoff) => ({ ...official(date, cutoff), [field]: date + 'T11:00:00+08:00' }))
  await selectPoint(observed + 'T10:05:14+08:00')
  expect(host.textContent).toContain('与所选时点不符')
  expect(host.querySelector('ol[aria-label="官网精选题材"]')).toBeNull()
})

it('官网最新榜盘中每五分钟刷新，历史实录暂停轮询且不启动本地模型', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-04T11:00:00+08:00'))
  calls.status.mockResolvedValue({ is_trading_hours: true })
  await render()
  expect(calls.home).toHaveBeenCalledTimes(1)
  await act(async () => vi.advanceTimersByTimeAsync(300010))
  expect(calls.home).toHaveBeenCalledTimes(2)
  await selectPoint('2026-11-04T10:05:14+08:00')
  expect(calls.home).toHaveBeenCalledTimes(3)
  await act(async () => vi.advanceTimersByTimeAsync(300010))
  expect(calls.home).toHaveBeenCalledTimes(3)
  expect(calls.sync).not.toHaveBeenCalled()
})

it('本地前三与全部入围名称按日期和板块代码打开同一成分弹窗', async () => {
  calls.themes.mockImplementation(async date => model(date, true))
  calls.members.mockImplementation(async (plate: string, date: string) => ({
    plate_code: plate, date, total: 0, rows: [], label: 'ZZShare 当日成分',
  }))
  calls.intraday.mockImplementation(async (_plate: string, date: string) => ({ date, status: 'empty', points: [] }))
  await render()
  await click('选取历史日')
  await click('本地强度')
  const names = [...host.querySelectorAll('button')].filter(b => b.textContent === '候选内排名第一')
  expect(names).toHaveLength(2)
  expect(calls.members).not.toHaveBeenCalled()
  for (const name of names) {
    await act(async () => name.click())
    await act(async () => { await vi.dynamicImportSettled() })
    await settle()
    expect(host.querySelector('[role=dialog]')?.textContent).toContain('ZZShare 当日成分')
    expect(host.querySelector('[role=dialog]')?.textContent).toContain(observed)
    expect(host.querySelector('[role=dialog]')?.textContent).toContain('暂无成分股')
    await act(async () => host.querySelector<HTMLButtonElement>('button[title=关闭]')!.click())
  }
  expect(calls.members).toHaveBeenCalledTimes(1)
  expect(calls.members).toHaveBeenCalledWith('A1', observed)
  expect(calls.intraday).toHaveBeenCalledWith('A1', observed)
  expect(calls.market).not.toHaveBeenCalled()
})

it('成员搜索、排序、等权分钟标签及个股跳转使用所选日，忽略最新快照缓存', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const originalScroll = HTMLElement.prototype.scrollTo
  HTMLElement.prototype.scrollTo = vi.fn()
  try {
    calls.themes.mockImplementation(async date => model(date, true))
    calls.members.mockResolvedValue({ plate_code: 'A1', date: observed, label: 'ZZShare 当日成分', total: 3,
      rows: [{ symbol: '600000.SH', name: '首日成员甲', close: 20, change_pct: .052, turnover_rate: 1.78, amount: 230000 },
        { symbol: '000001.SZ', name: '首日成员乙', close: 10, change_pct: -.015, amount: 500000 },
        { symbol: '110077', name: '待核验标的', close: null, change_pct: null, amount: null }] })
    calls.intraday.mockResolvedValue({ date: observed, status: 'ok', basis: 'prev_close',
      points: [{ time: '09:31', sector: .001, market: -.001 }, { time: '09:32', sector: .002, market: 0 }] })
    client.setQueryData(QK.marketSnapshot, { as_of: '2026-11-04', rows: [{ symbol: '600000.SH', name: '次日错误名称', close: 99 }] })
    await render()
    await click('选取历史日'); await click('本地强度'); await click('候选内排名第一')
    await act(async () => { await vi.dynamicImportSettled() }); await settle()
    const dialog = host.querySelector<HTMLElement>('[role=dialog]')!
    const memberRows = () => [...dialog.querySelectorAll<HTMLButtonElement>('button[data-index]')]
    expect(memberRows()[0].textContent).toContain('首日成员甲')
    expect(memberRows()[0].textContent).toContain('+5.20%')
    expect(memberRows()[2].disabled).toBe(true)
    expect(dialog.textContent).not.toContain('次日错误名称')
    expect(calls.market).not.toHaveBeenCalled()
    const timeline = draw.points.mock.calls[0][0]
    const chart = draw.chart.mock.calls[0][1]
    expect(timeline.map((p: {time: number}) => chart.timeScale.tickMarkFormatter(p.time))).toEqual(['09:31', '09:32'])
    const sort = dialog.querySelector<HTMLSelectElement>('[aria-label=排序方式]')!
    await act(async () => { sort.value = 'amount_desc'; sort.dispatchEvent(new Event('change', { bubbles: true })) })
    expect(memberRows()[0].textContent).toContain('首日成员乙')
    const search = dialog.querySelector<HTMLInputElement>('input')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, '600000')
      search.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(memberRows()).toHaveLength(1)
    await act(async () => memberRows()[0].click())
    expect(host.querySelector('[role=dialog]')).toBeNull()
    expect(host.querySelector('[data-stock]')?.textContent).toBe('600000.SH · ' + observed)
    expect(host.querySelector('[data-stock]')?.getAttribute('data-nav')).toBe('600000.SH')
  } finally {
    HTMLElement.prototype.scrollTo = originalScroll
  }
})

it('成员返回其它日期时提示错误并跳过分时请求', async () => {
  calls.themes.mockImplementation(async date => model(date, true))
  calls.members.mockResolvedValue({ plate_code: 'A1', date: '2025-02-06', total: 1, rows: [{ symbol: '000001.SZ', name: '非所选日成员' }] })
  await render()
  await click('选取历史日'); await click('本地强度'); await click('候选内排名第一')
  await act(async () => { await vi.dynamicImportSettled() }); await settle()
  const dialog = host.querySelector('[role=dialog]')!
  expect(dialog.textContent).toContain('成分数据与所选题材或交易日不符')
  expect(dialog.textContent).not.toContain('非所选日成员')
  expect(calls.intraday).not.toHaveBeenCalled()
})
