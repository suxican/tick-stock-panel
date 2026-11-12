// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { RecapSnapshot } from '@/lib/api'
import { RecapSnapshotPanel, stripPlanBlock } from './RecapSnapshot'

const chart = vi.hoisted(() => ({ setOption: vi.fn() }))
vi.mock('echarts', () => ({
  init: () => ({ setOption: chart.setOption, resize: vi.fn(), dispose: vi.fn() }),
}))

const snapshot: RecapSnapshot = {
  version: 1,
  as_of: '2026-11-11',
  intraday: false,
  generated_at: '2026-11-11T20:00:00',
  today_source: 'regime',
  phase: { key: 'ignite', label: '启动', days: 3, capped: false, date: '2026-11-11', state_label: '强势' },
  dates: ['2026-11-09', '2026-11-10', '2026-11-11'],
  metrics: [
    { key: 'amount_yi', label: '成交额', unit: '亿', digits: 0, today: 16812, prev: 15094, avg5: 17656, series: [14860, 15094, 16812] },
    { key: 'limit_up', label: '涨停', unit: '家', digits: 0, today: 85, prev: 93, avg5: 77, series: [55, 93, 85] },
    { key: 'seal_rate', label: '封板率', unit: '%', digits: 0, today: 78, prev: 82, avg5: 68, series: [53, 82, 78] },
    { key: 'max_boards', label: '最高连板', unit: '板', digits: 0, today: 4, prev: 7, avg5: 5, series: [5, 7, 4] },
    { key: 'promo_rate', label: '连板晋级率', unit: '%', digits: 0, today: 20.4, prev: 35.7, avg5: 25, series: [31, 36, 20] },
  ],
  mainlines: [
    { name: '特斯拉概念', rank: 1, limit_up: 10, max_boards: 4, leader: { symbol: '603366.SH', name: '龙头甲' }, streak: 3 },
    { name: '虚拟现实', rank: 2, limit_up: 8, max_boards: 3, leader: null, streak: 1 },
  ],
  sectors: { leading: [{ name: 'CPO', pct: 4.64, leader: '光一' }], lagging: [{ name: '白酒', pct: -3.08, leader: null }] },
  ladder: [
    { boards: 4, count: 3, stocks: [{ symbol: '000001.SZ', name: '四板甲' }] },
    { boards: 1, count: 66, stocks: [] },
  ],
  prev_limit_up: { count: 90, avg_pct: 2.15, up_ratio: 64 },
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.setOption.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

it('renders phase, metric comparisons, mainlines and ladder from the snapshot', () => {
  const onOpenStock = vi.fn()
  act(() => root.render(<RecapSnapshotPanel snapshot={snapshot} onOpenStock={onOpenStock} />))
  const text = host.textContent ?? ''

  expect(text).toContain('周期 · 启动 第3天')
  // 成交额较昨日 +11%, 封板率给差值 -4pp
  expect(text).toContain('16812')
  expect(text).toContain('+11%')
  expect(text).toContain('-4pp')
  expect(text).toContain('-3板')  // 连板高度给差值而非相对变化
  expect(text).toContain('特斯拉概念')
  expect(text).toContain('首日')
  expect(text).toContain('等3家')
  expect(text).toContain('晋级率 20%')
  expect(text).toContain('昨日涨停今日 +2.15%')
  expect(text).toContain('白酒 -3.08%')

  const leader = Array.from(host.querySelectorAll('button')).find(b => b.textContent === '龙头甲')!
  act(() => leader.click())
  expect(onOpenStock).toHaveBeenCalledWith('603366.SH')

  // 走势图: 成交额柱 + 涨停线
  const option = chart.setOption.mock.calls.at(-1)?.[0]
  expect(option.series.map((s: { name: string }) => s.name)).toEqual(['成交额', '涨停'])
})

it('marks intraday snapshots and missing mainline data', () => {
  act(() => root.render(
    <RecapSnapshotPanel snapshot={{ ...snapshot, intraday: true, generated_at: '2026-11-11T11:30:00', mainlines: [] }} />,
  ))
  const text = host.textContent ?? ''
  expect(text).toContain('盘中快照 11:30')
  expect(text).toContain('无达标涨停主线')
})

it('labels live-computed mainlines and marks sector chips that belong to a mainline', () => {
  act(() => root.render(<RecapSnapshotPanel snapshot={{
    ...snapshot,
    mainline_source: 'live',
    sectors: { leading: [{ name: '特斯拉概念', pct: 3.2, leader: null, mainline: true }, { name: 'CPO', pct: 4.64, leader: '光一', mainline: false }], lagging: [] },
  }} />))
  expect(host.textContent).toContain('涨停主线· 盘中')
  const chips = Array.from(host.querySelectorAll('span[title]'))
  expect(chips.find(c => c.textContent?.startsWith('特斯拉概念'))?.getAttribute('title')).toContain('属于涨停主线')
  expect(chips.find(c => c.textContent?.startsWith('CPO'))?.getAttribute('title')).toContain('未进入涨停主线')
})

it('renders the system plan check with hit-rate stats', () => {
  act(() => root.render(<RecapSnapshotPanel snapshot={{
    ...snapshot,
    plan_check: {
      from_as_of: '2026-11-10', as_of: '2026-11-11',
      scenarios: [
        { name: '强于预期', primary: false, status: 'miss', conditions: [
          { label: '开盘半小时成交额 ≥ 6000亿', actual: 5341, actual_text: '5341亿', status: 'miss' },
        ] },
        { name: '符合预期', primary: true, status: 'hit', conditions: [
          { label: '日出东方连板数 ≥ 5板', actual: 5, actual_text: '5板', status: 'hit' },
        ] },
        { name: '弱于预期', primary: false, match: 'any', status: 'pending', conditions: [
          { label: '开盘半小时成交额 < 4800亿', actual: null, actual_text: '—', status: 'pending' },
          { label: '涨停家数 < 40家', actual: null, actual_text: '—', status: 'pending' },
        ] },
      ],
    },
    plan_stats: { primary_total: 5, primary_hits: 3, covered_total: 4, covered: 4 },
  }} />))
  const text = host.textContent ?? ''
  expect(text).toContain('来自 11-10')
  expect(text).toContain('主判断命中 3/5')
  expect(text).toContain('剧本覆盖 4/4')
  expect(text).toContain('开盘半小时成交额 ≥ 6000亿→ 5341亿')
  expect(text).toContain('符合预期主判断')
  expect(text).toContain('✅ 命中')
  expect(text).toContain('❌ 落空')
  expect(text).toContain('➖ 待定')
  expect(text).toContain('或涨停家数 < 40家')
})

it('strips the plan block, including a half-streamed fence', () => {
  expect(stripPlanBlock('正文\n> 免责\n\n```plan\n{"scenarios":[]}\n```\n')).toBe('正文\n> 免责\n')
  expect(stripPlanBlock('正文\n```plan\n{"scen')).toBe('正文\n')
  expect(stripPlanBlock('正文\n```pl')).toBe('正文\n')
  expect(stripPlanBlock('正文')).toBe('正文')
})

it('renders holdings linkage with market-only mode and program conditions', () => {
  const onOpenStock = vi.fn()
  act(() => root.render(
    <MemoryRouter>
      <RecapSnapshotPanel onOpenStock={onOpenStock} snapshot={{
        ...snapshot,
        holdings: {
          as_of: '2026-11-11', market_only: 1,
          warnings: [],
          rows: [{
            symbol: '600522.SH', name: '中天科技', mode: 'market', missing: ['建仓日', '失效条件'],
            close: 24.65, price_date: '2026-11-11', pnl_pct: -57.5, pnl_state: 'deep_loss', age_days: null,
            trend: 'down', relative: 'weaker', stock_pct: 4.01,
            sector: { name: '通信线缆及配套', pct: 6.25 },
            hot: [{ name: '光纤概念', side: 'leading', pct: 4.42 }],
            mainline: { name: '特斯拉概念', rank: 1, streak: 3 },
            conditions: [{ metric: 'close', op: '>=', value: 26.8, label: '收盘 ≥ 26.80(压力)', meaning: '站上压力, 联动走强' }],
            warnings: ['最近20个完整交易日日线缺失、重复或未核准'],
          }, {
            symbol: '000657.SZ', name: '中钨高新', mode: 'market', missing: ['建仓日'],
            close: 41.79, price_date: '2026-11-11', pnl_pct: -36.7, pnl_state: 'deep_loss', age_days: null,
            trend: 'unknown', relative: 'unknown', stock_pct: 2.18, sector: null, hot: [], mainline: null,
            conditions: [], warnings: ['最近20个完整交易日日线缺失、重复或未核准'],
          }],
        },
      }} />
    </MemoryRouter>,
  ))
  const text = host.textContent ?? ''
  expect(text).toContain('1 只缺建仓日或失效条件, 只看市场联动')
  expect(text).toContain('只看联动')
  expect(text).toContain('主线 特斯拉概念 第1 · 3日')
  expect(text).toContain('通信线缆及配套 +6.25% · 跑输')
  expect(text).toContain('-57.5%深套')
  expect(text).toContain('收盘 ≥ 26.80(压力) → 站上压力, 联动走强')
  expect(text).toContain('未进入今日热点')
  expect(text).toContain('技术位不可用')
  // 共同的数据原因只提示一次
  expect(text.split('最近20个完整交易日日线缺失').length - 1).toBe(1)
  expect(host.querySelector('a[href="/lots"]')).not.toBeNull()
  act(() => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '中天科技')!.click())
  expect(onOpenStock).toHaveBeenCalledWith('600522.SH')
})

it('flags ambiguous plan checks and shows overlap counts', () => {
  act(() => root.render(<RecapSnapshotPanel snapshot={{
    ...snapshot,
    plan_check: {
      from_as_of: '2026-11-10', as_of: '2026-11-11', ambiguous: true, overlaps: [['强于预期', '符合预期']],
      scenarios: [
        { name: '强于预期', primary: false, status: 'hit', conditions: [{ label: '开盘半小时成交额 ≥ 5000亿', actual: 5341, actual_text: '5341亿', status: 'hit' }] },
        { name: '符合预期', primary: true, status: 'hit', conditions: [{ label: '开盘半小时成交额 ≥ 4000亿', actual: 5341, actual_text: '5341亿', status: 'hit' }] },
      ],
    },
    plan_stats: { primary_total: 2, primary_hits: 1, covered_total: 3, covered: 3, ambiguous: 1 },
  }} />))
  const text = host.textContent ?? ''
  expect(text).toContain('多个情景同时命中: 剧本情景不互斥, 本次不计入主判断命中率')
  expect(text).toContain('多情景命中 1 次')
})

it('lists holding conditions triggered today', () => {
  act(() => root.render(
    <MemoryRouter>
      <RecapSnapshotPanel snapshot={{
        ...snapshot,
        holdings: { as_of: '2026-11-11', market_only: 0, warnings: [], rows: [{
          symbol: '600522.SH', name: '中天科技', mode: 'full', missing: [], close: 27.1, price_date: '2026-11-11',
          pnl_pct: -50, pnl_state: 'deep_loss', age_days: 10, trend: 'down', relative: 'weaker', stock_pct: 1,
          sector: null, hot: [], mainline: null, conditions: [], warnings: [],
        }] },
        holdings_check: { from_as_of: '2026-11-10', as_of: '2026-11-11', triggered: 1, rows: [{
          symbol: '600522.SH', name: '中天科技', close: 27.1, conditions: [
            { label: '收盘 ≥ 26.80(压力)', meaning: '站上压力, 联动走强', status: 'triggered' },
            { label: '收盘 ≤ 22.10(支撑)', meaning: '跌破支撑', status: 'not' },
          ],
        }] },
      }} />
    </MemoryRouter>,
  ))
  const text = host.textContent ?? ''
  expect(text).toContain('昨日持仓条件(11-10 给出)今日触发')
  expect(text).toContain('中天科技 收盘 ≥ 26.80(压力) → 站上压力, 联动走强(收盘 27.10)')
  expect(text).not.toContain('跌破支撑')
})
